param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$RemoteRoot,
    [Parameter(Mandatory = $true)][string]$RemoteRuntimeRoot,
    [Parameter(Mandatory = $true)][string]$RemoteEnvPath,
    [Parameter(Mandatory = $true)][string]$ServiceName,
    [Parameter(Mandatory = $true)][string]$HealthUrl,
    [string]$ServiceUser = "",
    [string]$QuoteMuxSourceRoot = "",
    [string]$QuoteMuxPackagesSourceRoot = "",
    [ValidateSet("peer", "env")][string]$PrivilegedMigrationMode = "peer",
    [string]$PrivilegedMigrationEnvPath = "/data/markethub/env/quotemux-futures-partial-migration.env",
    [ValidateRange(30, 1800)][int]$CaptureDrainTimeoutSeconds = 300,
    [ValidateRange(1, 60)][int]$CaptureDrainRetrySeconds = 10
)

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param([Parameter(Mandatory = $true)][string]$FilePath, [Parameter(Mandatory = $true)][string[]]$Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $FilePath $($Arguments -join ' ')"
    }
}

function Get-PinnedCommit {
    param([Parameter(Mandatory = $true)][string]$RepositoryPath)

    $trackedChanges = @((& git -C $RepositoryPath status --porcelain=v1) | Where-Object { $_ -notmatch '^\?\?' })
    if ($trackedChanges.Count -ne 0) {
        throw "部署输入仓库不能有已跟踪的未提交改动: $RepositoryPath"
    }
    $commit = ((& git -C $RepositoryPath rev-parse HEAD) -replace "`0", "").Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') {
        throw "无法固定部署输入 commit: $RepositoryPath"
    }
    return $commit
}

$marketHubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workspaceRoot = Split-Path $marketHubRoot -Parent
$quoteMuxRoot = if ([string]::IsNullOrWhiteSpace($QuoteMuxSourceRoot)) { Join-Path $workspaceRoot "QuoteMux" } else { $QuoteMuxSourceRoot }
$quoteMuxPackagesRoot = if ([string]::IsNullOrWhiteSpace($QuoteMuxPackagesSourceRoot)) { Join-Path $workspaceRoot "QuoteMux_Packages" } else { $QuoteMuxPackagesSourceRoot }
$quoteMuxRoot = (Resolve-Path -LiteralPath $quoteMuxRoot).Path
$quoteMuxPackagesRoot = (Resolve-Path -LiteralPath $quoteMuxPackagesRoot).Path
foreach ($path in @($marketHubRoot, $quoteMuxRoot, $quoteMuxPackagesRoot)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "缺少部署目录: $path"
    }
}
if ([string]::IsNullOrWhiteSpace($ServiceUser)) {
    $ServiceUser = (& ssh $HostName "id -un") -replace "`0", ""
    $ServiceUser = $ServiceUser.Trim()
}
if ([string]::IsNullOrWhiteSpace($ServiceUser)) { throw "无法确定远端 MarketHub 服务用户" }

$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) "$releaseName.tgz"
$marketHubCommit = Get-PinnedCommit $marketHubRoot
$quoteMuxCommit = Get-PinnedCommit $quoteMuxRoot
$quoteMuxPackagesCommit = Get-PinnedCommit $quoteMuxPackagesRoot
$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) "$releaseName-source"
New-Item -ItemType Directory -Path $stagingRoot -ErrorAction Stop | Out-Null
try {
    foreach ($source in @(
        @{ Path = $marketHubRoot; Commit = $marketHubCommit; Name = "MarketHub" },
        @{ Path = $quoteMuxRoot; Commit = $quoteMuxCommit; Name = "QuoteMux" },
        @{ Path = $quoteMuxPackagesRoot; Commit = $quoteMuxPackagesCommit; Name = "QuoteMux_Packages" }
    )) {
        $sourceArchive = Join-Path $stagingRoot ("$($source.Name).tar")
        Invoke-NativeCommand -FilePath "git" -Arguments @("-C", $source.Path, "archive", "--format=tar", "--prefix=$($source.Name)/", "--output=$sourceArchive", $source.Commit)
        Invoke-NativeCommand -FilePath "tar.exe" -Arguments @("-xf", $sourceArchive, "-C", $stagingRoot)
        Remove-Item -LiteralPath $sourceArchive -Force
    }
    Invoke-NativeCommand -FilePath "tar.exe" -Arguments @(
        "-czf", $archivePath,
        "--exclude=.git", "--exclude=.pytest_cache", "--exclude=__pycache__",
        "--exclude=.venv", "--exclude=build", "--exclude=*.egg-info", "--exclude=quotemux_packages.egg-info",
        "--exclude=runtime", "--exclude=.runtime", "--exclude=.tmp", "--exclude=scratch", "--exclude=tests",
        "-C", $stagingRoot, "MarketHub", "QuoteMux", "QuoteMux_Packages"
    )
}
finally {
    if (Test-Path -LiteralPath $stagingRoot) { Remove-Item -LiteralPath $stagingRoot -Recurse -Force }
}

