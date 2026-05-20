# Digitorn installer for Windows.
#
# Usage:
#   irm https://digitorn.ai/install.ps1 | iex
#
# What it does:
#   1. Installs uv (Python manager) if not present.
#   2. Installs Digitorn into an isolated uv tool environment.
#   3. Registers Digitorn as a Windows Service (auto-start on boot).
#   4. Starts the service.
#
# Re-running the script upgrades to the latest release.

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Gray
}

function Write-Done {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Green
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-Elevated {
    Write-Step "Re-launching with administrator rights (service registration requires it)"
    $argList = "-NoProfile -ExecutionPolicy Bypass -Command `"& { irm https://digitorn.ai/install.ps1 | iex }`""
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    exit
}

if (-not (Test-Admin)) {
    Invoke-Elevated
}

Write-Host ""
Write-Host "Digitorn installer" -ForegroundColor White
Write-Host "------------------" -ForegroundColor White

# ---------------------------------------------------------------------------
# 1. uv (manages Python 3.12 transparently)
# ---------------------------------------------------------------------------
Write-Step "Checking for uv"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Info "Not found. Installing from astral.sh..."
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"

    # uv installs into $env:USERPROFILE\.local\bin and updates PATH for new
    # sessions only. Add it to the current session so the next commands work.
    $uvBin = Join-Path $env:USERPROFILE ".local\bin"
    if (Test-Path $uvBin) {
        $env:PATH = "$uvBin;$env:PATH"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv install failed. See https://docs.astral.sh/uv/getting-started/installation/" -ForegroundColor Red
    exit 1
}

$uvVersion = (uv --version) -replace "^uv ", ""
Write-Done "uv $uvVersion"

# ---------------------------------------------------------------------------
# 2. Digitorn
# ---------------------------------------------------------------------------
Write-Step "Installing Digitorn"
Write-Info "This downloads Python 3.12, the daemon, and ~2 GB of model weights"
Write-Info "the first time. Subsequent runs use the cache."

# `uv tool install` creates an isolated venv per tool and exposes the
# entry point on PATH ($env:USERPROFILE\.local\bin\digitorn.exe).
uv tool install --python 3.12 --force "digitorn" | Out-Host

# Make sure the tool dir is on PATH for the current session.
$toolBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path $toolBin) {
    $env:PATH = "$toolBin;$env:PATH"
}

if (-not (Get-Command digitorn -ErrorAction SilentlyContinue)) {
    Write-Host "digitorn entry point not found after install. Check uv output above." -ForegroundColor Red
    exit 1
}

$digitornVersion = (digitorn version 2>$null) -join " "
Write-Done "digitorn installed ($digitornVersion)"

# ---------------------------------------------------------------------------
# 3. Windows Service
# ---------------------------------------------------------------------------
Write-Step "Registering the Windows Service"

# Stop + remove any existing install so the upgrade path is idempotent.
& digitorn service stop 2>$null | Out-Null
& digitorn service uninstall 2>$null | Out-Null

& digitorn service install
if ($LASTEXITCODE -ne 0) {
    Write-Host "Service registration failed. See message above." -ForegroundColor Red
    exit 1
}
Write-Done "Service registered (auto-start on boot)"

# ---------------------------------------------------------------------------
# 4. Start
# ---------------------------------------------------------------------------
Write-Step "Starting the daemon"
& digitorn service start
if ($LASTEXITCODE -ne 0) {
    Write-Host "Service start failed. Try: digitorn service logs" -ForegroundColor Red
    exit 1
}
Write-Done "Daemon listening on http://127.0.0.1:8000"

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
Write-Host "  digitorn doctor            check the environment"
Write-Host "  digitorn init my-app       scaffold a project"
Write-Host "  digitorn service status    is the daemon up?"
Write-Host "  digitorn service logs      recent log lines"
Write-Host ""
Write-Host "Documentation: https://docs.digitorn.ai"
Write-Host ""
