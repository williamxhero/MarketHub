$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
powershell -ExecutionPolicy Bypass -File (Join-Path $root "services/markethub_console/scripts/build.ps1")
