$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force $dist | Out-Null
Copy-Item (Join-Path $root "web/index.html") (Join-Path $dist "index.html") -Force
Write-Host "MarketHub Console build output: $dist"
