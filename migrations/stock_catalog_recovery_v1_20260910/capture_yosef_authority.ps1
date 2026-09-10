param(
    [Parameter(Mandatory = $true)][string]$HostName,
    [Parameter(Mandatory = $true)][string]$ProviderPython,
    [Parameter(Mandatory = $true)][string]$RemoteEnvPath,
    [Parameter(Mandatory = $true)][string]$RemoteReleaseInputs,
    [Parameter(Mandatory = $true)][string]$HealthUrl,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"

foreach ($remotePath in @($ProviderPython, $RemoteEnvPath, $RemoteReleaseInputs)) {
    if ($remotePath -notmatch '^/[A-Za-z0-9._/-]+$') {
        throw "Remote path contains unsupported characters: $remotePath"
    }
}
if ($HealthUrl -notmatch '^http://127[.]0[.]0[.]1:[0-9]+/[A-Za-z0-9._/-]+$') {
    throw "HealthUrl must be an explicit loopback HTTP endpoint"
}

$target = [System.IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $target) {
    throw "Refusing to overwrite an authority bundle: $target"
}
$parent = Split-Path -Parent $target
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent -ErrorAction Stop | Out-Null
}

$captureScript = Join-Path $PSScriptRoot "capture_authority.py"
$source = Get-Content -LiteralPath $captureScript -Raw -Encoding UTF8
$remoteCommand = (
    "$ProviderPython -B - --env-file $RemoteEnvPath " +
    "--release-inputs $RemoteReleaseInputs --health-url $HealthUrl"
)
$output = $source | ssh $HostName $remoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "Read-only authority capture failed with exit code $LASTEXITCODE"
}
$text = ($output -join "`n") + "`n"
[System.IO.File]::WriteAllText(
    $target,
    $text,
    [System.Text.UTF8Encoding]::new($false)
)

$payload = Get-Content -LiteralPath $target -Raw -Encoding UTF8 | ConvertFrom-Json
if ($payload.format_version -ne "markethub-stock-authority-bundle-v1") {
    throw "Authority capture returned an unsupported format"
}
$fileHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output (ConvertTo-Json -Compress -InputObject ([ordered]@{
    output_path = $target
    file_sha256 = $fileHash
    bundle_sha256 = $payload.bundle_sha256
    listed = $payload.authority_source.raw_receipt.listed.row_count
    pending = $payload.authority_source.raw_receipt.pending.row_count
    delisted = $payload.authority_source.raw_receipt.delisted.row_count
}))
