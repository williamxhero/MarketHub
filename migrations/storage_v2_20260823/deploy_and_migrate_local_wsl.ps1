param(
    [string]$Distribution = "",
    [string]$AppRoot = "",
    [string]$RuntimeRoot = "",
    [string]$EnvPath = "",
    [string]$ServiceName = "",
    [string]$HealthUrl = "",
    [switch]$ConfirmRemoteDatabaseSpace,
    [switch]$QuiesceConflictingDistributions,
    [switch]$InstallOrUpgradePrerequisites,
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
$running = (wsl.exe --list --running --quiet) -replace "`0", "" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if (-not $available) { throw "没有可用的 WSL distribution" }
if ($Distribution -and $Distribution -notin $available) { throw "WSL distribution 不存在: $Distribution" }
if ($Distribution) {
    $conflicts = @($running | Where-Object { $_ -ne $Distribution })
    if ($conflicts -and -not $QuiesceConflictingDistributions) {
        throw "还有其他 WSL 正在运行（$($conflicts -join ', ')）；若它们共享数据目录，请显式追加 -QuiesceConflictingDistributions"
    }
    if ($QuiesceConflictingDistributions) {
        foreach ($conflict in $conflicts) {
            & wsl.exe --terminate $conflict
            if ($LASTEXITCODE -ne 0) { throw "无法停止冲突 WSL: $conflict" }
        }
    }
}

$discoveryWindows = (Join-Path $packageRoot "discover_environment.py").Replace("\", "/")
$drive = $discoveryWindows.Substring(0, 1).ToLowerInvariant()
$discoveryLinux = "/mnt/$drive/" + $discoveryWindows.Substring(3)

function Invoke-WslDiscovery {
    param([Parameter(Mandatory = $true)][string]$Distro, [string]$OutputPath = "")
    $arguments = @("-d", $Distro, "-u", "root", "--", "python3", $discoveryLinux)
    if ($AppRoot) { $arguments += @("--app-root", $AppRoot) }
    if ($RuntimeRoot) { $arguments += @("--runtime-root", $RuntimeRoot) }
    if ($EnvPath) { $arguments += @("--env-path", $EnvPath) }
    if ($ServiceName) { $arguments += @("--service-name", $ServiceName) }
    if ($HealthUrl) { $arguments += @("--health-url", $HealthUrl) }
    if ($OutputPath) { $arguments += @("--output", $OutputPath) }
    $output = & wsl.exe @arguments
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return (($output -join "`n") -replace "`0", "") | ConvertFrom-Json } catch { return $null }
}

if (-not $Distribution) {
    $candidates = @($running)
    if (-not $candidates) {
        if ($available.Count -ne 1) {
            throw "有多个未运行 WSL，自动探测会启动其服务；请显式指定 -Distribution"
        }
        $candidates = @($available[0])
    }
    $probes = foreach ($candidate in $candidates) {
        $probe = Invoke-WslDiscovery -Distro $candidate
        if ($null -ne $probe) {
            $score = 0
            if ($probe.deployment.mode -eq "existing") { $score += 10 }
            if ($probe.database.reachable) { $score += 100 }
            if ($probe.database.local_cluster.running) { $score += 50 }
            if ($probe.deployment.service_active_state -eq "active") { $score += 20 }
            [pscustomobject]@{ Distribution = $candidate; Environment = $probe; Score = $score }
        }
    }
    if (-not $probes) { throw "所有 WSL distribution 的环境发现都失败" }
    $best = @($probes | Sort-Object Score -Descending)
    if ($best.Count -gt 1) {
        throw "多个 WSL 正在运行，请显式指定 -Distribution；共享数据目录时同时追加 -QuiesceConflictingDistributions"
    }
    $Distribution = $best[0].Distribution
    $environment = $best[0].Environment
} else {
    $environment = Invoke-WslDiscovery -Distro $Distribution
    if ($null -eq $environment) { throw "WSL 环境发现失败；尚未开始迁移" }
}

$AppRoot = [string]$environment.deployment.app_root
$RuntimeRoot = [string]$environment.deployment.runtime_root
$EnvPath = [string]$environment.deployment.env_path
$ServiceName = [string]$environment.deployment.service_name
$HealthUrl = [string]$environment.deployment.health_url
$ServiceUser = [string]$environment.deployment.service_user
$detectedStorage = [string]$environment.storage.detected_version
$installedTimescale = [string]$environment.database.installed_timescaledb_version
if ($InstallOrUpgradePrerequisites -and $environment.database.local_cluster -and (
    -not $installedTimescale -or [version]$installedTimescale -lt [version]$manifest.minimum_timescaledb_version
)) {
    $prerequisiteWindows = (Join-Path $packageRoot "ensure_prerequisites_linux.sh").Replace("\", "/")
    $drive = $prerequisiteWindows.Substring(0, 1).ToLowerInvariant()
    $prerequisiteLinux = "/mnt/$drive/" + $prerequisiteWindows.Substring(3)
    & wsl.exe -d $Distribution -u root -- bash $prerequisiteLinux --apply $environment.database.local_cluster.major $manifest.minimum_timescaledb_version
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL/TimescaleDB 前置依赖升级失败" }
    $environment = Invoke-WslDiscovery -Distro $Distribution
    if ($null -eq $environment) { throw "前置依赖升级后的环境复检失败" }
    $installedTimescale = [string]$environment.database.installed_timescaledb_version
}
$allowedStorageStates = @($manifest.source_storage_version, $manifest.target_storage_version, "intermediate-resumable", "fresh")
if ($environment.deployment.mode -eq "existing" -and $detectedStorage -eq "unknown") {
    throw "已发现现有本机部署，但无法只读检查数据库；尚未开始迁移"
}
if ($detectedStorage -notin $allowedStorageStates -and $detectedStorage -ne "unknown") { throw "不支持的本机存储状态: $detectedStorage" }
if ($environment.filesystem.migration_volume_verified -and [uint64]$environment.storage.minimum_free_bytes_for_migration -gt [uint64]$environment.filesystem.free_bytes) {
    throw "本机空间不足：迁移至少需要 $($environment.storage.minimum_free_bytes_for_migration) bytes，可用 $($environment.filesystem.free_bytes) bytes"
}
if (-not $environment.filesystem.migration_volume_verified -and [uint64]$environment.storage.ordinary_relation_bytes -gt 0 -and -not $ConfirmRemoteDatabaseSpace) {
    throw "数据库数据目录不在本 WSL 中，无法自动核对 shadow 空间；核对数据库主机后显式传入 -ConfirmRemoteDatabaseSpace"
}
if ($environment.database.reachable) {
    if ([int]$environment.database.postgresql_major -notin @($manifest.supported_postgresql_majors)) {
        throw "不支持的 PostgreSQL 主版本: $($environment.database.postgresql_major)"
    }
    if (-not $environment.database.timescaledb_version) { throw "目标数据库未安装 TimescaleDB" }
    if ([version]$environment.database.timescaledb_version -lt [version]$manifest.minimum_timescaledb_version) {
        throw "TimescaleDB 版本过低: $($environment.database.timescaledb_version)"
    }
}
elseif ($environment.database.installed_timescaledb_version -and [version]$environment.database.installed_timescaledb_version -lt [version]$manifest.minimum_timescaledb_version) {
    throw "已安装 TimescaleDB 版本过低: $($environment.database.installed_timescaledb_version)"
}
if (-not $environment.database.reachable -and -not $environment.database.local_cluster) {
    throw "未发现可用 PostgreSQL cluster；请先安装 manifest 支持的 PostgreSQL/TimescaleDB，尚未开始发布"
}
if (-not $environment.database.reachable -and $environment.database.local_cluster -and -not $environment.database.installed_timescaledb_version) {
    throw "已发现 PostgreSQL cluster，但没有对应 TimescaleDB 安装；尚未开始发布"
}

$evidenceRoot = "$RuntimeRoot/migrations/$($manifest.migration_id)"
$preflightPath = "$evidenceRoot/preflight-local.json"
$environment = Invoke-WslDiscovery -Distro $Distribution -OutputPath $preflightPath
if ($null -eq $environment) { throw "无法保存本机迁移前环境证据；尚未开始迁移" }
Write-Output "迁移前环境已确认: distro=$Distribution, mode=$($environment.deployment.mode), storage=$detectedStorage, root=$AppRoot, runtime=$RuntimeRoot, service=$ServiceName, user=$ServiceUser"

$releaseName = "deploy_$(Get-Date -Format 'yyyyMMdd_HHmmss')_storage_v2"
$archiveWindows = Join-Path ([System.IO.Path]::GetTempPath()) "$releaseName.tgz"
& tar.exe -czf $archiveWindows `
    --exclude=.git --exclude=.pytest_cache --exclude=__pycache__ --exclude=.venv `
    --exclude=build --exclude=*.egg-info --exclude=runtime --exclude=.runtime `
    --exclude=.tmp --exclude=scratch --exclude=tests `
    -C $workspaceRoot MarketHub QuoteMux QuoteMux_Packages
if ($LASTEXITCODE -ne 0) { throw "创建本机 release 失败" }
$archiveFullPath = [System.IO.Path]::GetFullPath($archiveWindows)
if ($archiveFullPath -notmatch '^[A-Za-z]:\\') { throw "本机 release 必须位于 WSL 可映射的 Windows 盘符路径: $archiveFullPath" }
$archiveDrive = $archiveFullPath.Substring(0, 1).ToLowerInvariant()
$archiveLinux = "/mnt/$archiveDrive/" + $archiveFullPath.Substring(3).Replace("\", "/")

$installerWindows = (Join-Path $packageRoot "install_release_linux.sh").Replace("\", "/")
$drive = $installerWindows.Substring(0, 1).ToLowerInvariant()
$installerLinux = "/mnt/$drive/" + $installerWindows.Substring(3)
$statusLinux = "$evidenceRoot/install-$releaseName.status"
$installerEnvironment = @(
    "MARKETHUB_SERVICE_USER=$ServiceUser",
    "MARKETHUB_PACKAGE_VENV_ROOT=$($environment.deployment.package_venv_root)"
)
if ($environment.database.postgresql_major) { $installerEnvironment += "MARKETHUB_POSTGRES_MAJOR=$($environment.database.postgresql_major)" }
if ($environment.database.data_directory) { $installerEnvironment += "MARKETHUB_POSTGRES_DATA=$($environment.database.data_directory)" }
& wsl.exe -d $Distribution -u root -- env @installerEnvironment bash $installerLinux `
    $archiveLinux $releaseName $AppRoot $RuntimeRoot $EnvPath $ServiceName $HealthUrl
$installerExit = $LASTEXITCODE
$status = ""
for ($attempt = 0; $attempt -lt 720; $attempt++) {
    $status = (& wsl.exe -d $Distribution -u root -- bash -lc "test -f '$statusLinux' && cat '$statusLinux' || true") -replace "`0", ""
    $status = $status.Trim()
    if ($status -eq "success" -or $status.StartsWith("failed:")) { break }
    Start-Sleep -Seconds 10
}
if ($status -ne "success") {
    throw "本机 WSL 发布或迁移失败（wsl=$installerExit, status=$status）；必须修复版本化脚本后从本入口重跑"
}

try { Enable-ScheduledTask -TaskName "MarketHub WSL KeepAlive" -ErrorAction Stop | Out-Null } catch { }
$health = & curl.exe --noproxy "*" --fail --silent --show-error $HealthUrl
if ($LASTEXITCODE -ne 0) { throw "Windows 侧健康检查失败: $HealthUrl" }
Write-Output $health
Write-Output "本机完整发布与迁移完成: $releaseName / $($manifest.target_storage_version)"