$remoteArchive = "/tmp/$releaseName.tgz"
Invoke-NativeCommand -FilePath "scp" -Arguments @($archivePath, ($HostName + ':' + $remoteArchive))

$remoteScript = @'
set -euo pipefail
remote_root="$1"
release_name="$2"
remote_archive="$3"
runtime_root="$4"
env_path="$5"
service_name="$6"
service_user="$7"
health_url="$8"
market_hub_commit="$9"
quote_mux_commit="${10}"
quote_mux_packages_commit="${11}"
migration_mode="${12}"
migration_env_path="${13}"
capture_drain_timeout_seconds="${14}"
capture_drain_retry_seconds="${15}"
reader_env_path="$(dirname "$env_path")/quotemux-public-reader.env"
publisher_env_path="$(dirname "$env_path")/quotemux-futures-partial-publisher.env"
release_root="$remote_root/releases/$release_name"
previous_current="$(readlink -f "$remote_root/current" 2>/dev/null || true)"
current_switched=0
service_stopped=0
migration_stage=""
publisher_target_stage=""
reader_target_stage=""
env_backup="/tmp/${service_name}-${release_name}.env.bak"
unit_path="/etc/systemd/system/$service_name.service"
unit_backup="/tmp/${service_name}-${release_name}.service.bak"
shared_backup="/tmp/${service_name}-${release_name}.shared.bak"
scripts_existed=0; publisher_existed=0; governance_existed=0
cp "$env_path" "$env_backup"
if sudo -n test -f "$unit_path"; then sudo -n cp "$unit_path" "$unit_backup"; fi
mkdir -p "$shared_backup"
if test -d "$runtime_root/scripts"; then cp -a "$runtime_root/scripts" "$shared_backup/scripts"; scripts_existed=1; fi
if test -d "$runtime_root/publisher"; then cp -a "$runtime_root/publisher" "$shared_backup/publisher"; publisher_existed=1; fi
if sudo -n test -f /usr/local/sbin/markethub-storage-governance; then sudo -n cp /usr/local/sbin/markethub-storage-governance "$shared_backup/governance"; governance_existed=1; fi
restart_on_exit() {
  if [ -n "$migration_stage" ]; then sudo -n rm -rf "$migration_stage" || true; fi
  if [ -n "$publisher_target_stage" ]; then sudo -n rm -f "$publisher_target_stage" || true; fi
  if [ -n "$reader_target_stage" ]; then sudo -n rm -f "$reader_target_stage" || true; fi
  if [ "$current_switched" = 1 ] && [ -n "$previous_current" ]; then
    ln -sfn "$previous_current" "$remote_root/current.next"
    mv -Tf "$remote_root/current.next" "$remote_root/current"
  fi
  cp "$env_backup" "$env_path" || true
  if sudo -n test -f "$unit_backup"; then sudo -n cp "$unit_backup" "$unit_path"; fi
  rm -rf "$runtime_root/scripts" "$runtime_root/publisher"
  if [ "$scripts_existed" = 1 ]; then cp -a "$shared_backup/scripts" "$runtime_root/scripts"; fi
  if [ "$publisher_existed" = 1 ]; then cp -a "$shared_backup/publisher" "$runtime_root/publisher"; fi
  if [ "$governance_existed" = 1 ]; then sudo -n cp "$shared_backup/governance" /usr/local/sbin/markethub-storage-governance; else sudo -n rm -f /usr/local/sbin/markethub-storage-governance; fi
  if [ "$service_stopped" = 1 ] || [ "$current_switched" = 1 ]; then
    sudo -n systemctl daemon-reload || true
    sudo -n systemctl restart "$service_name.service" >/dev/null 2>&1 || true
    if ! curl -fsS "$health_url" >/dev/null; then
      echo "rollback failed: previous release health check also failed" >&2
    fi
  fi
}
trap restart_on_exit EXIT

