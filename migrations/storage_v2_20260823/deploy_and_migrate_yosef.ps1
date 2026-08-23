param(
    [string]$HostName = "yosef-server",
    [string]$RemoteRoot = "",
    [string]$RemoteRuntimeRoot = "",
    [string]$RemoteEnvPath = "",
    [string]$ServiceName = "",
    [string]$HealthUrl = "",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceStorageVersion,
    [Parameter(Mandatory = $true)][string]$TargetStorageVersion,
    [switch]$ConfirmRemoteDatabaseSpace,
    [switch]$InstallOrUpgradePrerequisites,
    [switch]$PreflightOnly,
    [switch]$CleanupLegacy,
    [switch]$PruneOldReleases
)

$ErrorActionPreference = "Stop"
$packageRoot = Split-Path $MyInvocation.MyCommand.Path -Parent
$marketHubRoot = (Resolve-Path (Join-Path $packageRoot "..\..")).Path
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot "manifest.json") -Raw | ConvertFrom-Json

function Invoke-RemoteBash {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [string]$RemoteCommand = "bash -s"
    )
    # PowerShell adds CRLF when it serializes a string to native stdin. Keep
    # that transport newline outside the decoded Bash program.
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Script.Replace("`r", "")))
    $encoded | ssh $HostName "base64 --decode --ignore-garbage | $RemoteCommand"
}

if ($ExpectedSourceStorageVersion -ne $manifest.source_storage_version) {
    throw "源版本不匹配。脚本要求 $($manifest.source_storage_version)，收到 $ExpectedSourceStorageVersion"
}
if ($TargetStorageVersion -ne $manifest.target_storage_version) {
    throw "目标版本不匹配。脚本要求 $($manifest.target_storage_version)，收到 $TargetStorageVersion"
}

$discoveryScript = Join-Path $packageRoot "discover_environment.py"
$remoteDiscovery = "/tmp/markethub-storage-v2-discover-$PID.py"
& scp $discoveryScript ($HostName + ':' + $remoteDiscovery)
if ($LASTEXITCODE -ne 0) { throw "无法上传只读环境发现脚本" }
$discoveryArguments = @($HostName, "python3", $remoteDiscovery)
if ($RemoteRoot) { $discoveryArguments += @("--app-root", $RemoteRoot) }
if ($RemoteRuntimeRoot) { $discoveryArguments += @("--runtime-root", $RemoteRuntimeRoot) }
if ($RemoteEnvPath) { $discoveryArguments += @("--env-path", $RemoteEnvPath) }
if ($ServiceName) { $discoveryArguments += @("--service-name", $ServiceName) }
if ($HealthUrl) { $discoveryArguments += @("--health-url", $HealthUrl) }
$discoveryOutput = & ssh @discoveryArguments
$discoveryExit = $LASTEXITCODE
& ssh $HostName "rm -f '$remoteDiscovery'" | Out-Null
if ($discoveryExit -ne 0) { throw "远端环境发现失败；尚未开始部署或迁移" }
$preflightJson = ($discoveryOutput -join "`n")
$environment = $preflightJson | ConvertFrom-Json

