param([int]$Port = 8000, [switch]$Install, [switch]$Intel)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$venvDirectory = if ($Intel) { '.venv-intel' } else { '.venv' }
$pythonPath = Join-Path $projectRoot "$venvDirectory/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
if ($Intel) { $env:LIBRARY_AI_PROVIDER = 'intel' }
if ($Install) {
    & $pythonPath -m pip install -r server/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if ($Intel) {
        & $pythonPath -m pip install -r server/requirements-ai-intel.txt
        if ($LASTEXITCODE -ne 0) { throw 'Intel inference dependency installation failed.' }
    }
    & $pythonPath scripts/install_mihomo.py
    if ($LASTEXITCODE -ne 0) { throw 'Bundled Mihomo installation failed.' }
    Push-Location web
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
New-Item -ItemType Directory -Force -Path '.runtime','data','imports','models' | Out-Null
$runtimeRoot = Join-Path $projectRoot '.runtime'
if (-not $env:LIBRARY_DATABASE_URL) { $env:LIBRARY_DATABASE_URL = 'sqlite:///' + ((Join-Path $runtimeRoot 'library.db') -replace '\\','/') }
if (-not $env:LIBRARY_STORAGE_ROOT) { $env:LIBRARY_STORAGE_ROOT = Join-Path $projectRoot 'data' }
if (-not $env:LIBRARY_IMPORT_ROOT) { $env:LIBRARY_IMPORT_ROOT = Join-Path $projectRoot 'imports' }
if (-not $env:LIBRARY_MODEL_DIR) { $env:LIBRARY_MODEL_DIR = Join-Path $projectRoot 'models' }
if (-not $env:LIBRARY_MIHOMO_PATH) { $env:LIBRARY_MIHOMO_PATH = Join-Path $runtimeRoot 'mihomo/mihomo.exe' }
if (-not $env:LIBRARY_MIHOMO_DIR) { $env:LIBRARY_MIHOMO_DIR = Join-Path $runtimeRoot 'mihomo-state' }
& $pythonPath scripts/migrate_dev.py
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed; the server was not started.' }
Write-Host "Local development: http://127.0.0.1:$Port (SQLite)."
Write-Host 'If LIBRARY_ADMIN_PASSWORD is unset, local development password is local-library.'
& $pythonPath -m uvicorn app.main:app --app-dir server --host 127.0.0.1 --port $Port
