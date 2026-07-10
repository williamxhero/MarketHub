param(
    [string]$HostName = "yosef-server",
    [string]$RemoteRoot = "/data/MarketHub2",
    [int]$ConceptWindowCount = 7
)

$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败: $FilePath $($Arguments -join ' ')"
    }
}

$marketHubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$workspaceRoot = Split-Path $marketHubRoot -Parent
$quoteMuxRoot = Join-Path $workspaceRoot "QuoteMux"
$packageRoot = Join-Path $workspaceRoot "QuoteMux_Packages"
foreach ($path in @($marketHubRoot, $quoteMuxRoot, $packageRoot)) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "缺少部署目录: $path"
    }
}

$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$deployTempRoot = Join-Path $workspaceRoot "_deploy_tmp"
New-Item -ItemType Directory -Force -Path $deployTempRoot | Out-Null
$archivePath = Join-Path $deployTempRoot "$releaseName.tgz"
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}

Invoke-NativeCommand -FilePath "tar.exe" -Arguments @(
    "-czf", $archivePath,
    "--exclude=.git",
    "--exclude=.pytest_cache",
    "--exclude=__pycache__",
    "--exclude=.venv",
    "--exclude=build",
    "--exclude=*.egg-info",
    "--exclude=runtime",
    "--exclude=scratch",
    "--exclude=tests",
    "-C", $workspaceRoot,
    "MarketHub", "QuoteMux", "QuoteMux_Packages"
)

$remoteArchive = "/tmp/$releaseName.tgz"
Invoke-NativeCommand -FilePath "scp" -Arguments @($archivePath, "$HostName`:$remoteArchive")

$remoteScript = @'
set -euo pipefail
remote_root="$1"
release_name="$2"
remote_archive="$3"
release_root="$remote_root/releases/$release_name"

mkdir -p "$release_root"
tar -xzf "$remote_archive" -C "$release_root"
ln -s /data/markethub/.venv "$release_root/.venv"
chmod +x "$release_root/MarketHub/scripts/global-data-update.sh"

set -a
. /data/markethub/env/markethub.env
set +a
export PYTHONPATH="$release_root/QuoteMux/src"
/data/markethub/.venv/bin/python "$release_root/MarketHub/scripts/migrate_market_daily_contracts.py"

ln -sfn "$release_root" "$remote_root/current.next"
mv -Tf "$remote_root/current.next" "$remote_root/current"
rm -f "$remote_archive"
'@
$remoteScript | ssh $HostName bash -s -- $RemoteRoot $releaseName $remoteArchive
if ($LASTEXITCODE -ne 0) {
    throw "远端发布目录切换失败"
}

$sudoPassword = $env:YOSEF_SUDO_PASSWORD
if ([string]::IsNullOrWhiteSpace($sudoPassword)) {
    throw "缺少 YOSEF_SUDO_PASSWORD，无法重启 markethub-api.service"
}
"$sudoPassword`n" | ssh $HostName "sudo -S systemctl restart markethub-api.service"
if ($LASTEXITCODE -ne 0) {
    throw "markethub-api.service 重启失败"
}

Invoke-NativeCommand -FilePath "ssh" -Arguments @(
    $HostName,
    "curl -fsS --retry 20 --retry-delay 2 --retry-connrefused http://127.0.0.1:8803/api/health"
)

Invoke-NativeCommand -FilePath "ssh" -Arguments @(
    $HostName,
    "set -a; . /data/markethub/env/markethub.env; set +a; export PYTHONPATH=$RemoteRoot/current/QuoteMux/src; /data/markethub/.venv/bin/python $RemoteRoot/current/MarketHub/scripts/backfill_concept_runtime_refs.py --quotes-only --window-count $ConceptWindowCount"
)

Invoke-NativeCommand -FilePath "ssh" -Arguments @(
    $HostName,
    "curl -fsS --max-time 1800 -X POST http://127.0.0.1:8803/api/admin/capture-runs/boards.quotes.daily"
)

Write-Output "部署完成: $RemoteRoot/releases/$releaseName"