$RemoteRoot = [string]$environment.deployment.app_root
$RemoteRuntimeRoot = [string]$environment.deployment.runtime_root
$RemoteEnvPath = [string]$environment.deployment.env_path
$ServiceName = [string]$environment.deployment.service_name
$HealthUrl = [string]$environment.deployment.health_url
$ServiceUser = [string]$environment.deployment.service_user
$detectedStorage = [string]$environment.storage.detected_version
$installedTimescale = [string]$environment.database.installed_timescaledb_version
if ($InstallOrUpgradePrerequisites -and $environment.database.local_cluster -and (
    -not $installedTimescale -or [version]$installedTimescale -lt [version]$manifest.minimum_timescaledb_version
)) {
    $prerequisiteScript = Join-Path $packageRoot "ensure_prerequisites_linux.sh"
    Invoke-RemoteBash `
        -Script (Get-Content -Raw $prerequisiteScript) `
        -RemoteCommand "sudo -n bash -s -- --apply $($environment.database.local_cluster.major) $($manifest.minimum_timescaledb_version)"
    if ($LASTEXITCODE -ne 0) { throw "远端 PostgreSQL/TimescaleDB 前置依赖升级失败" }
    & scp $discoveryScript ($HostName + ':' + $remoteDiscovery)
    if ($LASTEXITCODE -ne 0) { throw "无法重新上传环境发现脚本" }
    $discoveryOutput = & ssh @discoveryArguments
    $discoveryExit = $LASTEXITCODE
    & ssh $HostName "rm -f '$remoteDiscovery'" | Out-Null
    if ($discoveryExit -ne 0) { throw "前置依赖升级后的远端环境复检失败" }
    $preflightJson = ($discoveryOutput -join "`n")
    $environment = $preflightJson | ConvertFrom-Json
    $RemoteRoot = [string]$environment.deployment.app_root
    $RemoteRuntimeRoot = [string]$environment.deployment.runtime_root
    $RemoteEnvPath = [string]$environment.deployment.env_path
    $ServiceName = [string]$environment.deployment.service_name
    $HealthUrl = [string]$environment.deployment.health_url
    $ServiceUser = [string]$environment.deployment.service_user
    $detectedStorage = [string]$environment.storage.detected_version
    $installedTimescale = [string]$environment.database.installed_timescaledb_version
}
$allowedStorageStates = @(
    $manifest.source_storage_version,
    $manifest.target_storage_version,
    "intermediate-resumable",
    "fresh"
)
if ($environment.deployment.mode -eq "existing" -and $detectedStorage -eq "unknown") {
    throw "已发现现有部署，但无法只读检查数据库；尚未开始部署或迁移"
}
if ($detectedStorage -notin $allowedStorageStates -and $detectedStorage -ne "unknown") {
    throw "不支持的存储状态: $detectedStorage"
}
if ($environment.filesystem.migration_volume_verified -and [uint64]$environment.storage.minimum_free_bytes_for_migration -gt [uint64]$environment.filesystem.free_bytes) {
    throw "远端空间不足：迁移至少需要 $($environment.storage.minimum_free_bytes_for_migration) bytes，可用 $($environment.filesystem.free_bytes) bytes"
}
if (-not $environment.filesystem.migration_volume_verified -and [uint64]$environment.storage.ordinary_relation_bytes -gt 0 -and -not $ConfirmRemoteDatabaseSpace) {
    throw "数据库数据目录不在应用主机上，无法自动核对 shadow 空间；请人工核对数据库主机后显式传入 -ConfirmRemoteDatabaseSpace"
}
if ($environment.database.reachable) {
    if ([int]$environment.database.postgresql_major -notin @($manifest.supported_postgresql_majors)) {
        throw "不支持的 PostgreSQL 主版本: $($environment.database.postgresql_major)"
    }
    if (-not $environment.database.timescaledb_version) {
        throw "目标数据库未安装 TimescaleDB；尚未开始部署或迁移"
    }
    if ([version]$environment.database.timescaledb_version -lt [version]$manifest.minimum_timescaledb_version) {
        throw "TimescaleDB 版本过低: $($environment.database.timescaledb_version)，最低要求 $($manifest.minimum_timescaledb_version)"
    }
}
elseif ($environment.database.installed_timescaledb_version -and [version]$environment.database.installed_timescaledb_version -lt [version]$manifest.minimum_timescaledb_version) {
    throw "已安装 TimescaleDB 版本过低: $($environment.database.installed_timescaledb_version)"
}
if (-not $environment.database.reachable -and -not $environment.database.local_cluster) {
    throw "未发现可用 PostgreSQL cluster 或可达外部数据库；请先完成数据库前置安装"
}
if (-not $environment.database.reachable -and $environment.database.local_cluster -and -not $environment.database.installed_timescaledb_version) {
    throw "已发现 PostgreSQL cluster，但没有对应 TimescaleDB 安装；尚未开始发布"
}
Write-Output "迁移前环境已确认: mode=$($environment.deployment.mode), storage=$detectedStorage, root=$RemoteRoot, runtime=$RemoteRuntimeRoot, service=$ServiceName, user=$ServiceUser"
$evidenceRoot = "$RemoteRuntimeRoot/migrations/$($manifest.migration_id)"
$savePreflight = @"
set -Eeuo pipefail
service_group=`$(id -gn '$ServiceUser')
sudo -n install -d -o '$ServiceUser' -g "`$service_group" '$evidenceRoot'
base64 --decode --ignore-garbage | sudo -n -u '$ServiceUser' tee '$evidenceRoot/preflight.json' >/dev/null
"@
$encodedPreflight = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($preflightJson))
$encodedPreflight | ssh $HostName $savePreflight
if ($LASTEXITCODE -ne 0) { throw "无法保存迁移前环境证据；尚未开始部署或迁移" }
if ($PreflightOnly) {
    Write-Output "只读环境发现与证据保存完成；未创建 release，未执行数据库迁移"
    return
}

