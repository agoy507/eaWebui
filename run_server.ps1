$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Virtual environment belum tersedia. Jalankan setup sesuai README.md."
}

Set-Location -LiteralPath $projectRoot
& $pythonPath -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
