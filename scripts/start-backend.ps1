param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install backend requirements first. See README.md.' }
Push-Location $projectRoot
try {
    & $pythonPath -m uvicorn backend.app.main:create_app --factory --host 127.0.0.1 --port $Port --http h11 --ws none
} finally { Pop-Location }
