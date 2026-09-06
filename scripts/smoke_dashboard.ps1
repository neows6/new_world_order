# scripts/smoke_dashboard.ps1 — boot the dashboard on a scratch port and probe key endpoints.
# Scriptable companion to step 8 of the /nwo-check slash command.
#   powershell -ExecutionPolicy Bypass -File scripts\smoke_dashboard.ps1
param([int]$Port = 8766)

Set-Location (Split-Path $PSScriptRoot -Parent)
$srv = Start-Process python -ArgumentList "-m uvicorn monitor.dashboard:app --port $Port --host 127.0.0.1" -PassThru -WindowStyle Hidden
Start-Sleep 25

$base = "http://127.0.0.1:$Port"
$fail = 0
foreach ($t in @(
    @{u="$base/";                    n="Dashboard HTML"},
    @{u="$base/paper";               n="Paper page HTML"},
    @{u="$base/paper.js";            n="paper.js"},
    @{u="$base/api/status";          n="/api/status"},
    @{u="$base/api/signals";         n="/api/signals"},
    @{u="$base/api/paper/account";   n="/api/paper/account"},
    @{u="$base/api/paper/trades";    n="/api/paper/trades"},
    @{u="$base/api/paper/scheduler"; n="/api/paper/scheduler"}
)) {
    try {
        $r = Invoke-WebRequest $t.u -UseBasicParsing -TimeoutSec 20
        if ($r.StatusCode -eq 200) { Write-Host ("[PASS] " + $t.n) }
        else { Write-Host ("[FAIL] " + $t.n + " HTTP " + $r.StatusCode); $fail++ }
    } catch { Write-Host ("[FAIL] " + $t.n + " - " + $_.Exception.Message); $fail++ }
}

Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

Write-Host ""
if ($fail -eq 0) { Write-Host "ALL ENDPOINTS PASS" } else { Write-Host "$fail ENDPOINT(S) FAILED" }
exit $fail