$deployScript = Join-Path $marketHubRoot "scripts\local\deploy_yosef_server.ps1"
& $deployScript `
    -HostName $HostName `
    -RemoteRoot $RemoteRoot `
    -RemoteRuntimeRoot $RemoteRuntimeRoot `
    -RemoteEnvPath $RemoteEnvPath `
    -ServiceName $ServiceName `
    -HealthUrl $HealthUrl `
    -ServiceUser $ServiceUser
if ($LASTEXITCODE -ne 0) {
    throw "发布脚本失败"
}

$migrationRoot = "$RemoteRoot/current/MarketHub/migrations/storage_v2_20260823"
$remoteApply = @"
set -Eeuo pipefail
mkdir -p '$evidenceRoot'
'$RemoteRuntimeRoot/.venv/bin/python' '$migrationRoot/release_migration.py' --env-file '$RemoteEnvPath' --output '$evidenceRoot/inspect-before-apply.json' inspect
'$RemoteRuntimeRoot/.venv/bin/python' '$migrationRoot/release_migration.py' --env-file '$RemoteEnvPath' --output '$evidenceRoot/apply.json' apply --service-name '$ServiceName'
'$RemoteRuntimeRoot/.venv/bin/python' '$migrationRoot/release_migration.py' --env-file '$RemoteEnvPath' --output '$evidenceRoot/verify.json' verify
curl -fsS --retry 20 --retry-delay 2 --retry-connrefused '$HealthUrl'
"@
Invoke-RemoteBash -Script $remoteApply
if ($LASTEXITCODE -ne 0) {
    throw "版本化迁移失败；修复迁移包后重新运行本脚本，禁止手工绕过"
}

if ($CleanupLegacy) {
    $remoteCleanup = @"
set -Eeuo pipefail
MARKETHUB_ROOT='$RemoteRoot' MARKETHUB_RUNTIME_ROOT='$RemoteRuntimeRoot' MARKETHUB_SERVICE_NAME='$ServiceName' MARKETHUB_STORAGE_KEEP_RELEASES=$(if ($PruneOldReleases) { '1' } else { '5' }) \
  bash '$migrationRoot/cleanup_after_migration.sh' --apply --confirm-target-version '$TargetStorageVersion'
curl -fsS '$HealthUrl'
"@
    Invoke-RemoteBash -Script $remoteCleanup
    if ($LASTEXITCODE -ne 0) {
        throw "迁移后清理失败；数据库迁移仍保留成功状态，可修复脚本后安全重跑"
    }
}

Write-Output "发布与迁移完成: $($manifest.source_storage_version) -> $($manifest.target_storage_version)"
