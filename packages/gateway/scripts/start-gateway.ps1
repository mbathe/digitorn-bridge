# start-gateway.ps1
#
# Boot script for digitorn-gateway on Windows.
#
# Why this script exists:
# Pydantic-settings reads config sources in this priority order:
#   1. Process env vars  (HIGHEST)
#   2. ~/.digitorn/gateway.env
#   3. Settings defaults
#
# If the operator's shell holds stale DIGITORN_GATEWAY_* exports from a
# previous experiment, those silently override gateway.env at boot.
# That's how we ended up with a gateway running against the wrong JWKS
# URL twice in one session.
#
# This script:
#   - clears ALL DIGITORN_GATEWAY_* vars from the current process env
#     BEFORE launching, so only gateway.env + defaults apply.
#   - guarantees one process: kills any prior listener on :8002 first.
#   - logs to a timestamped file under ~/.digitorn/logs/.
#   - validates /healthz returns 200 within 30s before declaring success.
#
# Usage:
#   .\scripts\start-gateway.ps1                  # default port 8002
#   .\scripts\start-gateway.ps1 -Port 8002 -PythonExe py
#
# On Linux/Mac the same logic is implemented in start-gateway.sh
# (not yet written; mirror this contract).

param(
    [int]$Port = 8002,
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @("-3.12", "-m", "digitorn_gateway")
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "==> Clearing DIGITORN_GATEWAY_* overrides from current shell" -ForegroundColor Cyan
Get-ChildItem Env: | Where-Object { $_.Name -like "DIGITORN_GATEWAY_*" } | ForEach-Object {
    Write-Host "    unset $($_.Name)" -ForegroundColor DarkGray
    Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "==> Checking for existing process on port $Port" -ForegroundColor Cyan
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) {
    $existingPid = $listener.OwningProcess
    Write-Host "    found PID $existingPid on port $Port -> stopping" -ForegroundColor Yellow
    Stop-Process -Id $existingPid -Force
    Start-Sleep -Seconds 2
} else {
    Write-Host "    port $Port free" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "==> Verifying gateway.env exists" -ForegroundColor Cyan
$envFile = Join-Path $env:USERPROFILE ".digitorn\gateway.env"
if (-not (Test-Path $envFile)) {
    Write-Error "gateway.env missing at $envFile. The gateway needs DIGITORN_GATEWAY_DATABASE_URL and DIGITORN_GATEWAY_MASTER_KEY to boot."
    exit 1
}
Write-Host "    $envFile present" -ForegroundColor DarkGray

Write-Host ""
Write-Host "==> Launching gateway on port $Port" -ForegroundColor Cyan
$logDir = Join-Path $env:USERPROFILE ".digitorn\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $logDir "gateway-$ts.log"
$err = "$out.err"
Write-Host "    stdout -> $out" -ForegroundColor DarkGray
Write-Host "    stderr -> $err" -ForegroundColor DarkGray

# Set the port via env so Settings picks it up (DIGITORN_GATEWAY_PORT
# overrides the default 8002).
$env:DIGITORN_GATEWAY_PORT = "$Port"

Start-Process -FilePath $PythonExe -ArgumentList $PythonArgs `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err

Write-Host ""
Write-Host "==> Waiting for /healthz (up to 30s)" -ForegroundColor Cyan
$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$Port/healthz" -TimeoutSec 2 -ErrorAction Stop
        if ($r.status -eq "ok") { $ok = $true; break }
    } catch { }
}

if ($ok) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    $newPid = if ($listener) { $listener.OwningProcess } else { "?" }
    Write-Host ""
    Write-Host "    READY  pid=$newPid  port=$Port" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "    FAILED to come up. Tail of stderr:" -ForegroundColor Red
    Get-Content $err -Tail 30 -ErrorAction SilentlyContinue
    exit 1
}
