# =====================================================================
#  NWO Monitor — Always-Fresh Launcher  (Option A)
# ---------------------------------------------------------------------
#  Every run:
#    1. Finds any existing monitor (process LISTENING on :PORT) and
#       cleanly kills it AND the console window hosting it (no orphans).
#    2. Sweeps any lingering "NWO Monitor" titled windows from a prior
#       crash whose process is already dead.
#    3. Becomes the new monitor window itself, running current code.
#
#  Pointed at by the "NWO Start Monitor" desktop shortcut.
# =====================================================================

$ErrorActionPreference = 'SilentlyContinue'

$Proj = 'C:\Users\neo_w\new_world_order'
$Py   = 'C:\Users\neo_w\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$Port = 8765
$Title = 'NWO Monitor'

Write-Host ''
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host '   NWO Monitor  -  Always-Fresh Launcher' -ForegroundColor Cyan
Write-Host '  ============================================' -ForegroundColor Cyan
Write-Host ''

# --- 1. Kill any existing instance bound to the port + its window ----
$killedAnything = $false
try {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    foreach ($c in $conns) {
        $opid = [int]$c.OwningProcess
        if (-not $opid -or $opid -eq $PID) { continue }

        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$opid"
        if ($proc) {
            # If a console host (cmd/powershell/terminal) is its parent, kill
            # the whole tree from the parent so the old WINDOW closes too.
            $ppid = [int]$proc.ParentProcessId
            if ($ppid) {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ppid"
                if ($parent -and $ppid -ne $PID -and
                    $parent.Name -match '^(cmd|powershell|pwsh|WindowsTerminal|conhost)\.exe$') {
                    Write-Host "  Closing old monitor window (PID $ppid)..." -ForegroundColor Yellow
                    taskkill /PID $ppid /T /F | Out-Null
                    $killedAnything = $true
                }
            }
        }
        # Backstop: make sure the python itself is gone.
        taskkill /PID $opid /T /F | Out-Null
        Write-Host "  Stopped old monitor process (PID $opid)." -ForegroundColor Yellow
        $killedAnything = $true
    }
} catch {
    # Get-NetTCPConnection throws when nothing is listening — that's fine.
}

# --- 2. Sweep lingering titled windows from a crashed prior run ------
#     (process already dead but the console window is still sitting open)
taskkill /FI "WINDOWTITLE eq $Title*" /T /F | Out-Null

if (-not $killedAnything) {
    Write-Host '  No prior instance running. Starting fresh.' -ForegroundColor Green
} else {
    Write-Host '  Prior instance cleared.' -ForegroundColor Green
    Start-Sleep -Milliseconds 400   # let the port free up
}

# --- 3. Become the new monitor window -------------------------------
$Host.UI.RawUI.WindowTitle = $Title
Set-Location $Proj
Write-Host ''
Write-Host "  Launching fresh monitor on current code (http://localhost:$Port)" -ForegroundColor Cyan
Write-Host '  Press Ctrl+C to stop.' -ForegroundColor DarkGray
Write-Host ''

& $Py -m monitor.start_monitor

# --- After the monitor stops, hold the window so output stays visible
Write-Host ''
Write-Host '  Monitor stopped.' -ForegroundColor Yellow
Read-Host '  Press Enter to close this window'
