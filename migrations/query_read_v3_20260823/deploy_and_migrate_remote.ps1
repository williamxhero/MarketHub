param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$RemoteRoot = "",
    [string]$RemoteRuntimeRoot = "",
    [string]$RemoteEnvPath = "",
    [string]$ServiceName = "",
    [string]$HealthUrl = "",
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
$marketHubRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$discover = Join-Path $marketHubRoot "migrations\storage_v2_20260823\discover_environment.py"
$remoteDiscover = "/tmp/markethub-query-read-v3-discover.py"
& scp $discover ($HostName + ':' + $remoteDiscover)
if ($LASTEXITCODE -ne 0) { throw "上传环境发现器失败" }
$arguments = @($remoteDiscover)
if ($RemoteRoot) { $arguments += @("--app-root", $RemoteRoot) }
if ($RemoteRuntimeRoot) { $arguments += @("--runtime-root", $RemoteRuntimeRoot) }
if ($RemoteEnvPath) { $arguments += @("--env-path", $RemoteEnvPath) }
if ($ServiceName) { $arguments += @("--service-name", $ServiceName) }
if ($HealthUrl) { $arguments += @("--health-url", $HealthUrl) }
$discoveryJson = & ssh $HostName python3 @arguments
if ($LASTEXITCODE -ne 0) { throw "目标环境发现失败" }
$discovery = $discoveryJson | ConvertFrom-Json
$evidenceRoot = Join-Path $PSScriptRoot "evidence"
New-Item -ItemType Directory -Force -Path $evidenceRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$discoveryJson | Set-Content -Encoding utf8 (Join-Path $evidenceRoot "preflight_$stamp.json")
if ($PreflightOnly) { $discoveryJson; exit 0 }

$deployment = $discovery.deployment
foreach ($name in @("app_root", "runtime_root", "env_path", "service_name", "health_url", "service_user")) {
    if ([string]::IsNullOrWhiteSpace([string]$deployment.$name)) { throw "发现结果缺少 $name" }
}
$freezeOwner = "query-read-v3-$stamp"
$freezeTool = "$($deployment.runtime_root)/scripts/manage-formal-export-freeze.sh"
& ssh $HostName "test -x '$freezeTool' && '$freezeTool' acquire '$freezeOwner'"
if ($LASTEXITCODE -ne 0) { throw "无法通过正式工具获取更新 freeze" }
$migrationSucceeded = $false
try {
    & (Join-Path $marketHubRoot "scripts\local\deploy_yosef_server.ps1") `
      -HostName $HostName -RemoteRoot $deployment.app_root -RemoteRuntimeRoot $deployment.runtime_root `
      -RemoteEnvPath $deployment.env_path -ServiceName $deployment.service_name -HealthUrl $deployment.health_url `
      -ServiceUser $deployment.service_user
    if ($LASTEXITCODE -ne 0) { throw "正式 release 部署失败" }
    $remoteCommand = "set -euo pipefail; set -a; . '$($deployment.env_path)'; set +a; export PYTHONPATH='$($deployment.app_root)/current/QuoteMux/src:$($deployment.app_root)/current/MarketHub/services/markethub_api/src'; '$($deployment.runtime_root)/.venv/bin/python' '$($deployment.app_root)/current/MarketHub/migrations/query_read_v3_20260823/release_migration.py' apply --output '$($deployment.runtime_root)/query-read-v3-apply.json'"
    & ssh $HostName $remoteCommand
    if ($LASTEXITCODE -ne 0) { throw "query-read-v3 apply/verify 失败" }
    & ssh $HostName "curl -fsS '$($deployment.health_url)'"
    if ($LASTEXITCODE -ne 0) { throw "迁移后 API health 失败" }
    $migrationSucceeded = $true
} finally {
    if ($migrationSucceeded) {
        & ssh $HostName "'$freezeTool' restore '$freezeOwner'"
        if ($LASTEXITCODE -ne 0) { Write-Warning "freeze restore 失败，必须在目标机修复后再放行更新任务" }
    } else {
        Write-Warning "迁移未完成；正式 freeze $freezeOwner 保持启用。修复迁移脚本并重试成功后再通过正式工具 restore。"
    }
}
