<#
================================================================================
  New World Order (NWO) — Windows installer
  Run from the project folder:   powershell -ExecutionPolicy Bypass -File install.ps1
  Safe to re-run; it skips steps that are already done.
================================================================================
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Say  ($m) { Write-Host "`n>> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "   [ok] $m"   -ForegroundColor Green }
function Warn ($m) { Write-Host "   [!]  $m"   -ForegroundColor Yellow }
function Die  ($m) { Write-Host "`n[X] $m" -ForegroundColor Red; exit 1 }

Write-Host "==============================================" -ForegroundColor Magenta
Write-Host "  New World Order  -  install" -ForegroundColor Magenta
Write-Host "==============================================" -ForegroundColor Magenta

# --- 1. Python -----------------------------------------------------------------
Say "Checking for Python 3.10+"
$py = $null
foreach ($cand in @("python", "py -3")) {
    try {
        $v = & cmd /c "$cand --version" 2>&1
        if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 10) { $py = $cand; $pyver = $v; break }
        }
    } catch {}
}
if (-not $py) { Die "Python 3.10+ not found. Install it from https://python.org (check 'Add to PATH') and re-run." }
Ok "$pyver  ($py)"

# --- 2. Virtual environment ----------------------------------------------------
Say "Creating virtual environment (.venv)"
if (Test-Path ".venv") {
    Ok ".venv already exists"
} else {
    & cmd /c "$py -m venv .venv"
    if ($LASTEXITCODE -ne 0) { Die "Failed to create virtual environment." }
    Ok "created .venv"
}
$vpy  = Join-Path $Root ".venv\Scripts\python.exe"
$vpip = Join-Path $Root ".venv\Scripts\pip.exe"
if (-not (Test-Path $vpy)) { Die "venv python missing at $vpy" }

# --- 3. Dependencies -----------------------------------------------------------
Say "Installing dependencies (this can take a few minutes)"
& $vpy -m pip install --upgrade pip --quiet
& $vpip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die "pip install failed. Scroll up for the error." }
Ok "dependencies installed"

# --- 4. .env -------------------------------------------------------------------
Say "Setting up .env (your private API keys)"
if (Test-Path ".env") {
    Ok ".env already exists (left untouched)"
} else {
    Copy-Item ".env.example" ".env"
    Ok "created .env from template"
    Warn "You MUST edit .env and add your own API keys before NWO will work."
    $open = Read-Host "   Open .env in Notepad now? (y/n)"
    if ($open -eq "y") { Start-Process notepad ".env" }
}

# --- 5. Folders + database -----------------------------------------------------
Say "Creating runtime folders + database"
foreach ($d in @("data", "logs", "tokens")) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory $d | Out-Null }
}
& $vpy -c "from config import config; from models.database import init_db; init_db(config.database.url); print('db-ok')"
if ($LASTEXITCODE -ne 0) { Warn "DB init reported an error (often just missing keys in .env). Fix .env, then re-run." }
else { Ok "database initialized (data/schwab_trader.db)" }

# --- 6. CA bundle (Norton / AV SSL inspection) ---------------------------------
Say "Building CA bundle for antivirus SSL inspection"
if (Test-Path "scripts\refresh_ca_bundle.ps1") {
    try {
        & powershell -ExecutionPolicy Bypass -File "scripts\refresh_ca_bundle.ps1"
        Ok "CA bundle refreshed (data/ca_bundle_with_norton.pem)"
    } catch {
        Warn "CA bundle step skipped/failed. Only needed if an AV (Norton, ESET, etc.) intercepts HTTPS. NWO still runs without it."
    }
} else {
    Warn "scripts\refresh_ca_bundle.ps1 not found - skipping (only matters on AV-inspected machines)."
}

# --- 7. Claude Code setup skill ------------------------------------------------
Say "Installing the /nwo-setup Claude Code skill"
$skillSrc = Join-Path $Root "packaging\nwo-setup-skill"
$skillDst = Join-Path $HOME ".claude\skills\nwo-setup"
if (Test-Path $skillSrc) {
    New-Item -ItemType Directory -Force (Split-Path $skillDst) | Out-Null
    Copy-Item $skillSrc $skillDst -Recurse -Force
    Ok "skill installed -> $skillDst  (use /nwo-setup inside Claude Code)"
} else {
    Warn "setup skill folder not found - skipping."
}

# --- Done ----------------------------------------------------------------------
Write-Host "`n==============================================" -ForegroundColor Green
Write-Host "  Install complete." -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host @"

Next steps:
  1. Make sure .env has your real API keys.
  2. Authorize Schwab (opens a browser, one time):
       .\.venv\Scripts\python.exe get_token.py
  3. Start the dashboard:
       .\.venv\Scripts\python.exe -m monitor.start_monitor
       then open http://localhost:8765
  4. Run the trading pipeline:
       .\.venv\Scripts\python.exe main.py

Tip: inside Claude Code, run /nwo-setup and it will walk you through all of this.
"@ -ForegroundColor Gray
