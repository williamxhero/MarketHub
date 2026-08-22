param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$RemoteRoot,
    [Parameter(Mandatory = $true)][string]$RemoteRuntimeRoot,
    [Parameter(Mandatory = $true)][string]$RemoteEnvPath,
    [Parameter(Mandatory = $true)][string]$ServiceName,
    [Parameter(Mandatory = $true)][string]$HealthUrl
)

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param([Parameter(Mandatory = $true)][string]$FilePath, [Parameter(Mandatory = $true)][string[]]$Arguments)

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $FilePath $($Arguments -join ' ')"
    }
}

$marketHubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$workspaceRoot = Split-Path $marketHubRoot -Parent
$quoteMuxRoot = Join-Path $workspaceRoot "QuoteMux"
$quoteMuxPackagesRoot = Join-Path $workspaceRoot "QuoteMux_Packages"
foreach ($path in @($marketHubRoot, $quoteMuxRoot, $quoteMuxPackagesRoot)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "缺少部署目录: $path"
    }
}

$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) "$releaseName.tgz"
Invoke-NativeCommand -FilePath "tar.exe" -Arguments @(
    "-czf", $archivePath,
    "--exclude=.git", "--exclude=.pytest_cache", "--exclude=__pycache__",
    "--exclude=.venv", "--exclude=build", "--exclude=*.egg-info", "--exclude=quotemux_packages.egg-info",
    "--exclude=runtime", "--exclude=.runtime", "--exclude=.tmp", "--exclude=scratch", "--exclude=tests",
    "-C", $workspaceRoot, "MarketHub", "QuoteMux", "QuoteMux_Packages"
)

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
release_root="$remote_root/releases/$release_name"
restart_on_exit() {
  sudo -n systemctl restart "$service_name.service" >/dev/null 2>&1 || true
}
trap restart_on_exit EXIT
if ! sudo -n systemctl stop "$service_name.service" >/dev/null 2>&1; then
  # A source worker can keep Uvicorn's background task alive past TimeoutStopSec.
  # systemd may already have killed it; make the release handoff deterministic
  # instead of abandoning the deployment after a timed-out graceful stop.
  sudo -n systemctl kill --kill-who=all --signal=KILL "$service_name.service" >/dev/null 2>&1 || true
  sudo -n systemctl reset-failed "$service_name.service" >/dev/null 2>&1 || true
fi

test -f "$env_path"
mkdir -p "$release_root" "$runtime_root"
tar --no-same-owner -xzf "$remote_archive" -C "$release_root"
rm -rf "$release_root/QuoteMux_Packages/quotemux_packages.egg-info" "$release_root/QuoteMux_Packages/build"
chown -R yosef:yosef "$release_root"
test -x "$runtime_root/.venv/bin/python" || python3 -m venv "$runtime_root/.venv"
"$runtime_root/.venv/bin/python" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python" -m pip install -e "$release_root/QuoteMux"
export QUOTEMUX_PACKAGE_REPO_SPEC="$release_root/QuoteMux_Packages"
"$runtime_root/.venv/bin/python" -m pip install -r "$release_root/MarketHub/requirements.txt"
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
# 某些构建后端会在源码包目录重新生成 egg-info；发布产物不允许带入 root-owned 构建目录。
rm -rf "$release_root/QuoteMux_Packages/quotemux_packages.egg-info"
rm -rf "$release_root/QuoteMux_Packages/build"
rm -rf "$release_root/QuoteMux/src/quotemux.egg-info"
rm -rf "$release_root/QuoteMux/build"
chown -R yosef:yosef "$release_root"
ln -sfn "$release_root" "$remote_root/current.next"
mv -Tf "$remote_root/current.next" "$remote_root/current"
mkdir -p "$runtime_root/scripts"
install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/global-data-update.sh" "$runtime_root/scripts/global-data-update.sh"
install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/global-data-update-with-health.sh" "$runtime_root/scripts/global-data-update-with-health.sh"
install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/data-health-check.sh" "$runtime_root/scripts/data-health-check.sh"
install -m 0755 "$remote_root/current/MarketHub/scripts/dailyupdate/update-futures-1m.sh" "$runtime_root/scripts/update-futures-1m.sh"
install -m 0755 "$remote_root/current/MarketHub/scripts/maintenance/manage_formal_export_freeze.sh" "$runtime_root/scripts/manage-formal-export-freeze.sh"
install -m 0755 "$remote_root/current/MarketHub/migrations/storage_v2_20260823/cleanup_after_migration.sh" "$runtime_root/scripts/storage-v2-cleanup-after-migration.sh"
cat >/tmp/markethub-service <<UNIT
[Unit]
Description=MarketHub API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=yosef
Group=yosef
WorkingDirectory=$remote_root/current/MarketHub/services/markethub_api
EnvironmentFile=$env_path
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
trap - EXIT
rm -f "$remote_archive" /tmp/markethub-service
'@
$remoteScript.Replace("`r", "") | ssh $HostName bash -s -- $RemoteRoot $releaseName $remoteArchive $RemoteRuntimeRoot $RemoteEnvPath $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "远端发布失败"
}
# 服务重启后允许短暂启动窗口，健康检查固定在远端执行，避免本机解析或转发时序造成误报。
Invoke-NativeCommand -FilePath "ssh" -Arguments @($HostName, "curl -fsS --retry 20 --retry-delay 2 --retry-connrefused 'http://127.0.0.1:8803/api/health'")
Write-Output "部署完成: $RemoteRoot/releases/$releaseName"
