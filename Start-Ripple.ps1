param(
    [ValidateRange(1, 65535)][int]$Port = 7860
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Frontend = Join-Path $Root 'web\frontend\dist\index.html'

if (-not (Test-Path $Python)) {
    throw 'Missing .venv. Run: powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1'
}
if (-not (Test-Path $Frontend)) {
    throw 'Frontend is not built. Run setup.ps1, or run npm ci && npm run build in web\frontend.'
}

$env:PYTHONUTF8 = '1'
$env:RIPPLE_PORT = [string]$Port
Write-Host "Ripple -> http://127.0.0.1:$Port" -ForegroundColor Cyan
& $Python -X utf8 (Join-Path $Root 'web\app.py')
exit $LASTEXITCODE
