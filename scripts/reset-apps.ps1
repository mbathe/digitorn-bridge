# Reset all custom (non-builtin) deployed apps + their bundles.
# Builtins (digitorn-chat / -builder / -clone / -code / -deepresearch /
# -react-sandbox) are off-limits — the daemon refuses to delete them and
# re-bootstraps them at every start.
#
# The /api/apps response unfortunately doesn't always populate
# `is_builtin`, so we hard-code the list from the source of truth:
# packages/digitorn/builtins/.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\reset-apps.ps1

$ErrorActionPreference = "Stop"

$BUILTINS = @(
    "digitorn-builder",
    "digitorn-chat",
    "digitorn-clone",
    "digitorn-code",
    "digitorn-deepresearch",
    "digitorn-react-sandbox"
)

# 1. Read CLI credentials
$credPath = Join-Path $env:USERPROFILE ".digitorn\credentials.json"
if (-not (Test-Path $credPath)) {
    Write-Host "No CLI credentials at $credPath - run 'digitorn login' first." -ForegroundColor Red
    exit 1
}
$cred = Get-Content $credPath -Raw | ConvertFrom-Json
$token = $cred.access_token
if (-not $token) {
    Write-Host "credentials.json has no access_token - run 'digitorn login' again." -ForegroundColor Red
    exit 1
}

$headers = @{ Authorization = "Bearer $token" }
$daemon = "http://127.0.0.1:8000"

# 2. List apps (admin view: include disabled too)
Write-Host ""
Write-Host "== Listing apps ==" -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Uri "$daemon/api/apps?include_disabled=true" -Headers $headers -Method GET
} catch {
    Write-Host "Could not reach daemon at $daemon - is it running?" -ForegroundColor Red
    exit 1
}

$apps = $resp.data
if (-not $apps -or $apps.Count -eq 0) {
    Write-Host "No apps deployed." -ForegroundColor Yellow
    exit 0
}

foreach ($a in $apps) {
    $tag = if ($BUILTINS -contains $a.app_id) { "[builtin]" } else { "[custom] " }
    Write-Host "  $tag $($a.app_id) - $($a.runtime_status)"
}

# 3. Delete every custom app at scope=system (CLI deploy default).
#    delete_history=true wipes bundles + DB rows in one shot.
Write-Host ""
Write-Host "== Deleting custom apps (scope=system) ==" -ForegroundColor Cyan
$deleted = 0
$skipped = 0
$failed  = 0
$succeededIds = @()  # only these get the disk-wipe defence at the end
foreach ($a in $apps) {
    if ($BUILTINS -contains $a.app_id) {
        Write-Host "  SKIP $($a.app_id) (builtin)" -ForegroundColor DarkGray
        $skipped++
        continue
    }
    $url = "$daemon/api/apps/$($a.app_id)?delete_history=true&scope=system"
    try {
        $r = Invoke-RestMethod -Uri $url -Headers $headers -Method DELETE
        if ($r.success) {
            $bundles = $r.data.bundles_deleted
            Write-Host "  DEL  $($a.app_id) (bundles=$bundles)" -ForegroundColor Green
            $deleted++
            $succeededIds += $a.app_id
        } else {
            $errCode = $r.error
            Write-Host "  FAIL $($a.app_id) - $errCode" -ForegroundColor Red
            $failed++
        }
    } catch {
        $msg = $_.Exception.Message
        # Surface the actual response body when the daemon returned 4xx/5xx,
        # otherwise the PowerShell exception message is opaque.
        try {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $body = $reader.ReadToEnd()
            $msg = "$msg :: $body"
        } catch { /* ignore */ }
        Write-Host "  FAIL $($a.app_id) - $msg" -ForegroundColor Red
        $failed++
    }
}

# 4. Defence-in-depth disk wipe — ONLY for apps the API confirmed it
#    deleted. Earlier this step ran for every app unconditionally, so a
#    blocked API call (403 / nothing_to_delete) still wiped the on-disk
#    bundle while the DB row stayed. Result: orphan rows that make the
#    daemon log "Bundle … missing on disk - falling back to legacy
#    yaml_content" at every startup. Now the disk wipe is gated.
if ($succeededIds.Count -gt 0) {
    Write-Host ""
    Write-Host "== Cleaning bundle dirs (only for confirmed deletes) ==" -ForegroundColor Cyan
    $bundleRoot = Join-Path $env:USERPROFILE ".digitorn\apps"
    if (Test-Path $bundleRoot) {
        foreach ($appId in $succeededIds) {
            $appDir = Join-Path $bundleRoot $appId
            if (Test-Path $appDir) {
                try {
                    Remove-Item -Path $appDir -Recurse -Force
                    Write-Host "  RM   $appDir" -ForegroundColor Green
                } catch {
                    Write-Host "  FAIL $appDir - $($_.Exception.Message)" -ForegroundColor Red
                }
            }
        }
    }
}

Write-Host ""
Write-Host "Done. deleted=$deleted skipped=$skipped failed=$failed" -ForegroundColor Cyan
if ($failed -gt 0) {
    Write-Host ""
    Write-Host "API DELETE failed for $failed app(s). The daemon now has them" -ForegroundColor Yellow
    Write-Host "in DB but with possibly missing bundles on disk - which is what" -ForegroundColor Yellow
    Write-Host "produces the 'Bundle … missing on disk' warnings at boot." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Workaround: stop the daemon, then run the direct-DB wipe:" -ForegroundColor Yellow
    Write-Host "  py -3.12 c:\Users\ASUS\Documents\digitorn-bridge\scripts\reset-apps-direct.py" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Restart the daemon (kill + relaunch) for a clean in-memory state."
Write-Host "  2. Re-deploy with:"
Write-Host "       digitorn dev deploy `"c:\Users\ASUS\Documents\digitorn-bridge\examples\copilot-smoke\app.yaml`""
