$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
py -3.13 -m pytest services/markethub_api/tests services/markethub_console/tests -q