test -f "$env_path"
mkdir -p "$release_root" "$runtime_root"
tar --no-same-owner -xzf "$remote_archive" -C "$release_root"
printf '{"market_hub_commit":"%s","quote_mux_commit":"%s","quote_mux_packages_commit":"%s"}\n' "$market_hub_commit" "$quote_mux_commit" "$quote_mux_packages_commit" > "$release_root/release-inputs.json"
find "$release_root/MarketHub" -type f -name '*.sh' -exec chmod 0755 {} +
rm -rf "$release_root/QuoteMux_Packages/quotemux_packages.egg-info" "$release_root/QuoteMux_Packages/build"
service_group="$(id -gn "$service_user")"
sudo -n chown -R "$service_user:$service_group" "$release_root"
test -x "$runtime_root/.venv/bin/python" || python3 -m venv "$runtime_root/.venv"
"$runtime_root/.venv/bin/python" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python" -m pip install -e "$release_root/QuoteMux"
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
"$runtime_root/.venv/bin/python" -m pip install -r "$release_root/MarketHub/requirements.txt"
python3 "$release_root/MarketHub/migrations/storage_v2_20260823/sync_runtime_env.py" \
  --env-file "$env_path" \
  --app-root "$remote_root" \
  --runtime-root "$runtime_root" \
  --release-root "$release_root" \
  --package-venv-root "$runtime_root/package_venvs/$release_name"
sudo -n chmod 0600 "$env_path"
sudo -n chown "$service_user:$service_group" "$env_path"
set -a
. "$env_path"
set +a
# 环境文件可能保留旧 current 的包源，安装当前 release 前必须以本次发布目录为准。
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
export MARKETHUB_RUNTIME_ROOT="$runtime_root"
export MARKETHUB_ENV_PATH="$env_path"
export MARKETHUB_VENV_ROOT="$runtime_root/.venv"
export QUOTEMUX_PACKAGE_VENV_ROOT="$runtime_root/package_venvs/$release_name"
export QUOTEMUX_ALLOW_LOCAL_PACKAGE_REPO=true
export PYTHONPATH="$release_root/QuoteMux/src:$release_root/MarketHub/services/markethub_api/src"
mkdir -p "$runtime_root/type=cache"
sudo -n chown -R "$(id -un):$(id -gn)" "$runtime_root/type=cache" || true
# package_venvs 是部署用户维护的运行时生成目录；清除历史 sudo 安装留下的所有权漂移。
sudo -n chown -R "$(id -un):$(id -gn)" "$runtime_root/package_venvs" || true
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/install_all_packages.py"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/deploy/bootstrap_database.py"
publisher_stage=""
reader_stage=""
case "$migration_mode" in
  peer)
    sudo -n -u postgres true
    : "${MARKETHUB_DB_HOST:?MARKETHUB_DB_HOST is required for TCP role probes}"
    : "${MARKETHUB_DB_PORT:?MARKETHUB_DB_PORT is required for peer migration}"
    : "${MARKETHUB_DB_NAME:?MARKETHUB_DB_NAME is required for peer migration}"
    sudo -n -u postgres test -S "/var/run/postgresql/.s.PGSQL.$MARKETHUB_DB_PORT"
    migration_stage="$(mktemp -d "/tmp/${service_name}-${release_name}-quotemux.XXXXXX")"
    publisher_stage="$migration_stage/publisher.env"
    reader_stage="$migration_stage/reader.env"
    mkdir -p "$migration_stage/code"
    cp -a "$release_root/QuoteMux/src/quotemux" "$migration_stage/code/quotemux"
    install -m 0644 "$release_root/MarketHub/migrations/quotemux_futures_partial_v1_20260826/release_migration.py" "$migration_stage/code/release_migration.py"
    if test -f "$publisher_env_path"; then cp "$publisher_env_path" "$publisher_stage"; fi
    if test -f "$reader_env_path"; then cp "$reader_env_path" "$reader_stage"; fi
    sudo -n chown -R postgres:postgres "$migration_stage"
    sudo -n chmod 0700 "$migration_stage"
    sudo -n -u postgres env \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_PEER=1 \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_SOCKET_DIR=/var/run/postgresql \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_HOST="$MARKETHUB_DB_HOST" \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_PORT="$MARKETHUB_DB_PORT" \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_MIGRATION_DB_NAME="$MARKETHUB_DB_NAME" \
      MARKETHUB_QUOTEMUX_FUTURES_PARTIAL_PUBLISHER_ENV="$publisher_stage" \
      MARKETHUB_QUOTEMUX_PUBLIC_READER_ENV="$reader_stage" \
      MARKETHUB_HEALTH_URL="$health_url" \
      PYTHONPATH="$migration_stage/code" \
      "$runtime_root/.venv/bin/python" "$migration_stage/code/release_migration.py"
    publisher_target_stage="${publisher_env_path}.${release_name}.new"
    reader_target_stage="${reader_env_path}.${release_name}.new"
    sudo -n install -o "$service_user" -g "$service_group" -m 0600 "$publisher_stage" "$publisher_target_stage"
    sudo -n install -o "$service_user" -g "$service_group" -m 0600 "$reader_stage" "$reader_target_stage"
    sudo -n mv -Tf "$publisher_target_stage" "$publisher_env_path"
    publisher_target_stage=""
    sudo -n mv -Tf "$reader_target_stage" "$reader_env_path"
    reader_target_stage=""
    sudo -n rm -rf "$migration_stage"
    migration_stage=""
    ;;
  env)
    test -f "$migration_env_path"
    set -a
    . "$migration_env_path"
    set +a
    MARKETHUB_HEALTH_URL="$health_url" "$runtime_root/.venv/bin/python" "$release_root/MarketHub/migrations/quotemux_futures_partial_v1_20260826/release_migration.py"
    ;;
  *) echo "unsupported QuoteMux privileged migration mode: $migration_mode" >&2; exit 2 ;;
