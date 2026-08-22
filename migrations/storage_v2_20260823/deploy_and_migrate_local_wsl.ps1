param(
    [string]$Distribution = "Ubuntu",
    [string]$DataRoot = "E:\ubuntu_data",
    [string]$AppRoot = "/data/MarketHub2",
    [string]$RuntimeRoot = "/data/markethub",
    [string]$EnvPath = "/data/markethub/env/markethub.env",
    [string]$ServiceName = "markethub-api",
    [string]$HealthUrl = "http://127.0.0.1:8803/api/health",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceStorageVersion,
    [Parameter(Mandatory = $true)][string]$TargetStorageVersion
)

$ErrorActionPreference = "Stop"
$packageRoot = Split-Path $MyInvocation.MyCommand.Path -Parent
$marketHubRoot = (Resolve-Path (Join-Path $packageRoot "..\..")).Path
$workspaceRoot = Split-Path $marketHubRoot -Parent
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot "manifest.json") -Raw | ConvertFrom-Json

if ($ExpectedSourceStorageVersion -ne $manifest.source_storage_version) {
    throw "源版本不匹配。脚本要求 $($manifest.source_storage_version)"
}
if ($TargetStorageVersion -ne $manifest.target_storage_version) {
    throw "目标版本不匹配。脚本要求 $($manifest.target_storage_version)"
}
foreach ($path in @($marketHubRoot, (Join-Path $workspaceRoot "QuoteMux"), (Join-Path $workspaceRoot "QuoteMux_Packages"))) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "缺少部署目录: $path" }
}

$available = (wsl.exe --list --quiet) -replace "`0", "" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($Distribution -notin $available) { throw "WSL distribution 不存在: $Distribution" }
if ($Distribution -eq "Ubuntu" -and "MarketHubUbuntu" -in $available) {
    wsl.exe --terminate MarketHubUbuntu 2>$null
}

$inbox = Join-Path $DataRoot "inbox"
New-Item -ItemType Directory -Force -Path $inbox | Out-Null
$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')_storage_v2"
$archiveWindows = Join-Path $inbox "$releaseName.tgz"
& tar.exe -czf $archiveWindows `
    --exclude=.git --exclude=.pytest_cache --exclude=__pycache__ --exclude=.venv `
    --exclude=build --exclude=*.egg-info --exclude=runtime --exclude=.runtime `
    --exclude=.tmp --exclude=scratch --exclude=tests `
    -C $workspaceRoot MarketHub QuoteMux QuoteMux_Packages
if ($LASTEXITCODE -ne 0) { throw "创建本机 release 失败" }

$archiveLinux = "/data/inbox/$releaseName.tgz"
$installerWindows = (Join-Path $packageRoot "install_release_linux.sh").Replace("\", "/")
$drive = $installerWindows.Substring(0, 1).ToLowerInvariant()
$installerLinux = "/mnt/$drive/" + $installerWindows.Substring(3)

& wsl.exe -d $Distribution -u root -- bash $installerLinux `
    $archiveLinux $releaseName $AppRoot $RuntimeRoot $EnvPath $ServiceName $HealthUrl
if ($LASTEXITCODE -ne 0) {
    throw "本机 WSL 发布或迁移失败；必须修复版本化脚本后从本入口重跑"
}

try { Enable-ScheduledTask -TaskName "MarketHub WSL KeepAlive" -ErrorAction Stop | Out-Null } catch { }
$health = & curl.exe --noproxy "*" --fail --silent --show-error $HealthUrl
if ($LASTEXITCODE -ne 0) { throw "Windows 侧健康检查失败: $HealthUrl" }
Write-Output $health
Write-Output "本机完整发布与迁移完成: $releaseName / $($manifest.target_storage_version)"
