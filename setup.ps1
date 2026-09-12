param(
    [switch]$SkipBrowserRuntime
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Frontend = Join-Path $Root 'web\frontend'
$env:PYTHONUTF8 = '1'

function Info($Message) { Write-Host "[Ripple] $Message" -ForegroundColor Cyan }
function Ok($Message) { Write-Host "  [OK] $Message" -ForegroundColor Green }
function Fail($Message) { throw $Message }

Write-Host "`nRipple - Windows setup" -ForegroundColor Magenta

$py = Get-Command py -ErrorAction SilentlyContinue
$pyArgs = @('-3')
if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue; $pyArgs = @() }
if (-not $py) { Fail 'Python 3.10+ is required.' }
& $py.Source @pyArgs -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { Fail 'Ripple requires Python 3.10+.' }

$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) { Fail 'Node.js 22.19+ with npm is required.' }
$parts = (& $node.Source -p 'process.versions.node').Split('.') | ForEach-Object { [int]$_ }
if ($parts[0] -lt 22 -or ($parts[0] -eq 22 -and $parts[1] -lt 19)) { Fail 'Ripple frontend build requires Node.js 22.19+.' }

if (-not (Test-Path $Venv)) {
    Info 'Creating project virtual environment .venv...'
    & $py.Source @pyArgs -m venv $Venv
}
if (-not (Test-Path $Python)) { Fail 'Failed to create .venv.' }

Info 'Installing Python dependencies...'
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail 'pip upgrade failed.' }
& $Python -m pip install -e $Root
if ($LASTEXITCODE -ne 0) { Fail 'Ripple Python dependency installation failed.' }

Info 'Building Web frontend...'
Push-Location $Frontend
try {
    & $npm.Source ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Fail 'npm ci failed.' }
    & $npm.Source run build
    if ($LASTEXITCODE -ne 0) { Fail 'Frontend build failed.' }
} finally { Pop-Location }

if (-not $SkipBrowserRuntime) {
    Info 'Installing Playwright Chromium...'
    & $Python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Fail 'Playwright Chromium installation failed.' }
}

$envPath = Join-Path $Root '.env'
if (-not (Test-Path $envPath) -and (Test-Path (Join-Path $Root '.env.example'))) {
    Copy-Item (Join-Path $Root '.env.example') $envPath
    Ok 'Created local .env (ignored by Git).'
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning 'FFmpeg was not detected. Video/audio processing will be limited; the core workbench can still start.'
}

Ok 'Setup complete.'
Write-Host 'Ripple does not globally install or modify OpenCode / Claude Code / Codex / Hermes. Existing Agents are detected from Settings > Agent after startup.'
Write-Host 'Start: powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-Ripple.ps1' -ForegroundColor Cyan