esac
test -f "$reader_env_path"
test "$(stat -c %a "$reader_env_path")" = 600
test "$(stat -c %U "$reader_env_path")" = "$service_user"
test "$(stat -c %G "$reader_env_path")" = "$service_group"
test -f "$publisher_env_path"
test "$(stat -c %a "$publisher_env_path")" = 600
test "$(stat -c %U "$publisher_env_path")" = "$service_user"
test "$(stat -c %G "$publisher_env_path")" = "$service_group"
# 旧 API 仍在线时进行 staged install 和 privileged migration。只等待当前
# 合法持锁 capture 自行完成；到时仍有锁则 fail-closed，不 kill、不覆盖。
capture_drain_deadline=$((SECONDS + capture_drain_timeout_seconds))
while true; do
  set +e
  capture_reconcile_json="$("$runtime_root/.venv/bin/python" -c 'import json; from quotemux.store import reconcile_stale_capture_runs; result = reconcile_stale_capture_runs(); print(json.dumps(result, ensure_ascii=False)); raise SystemExit(20 if result["active_capability_ids"] else 0)')"
  capture_reconcile_status=$?
  set -e
  printf 'capture run reconciliation: %s\n' "$capture_reconcile_json"
  if [ "$capture_reconcile_status" = 0 ]; then break; fi
  if [ "$capture_reconcile_status" != 20 ]; then
    echo "capture reconciliation failed with status $capture_reconcile_status" >&2
    exit "$capture_reconcile_status"
  fi
  if [ "$SECONDS" -ge "$capture_drain_deadline" ]; then
    echo "active QuoteMux capture locks did not drain within ${capture_drain_timeout_seconds}s; keeping old release active" >&2
    exit 20
  fi
  sleep "$capture_drain_retry_seconds"
done
# 某些构建后端会在源码包目录重新生成 egg-info；发布产物不允许带入 root-owned 构建目录。
rm -rf "$release_root/QuoteMux_Packages/quotemux_packages.egg-info"
rm -rf "$release_root/QuoteMux_Packages/build"
rm -rf "$release_root/QuoteMux/src/quotemux.egg-info"
rm -rf "$release_root/QuoteMux/build"
sudo -n chown -R "$service_user:$service_group" "$release_root"
if ! sudo -n systemctl stop "$service_name.service" >/dev/null 2>&1; then
  # A source worker can keep Uvicorn's background task alive past TimeoutStopSec.
  # systemd may already have killed it; make the release handoff deterministic.
  sudo -n systemctl kill --kill-who=all --signal=KILL "$service_name.service" >/dev/null 2>&1 || true
  sudo -n systemctl reset-failed "$service_name.service" >/dev/null 2>&1 || true
fi
service_stopped=1
ln -sfn "$release_root" "$remote_root/current.next"
mv -Tf "$remote_root/current.next" "$remote_root/current"
current_switched=1
cat >/tmp/markethub-service <<UNIT
[Unit]
Description=MarketHub API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=$service_user
Group=$service_group
WorkingDirectory=$remote_root/current/MarketHub/services/markethub_api
EnvironmentFile=$env_path
# The QuoteMux partial reader has a distinct least-privilege credential and
# is generated by the staged privileged migration before this atomic switch.
EnvironmentFile=$reader_env_path
Environment=MARKETHUB_RUNTIME_ROOT=$runtime_root
Environment=MARKETHUB_DATA_ROOT=$runtime_root/store
Environment=MARKETHUB_RELEASE=$release_name
Environment=QUOTEMUX_RUNTIME_ROOT=$runtime_root/runtime
Environment=PYTHONPATH=$remote_root/current/QuoteMux/src:$remote_root/current/MarketHub/services/markethub_api/src
Environment=QUOTEMUX_PACKAGE_REPO_SPEC=$remote_root/current/QuoteMux_Packages
Environment=QUOTEMUX_PACKAGE_VENV_ROOT=$runtime_root/package_venvs/$release_name
ExecStart=$runtime_root/.venv/bin/python $remote_root/current/MarketHub/services/markethub_api/app.py
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30s
MemoryHigh=12G
MemoryMax=18G
MemorySwapMax=2G
OOMPolicy=stop

