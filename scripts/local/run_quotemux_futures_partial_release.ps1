param(
    [Parameter(Mandatory = $true)][ValidateSet("deploy", "classify", "import", "partial-plan", "partial-publish", "verify")][string]$Action,
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$RemoteRoot = "/data/MarketHub2",
    [string]$RemoteRuntimeRoot = "/data/markethub",
    [string]$RemoteEnvPath = "/data/markethub/env/markethub.env",
    [string]$ServiceName = "markethub-api",
    [string]$HealthUrl = "http://127.0.0.1:8803/api/health",
    [string]$QuoteMuxSourceRoot = "",
    [string]$QuoteMuxPackagesSourceRoot = "",
    [string]$ReleaseRoot = "",
    [string]$BundlePath = "",
    [string]$ImportPlanPath = "",
    [string]$PartialPlanPath = "",
    [string]$QmiId = "",
    [string]$CatalogIdentity = "",
    [Nullable[int]]$ExpectedGeneration = $null,
    [ValidateSet("peer", "env")][string]$PrivilegedMigrationMode = "peer",
    [string]$PrivilegedEnvPath = "/data/markethub/env/quotemux-futures-partial-migration.env",
    [string]$PublisherEnvPath = "/data/markethub/env/quotemux-futures-partial-publisher.env",
    [ValidateRange(30, 1800)][int]$CaptureDrainTimeoutSeconds = 300,
    [ValidateRange(1, 60)][int]$CaptureDrainRetrySeconds = 10
)

$ErrorActionPreference = "Stop"

function Assert-RemoteToken {
    param([Parameter(Mandatory = $true)][string]$Value, [Parameter(Mandatory = $true)][string]$Name)
    if ($Value -notmatch '^[A-Za-z0-9_./:=-]+$') { throw "$Name contains unsupported shell characters" }
}

function Invoke-RemoteStage {
    param([Parameter(Mandatory = $true)][string]$Command)
    & ssh $HostName $Command
    if ($LASTEXITCODE -ne 0) { throw "远端 QuoteMux partial stage failed: $Action" }
}

if ($Action -eq "deploy") {
    $deploy = Join-Path $PSScriptRoot "deploy_yosef_server.ps1"
    & $deploy -HostName $HostName -RemoteRoot $RemoteRoot -RemoteRuntimeRoot $RemoteRuntimeRoot -RemoteEnvPath $RemoteEnvPath -ServiceName $ServiceName -HealthUrl $HealthUrl -QuoteMuxSourceRoot $QuoteMuxSourceRoot -QuoteMuxPackagesSourceRoot $QuoteMuxPackagesSourceRoot -PrivilegedMigrationMode $PrivilegedMigrationMode -PrivilegedMigrationEnvPath $PrivilegedEnvPath -CaptureDrainTimeoutSeconds $CaptureDrainTimeoutSeconds -CaptureDrainRetrySeconds $CaptureDrainRetrySeconds
    if ($LASTEXITCODE -ne 0) { throw "部署失败" }
    Write-Output "部署完成；staged migration/role provisioning 已完成，未执行数据 import 或 partial publish。"
    exit 0
}

foreach ($item in @($ReleaseRoot, $RemoteRuntimeRoot, $PrivilegedEnvPath, $PublisherEnvPath, $BundlePath, $ImportPlanPath, $PartialPlanPath, $QmiId, $CatalogIdentity)) {
    if (-not [string]::IsNullOrWhiteSpace($item)) { Assert-RemoteToken $item "remote path" }
}
if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) { throw "非 deploy 阶段必须指定 -ReleaseRoot" }

$python = "$RemoteRuntimeRoot/.venv/bin/python"
$base = "PYTHONPATH=$ReleaseRoot/QuoteMux/src:$ReleaseRoot/MarketHub/services/markethub_api/src $python"
switch ($Action) {
    "classify" {
        if (!$BundlePath -or !$ImportPlanPath) { throw "classify requires -BundlePath and -ImportPlanPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_pyramid_import classify --bundle '$BundlePath' --plan '$ImportPlanPath'"
    }
    "import" {
        if (!$BundlePath -or !$ImportPlanPath) { throw "import requires -BundlePath and -ImportPlanPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_pyramid_import publish --bundle '$BundlePath' --plan '$ImportPlanPath'"
    }
    "partial-plan" {
        if (!$PartialPlanPath -or !$QmiId) { throw "partial-plan requires -PartialPlanPath and -QmiId" }
        $generationArgument = if ($null -eq $ExpectedGeneration) { "" } else { " --expected-generation $ExpectedGeneration" }
        $catalogArgument = if ([string]::IsNullOrWhiteSpace($CatalogIdentity)) { "" } else { " --catalog-identity '$CatalogIdentity'" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication plan --qmi-id '$QmiId'$catalogArgument$generationArgument --plan '$PartialPlanPath'"
    }
    "partial-publish" {
        if (!$PartialPlanPath) { throw "partial-publish requires -PartialPlanPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication publish --plan '$PartialPlanPath'"
    }
    "verify" {
        if (!$PartialPlanPath) { throw "verify requires -PartialPlanPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication verify --plan '$PartialPlanPath'"
    }
}
