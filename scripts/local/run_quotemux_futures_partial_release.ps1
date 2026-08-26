param(
    [Parameter(Mandatory = $true)][ValidateSet("deploy", "migrate", "classify", "import", "partial-plan", "partial-publish", "verify")][string]$Action,
    [Parameter(Mandatory = $true)][string]$HostName,
    [string]$RemoteRoot = "/data/MarketHub2",
    [string]$RemoteRuntimeRoot = "/data/markethub",
    [string]$RemoteEnvPath = "/data/markethub/env/markethub-api.env",
    [string]$ServiceName = "markethub-api",
    [string]$HealthUrl = "http://127.0.0.1:8803/api/health",
    [string]$QuoteMuxSourceRoot = "",
    [string]$QuoteMuxPackagesSourceRoot = "",
    [string]$ReleaseRoot = "",
    [string]$BundlePath = "",
    [string]$ManifestPath = "",
    [string]$PrivilegedEnvPath = "/data/markethub/env/quotemux-futures-partial-migration.env",
    [string]$PublisherEnvPath = "/data/markethub/env/quotemux-futures-partial-publisher.env"
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
    & $deploy -HostName $HostName -RemoteRoot $RemoteRoot -RemoteRuntimeRoot $RemoteRuntimeRoot -RemoteEnvPath $RemoteEnvPath -ServiceName $ServiceName -HealthUrl $HealthUrl -QuoteMuxSourceRoot $QuoteMuxSourceRoot -QuoteMuxPackagesSourceRoot $QuoteMuxPackagesSourceRoot
    if ($LASTEXITCODE -ne 0) { throw "部署失败" }
    Write-Output "部署完成；未执行 migration/import/partial publish。"
    exit 0
}

foreach ($item in @($ReleaseRoot, $RemoteRuntimeRoot, $PrivilegedEnvPath, $PublisherEnvPath, $BundlePath, $ManifestPath)) {
    if (-not [string]::IsNullOrWhiteSpace($item)) { Assert-RemoteToken $item "remote path" }
}
if ([string]::IsNullOrWhiteSpace($ReleaseRoot)) { throw "非 deploy 阶段必须指定 -ReleaseRoot" }

$python = "$RemoteRuntimeRoot/.venv/bin/python"
$base = "PYTHONPATH=$ReleaseRoot/QuoteMux/src:$ReleaseRoot/MarketHub/services/markethub_api/src $python"
switch ($Action) {
    "migrate" {
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PrivilegedEnvPath'; set +a; $base '$ReleaseRoot/MarketHub/migrations/quotemux_futures_partial_v1_20260826/release_migration.py'"
    }
    "classify" {
        if (!$BundlePath) { throw "classify requires -BundlePath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_pyramid_import classify --bundle '$BundlePath'"
    }
    "import" {
        if (!$BundlePath) { throw "import requires -BundlePath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_pyramid_import publish --bundle '$BundlePath'"
    }
    "partial-plan" {
        if (!$ManifestPath) { throw "partial-plan requires -ManifestPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication plan --manifest '$ManifestPath'"
    }
    "partial-publish" {
        if (!$ManifestPath) { throw "partial-publish requires -ManifestPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication publish --manifest '$ManifestPath'"
    }
    "verify" {
        if (!$ManifestPath) { throw "verify requires -ManifestPath" }
        Invoke-RemoteStage "set -euo pipefail; set -a; . '$PublisherEnvPath'; set +a; $base -m quotemux.store.futures_partial_publication verify --manifest '$ManifestPath'"
    }
}