[Install]
WantedBy=multi-user.target
UNIT
sudo -n install -m 0644 /tmp/markethub-service "/etc/systemd/system/$service_name.service"
sudo -n systemctl daemon-reload
sudo -n systemctl enable "$service_name.service"
sudo -n systemctl restart "$service_name.service"
for attempt in $(seq 1 20); do
  if curl -fsS "$health_url" >/dev/null; then
    api_base="${health_url%/api/health}"
    health_payload="$(curl -fsS "$health_url")"
    stock_data_version="$(printf '%s' "$health_payload" | "$runtime_root/.venv/bin/python" -c 'import json, sys, urllib.parse; value = json.load(sys.stdin).get("data_version"); assert isinstance(value, str) and value; print(urllib.parse.quote(value, safe=""))')"
    curl -fsS "$api_base/api/stocks/quotes?code=600000&freq=1d&count=1&data_version=$stock_data_version" >/dev/null
    strict_status="$(curl -sS -o /tmp/markethub-strict-futures.json -w '%{http_code}' "$api_base/api/futures/quotes/1m?codes=ag,al,AP,CF,cu,hc,i,j,m,MA,ni,p,ru,sc,T,TA,TF,v,y,lh,SA,ao,si&series_type=back_adjusted_continuous&start_time=2012-01-01%2009%3A01%3A00&end_time=2026-08-11%2015%3A00%3A00")"
    if [ "$strict_status" != 409 ]; then
      echo "strict futures readiness expected HTTP 409, got $strict_status" >&2
      exit 1
    fi
    mkdir -p "$runtime_root/scripts" "$runtime_root/publisher"
    install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/global-data-update.sh" "$runtime_root/scripts/global-data-update.sh"
    install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/global-data-update-with-health.sh" "$runtime_root/scripts/global-data-update-with-health.sh"
    install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/data-health-check.sh" "$runtime_root/scripts/data-health-check.sh"
    install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/update-futures-1m.sh" "$runtime_root/scripts/update-futures-1m.sh"
    install -m 0755 "$remote_root/current/MarketHub/scripts/maintenance/manage_formal_export_freeze.sh" "$runtime_root/scripts/manage-formal-export-freeze.sh"
    install -m 0755 "$remote_root/current/MarketHub/migrations/storage_v2_20260823/cleanup_after_migration.sh" "$runtime_root/scripts/storage-v2-cleanup-after-migration.sh"
    install -m 0755 "$remote_root/current/MarketHub/scripts/publisher/publish_stock_daily_parquet.py" "$runtime_root/publisher/publish_stock_daily_parquet.py"
    sudo -n install -m 0755 "$remote_root/current/MarketHub/scripts/maintenance/storage-governance.sh" /usr/local/sbin/markethub-storage-governance
    current_switched=0
    trap - EXIT
    rm -f "$remote_archive" /tmp/markethub-service
    exit 0
  fi
  sleep 2
done
echo "new MarketHub release failed health check; restoring previous current release" >&2
exit 1
'@
$encodedRemoteScript = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($remoteScript.Replace("`r", "")))
$encodedRemoteScript | ssh $HostName "base64 --decode --ignore-garbage | bash -s -- '$RemoteRoot' '$releaseName' '$remoteArchive' '$RemoteRuntimeRoot' '$RemoteEnvPath' '$ServiceName' '$ServiceUser' '$HealthUrl' '$marketHubCommit' '$quoteMuxCommit' '$quoteMuxPackagesCommit' '$PrivilegedMigrationMode' '$PrivilegedMigrationEnvPath' '$CaptureDrainTimeoutSeconds' '$CaptureDrainRetrySeconds'"
if ($LASTEXITCODE -ne 0) {
    throw "远端发布失败"
}
# 服务重启后允许短暂启动窗口，健康检查固定在远端执行，避免本机解析或转发时序造成误报。
Invoke-NativeCommand -FilePath "ssh" -Arguments @($HostName, "curl -fsS --retry 20 --retry-delay 2 --retry-connrefused '$HealthUrl'")
Write-Output "部署完成: $RemoteRoot/releases/$releaseName"
