param(
    [string]$HostName = "yosef-server",
    [string]$RemoteRoot = "/data/MarketHub2",
    [string]$RemoteRuntimeRoot = "/data/markethub",
    [string]$RemoteEnvPath = "/data/markethub/env/markethub.env",
    [string]$ServiceName = "markethub-api",
    [string]$HealthUrl = "http://127.0.0.1:8803/api/health",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceStorageVersion,
    [Parameter(Mandatory = $true)][string]$TargetStorageVersion,
    [switch]$CleanupLegacy,
    [switch]$PruneOldReleases
)

$ErrorActionPreference = "Stop"
$packageRoot = Split-Path $MyInvocation.MyCommand.Path -Parent
$marketHubRoot = (Resolve-Path (Join-Path $packageRoot "..\..")).Path
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot "manifest.json") -Raw | ConvertFrom-Json

if ($ExpectedSourceStorageVersion -ne $manifest.source_storage_version) {
    throw "源版本不匹配。脚本要求 $($manifest.source_storage_version)，收到 $ExpectedSourceStorageVersion"
}
if ($TargetStorageVersion -ne $manifest.target_storage_version) {
    throw "目标版本不匹配。脚本要求 $($manifest.target_storage_version)，收到 $TargetStorageVersion"
}

$deployScript = Join-Path $marketHubRoot "scripts\local\deploy_yosef_server.ps1"
& $deployScript `
    -HostName $HostName `
    -RemoteRoot $RemoteRoot `
    -RemoteRuntimeRoot $RemoteRuntimeRoot `
    -RemoteEnvPath $RemoteEnvPath `
    -ServiceName $ServiceName `
    -HealthUrl $HealthUrl
if ($LASTEXITCODE -ne 0) {
    throw "发布脚本失败"
}

$migrationRoot = "$RemoteRoot/current/MarketHub/migrations/storage_v2_20260823"
$evidenceRoot = "$RemoteRuntimeRoot/migrations/$($manifest.migration_id)"
$remoteApply = @"
set -Eeuo pipefail
mkdir -p '$evidenceRoot'
'$RemoteRuntimeRoot/.venv/bin/python' '$migrationRoot/release_migration.py' --env-file '$RemoteEnvPath' --output '$evidenceRoot/apply.json' apply --service-name '$ServiceName'
'$RemoteRuntimeRoot/.venv/bin/python' '$migrationRoot/release_migration.py' --env-file '$RemoteEnvPath' --output '$evidenceRoot/verify.json' verify
curl -fsS --retry 20 --retry-delay 2 --retry-connrefused '$HealthUrl'
"@
$remoteApply.Replace("`r", "") | ssh $HostName bash -s
if ($LASTEXITCODE -ne 0) {
    throw "版本化迁移失败；修复迁移包后重新运行本脚本，禁止手工绕过"
}

if ($CleanupLegacy) {
    $remoteCleanup = @"
set -Eeuo pipefail
MARKETHUB_ROOT='$RemoteRoot' MARKETHUB_RUNTIME_ROOT='$RemoteRuntimeRoot' MARKETHUB_STORAGE_KEEP_RELEASES=$(if ($PruneOldReleases) { '1' } else { '5' }) \
  '$migrationRoot/cleanup_after_migration.sh' --apply --confirm-target-version '$TargetStorageVersion'
curl -fsS '$HealthUrl'
"@
    $remoteCleanup.Replace("`r", "") | ssh $HostName bash -s
    if ($LASTEXITCODE -ne 0) {
        throw "迁移后清理失败；数据库迁移仍保留成功状态，可修复脚本后安全重跑"
    }
}

Write-Output "发布与迁移完成: $($manifest.source_storage_version) -> $($manifest.target_storage_version)"
