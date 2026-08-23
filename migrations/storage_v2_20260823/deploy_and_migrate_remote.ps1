param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$RemoteRoot = "",
    [string]$RemoteRuntimeRoot = "",
    [string]$RemoteEnvPath = "",
    [string]$ServiceName = "",
    [string]$HealthUrl = "",
    [Parameter(Mandatory = $true)][string]$ExpectedSourceStorageVersion,
    [Parameter(Mandatory = $true)][string]$TargetStorageVersion,
    [switch]$ConfirmRemoteDatabaseSpace,
    [switch]$InstallOrUpgradePrerequisites,
    [switch]$CleanupLegacy,
    [switch]$PruneOldReleases
)

$ErrorActionPreference = "Stop"
$implementation = Join-Path $PSScriptRoot "deploy_and_migrate_yosef.ps1"
& $implementation @PSBoundParameters
if ($LASTEXITCODE -ne 0) { throw "远端发布与迁移入口失败" }
