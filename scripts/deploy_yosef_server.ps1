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

$marketHubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = Split-Path $marketHubRoot -Parent
$quoteMuxRoot = Join-Path $workspaceRoot "QuoteMux"
foreach ($path in @($marketHubRoot, $quoteMuxRoot)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "缺少部署目录: $path"
    }
}

$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$deployTempRoot = Join-Path $workspaceRoot "_deploy_tmp"
New-Item -ItemType Directory -Force -Path $deployTempRoot | Out-Null
$archivePath = Join-Path $deployTempRoot "$releaseName.tgz"
Invoke-NativeCommand -FilePath "tar.exe" -Arguments @(
    "-czf", $archivePath,
    "--exclude=.git", "--exclude=.pytest_cache", "--exclude=__pycache__",
    "--exclude=.venv", "--exclude=build", "--exclude=*.egg-info",
    "--exclude=runtime", "--exclude=scratch", "--exclude=tests",
    "-C", $workspaceRoot, "MarketHub", "QuoteMux"
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

if [ ! -f "$env_path" ]; then
    echo "缺少远端环境文件: $env_path" >&2
    exit 2
fi
mkdir -p "$release_root" "$runtime_root"
tar -xzf "$remote_archive" -C "$release_root"
if [ ! -x "$runtime_root/.venv/bin/python" ]; then
    python3 -m venv "$runtime_root/.venv"
fi
"$runtime_root/.venv/bin/python" -m pip install --upgrade pip
"$runtime_root/.venv/bin/python" -m pip install -e "$release_root/QuoteMux"
"$runtime_root/.venv/bin/python" -m pip install -r "$release_root/MarketHub/requirements.txt"
set -a
. "$env_path"
set +a
export MARKETHUB_RUNTIME_ROOT="$runtime_root"
export MARKETHUB_ENV_PATH="$env_path"
export PYTHONPATH="$release_root/QuoteMux/src:$release_root/MarketHub/services/markethub_api/src"
unset QUOTEMUX_PACKAGE_REPO_SPEC
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/install_all_packages.py"
"$runtime_root/.venv/bin/python" "$release_root/MarketHub/scripts/bootstrap_database.py"
ln -sfn "$release_root" "$remote_root/current.next"
mv -Tf "$remote_root/current.next" "$remote_root/current"
mkdir -p "$runtime_root/scripts"
install -m 0755 "$remote_root/current/MarketHub/dailyupdate/global-data-update.sh" "$runtime_root/scripts/global-data-update.sh"
install -m 0755 "$remote_root/current/MarketHub/dailyupdate/global-data-update-with-health.sh" "$runtime_root/scripts/global-data-update-with-health.sh"
install -m 0755 "$remote_root/current/MarketHub/scripts/data-health-check.sh" "$runtime_root/scripts/data-health-check.sh"
cat >/tmp/markethub-service <<UNIT
[Unit]
Description=MarketHub API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$remote_root/current/MarketHub/services/markethub_api
EnvironmentFile=$env_path
Environment=MARKETHUB_RUNTIME_ROOT=$runtime_root
Environment=PYTHONPATH=$remote_root/current/QuoteMux/src:$remote_root/current/MarketHub/services/markethub_api/src
ExecStart=$runtime_root/.venv/bin/python $remote_root/current/MarketHub/services/markethub_api/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo -n install -m 0644 /tmp/markethub-service "/etc/systemd/system/$service_name.service"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now "$service_name.service"
rm -f "$remote_archive" /tmp/markethub-service
'@
$remoteScript | ssh $HostName bash -s -- $RemoteRoot $releaseName $remoteArchive $RemoteRuntimeRoot $RemoteEnvPath $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "远端发布失败"
}
Invoke-NativeCommand -FilePath "ssh" -Arguments @($HostName, ("curl -fsS --retry 20 --retry-delay 2 --retry-connrefused '" + $HealthUrl + "'"))
Write-Output "部署完成: $RemoteRoot/releases/$releaseName"