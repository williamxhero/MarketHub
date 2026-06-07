$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$workspaceRoot = Split-Path -Parent $root
$pythonExecutable = Join-Path $workspaceRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExecutable)) {
    throw "Missing virtual environment interpreter. Run install_markethub.py in the workspace root first."
}

Set-Location $root
& $pythonExecutable -m pytest services/markethub_api/tests services/markethub_console/tests -q
