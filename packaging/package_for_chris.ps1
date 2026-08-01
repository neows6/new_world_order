<#
================================================================================
  Build a clean, secret-free NWO zip to share.
  Run from the project root:
      powershell -ExecutionPolicy Bypass -File packaging\package_for_chris.ps1
  Output:  <Desktop>\nwo_share_<yyyy-MM-dd>.zip
================================================================================
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Say ($m) { Write-Host ">> $m" -ForegroundColor Cyan }

# Folders / files that must NEVER be shared (secrets, runtime state, junk)
$excludeDirs = @(
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".idea", ".vscode", "data", "logs", "tokens", "backups", ".mypy_cache",
    ".ruff_cache", "node_modules"
)
$excludeFiles = @(
    ".env", "test_page.html"
)
$excludeExt = @(".log", ".pyc", ".pyo", ".pyd", ".db", ".sqlite", ".sqlite3")

$stamp   = Get-Date -Format "yyyy-MM-dd"
$stageDir = Join-Path $env:TEMP "nwo_share_$stamp"
$zipPath  = Join-Path ([Environment]::GetFolderPath("Desktop")) "nwo_share_$stamp.zip"

if (Test-Path $stageDir) { Remove-Item $stageDir -Recurse -Force }
if (Test-Path $zipPath)  { Remove-Item $zipPath -Force }
New-Item -ItemType Directory $stageDir | Out-Null

Say "Copying project (excluding secrets + runtime data)..."
$copied = 0
Get-ChildItem -Path $Root -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($Root.Length).TrimStart('\')
    $parts = $rel -split '\\'

    # skip excluded directories anywhere in the path
    foreach ($p in $parts[0..($parts.Length - 2)]) {
        if ($excludeDirs -contains $p) { return }
    }
    if ($excludeFiles -contains $_.Name) { return }
    if ($excludeExt   -contains $_.Extension.ToLower()) { return }

    # Keep .claude ONLY for the setup skill + shareable commands; drop settings (machine-specific)
    if ($parts[0] -eq ".claude") {
        if ($rel -notlike ".claude\commands\*") { return }
    }

    $dest = Join-Path $stageDir $rel
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Copy-Item $_.FullName $dest -Force
    $copied++
}
Write-Host "   copied $copied files" -ForegroundColor Green

# Guarantee .env.example is present even if filtering was aggressive
if (-not (Test-Path (Join-Path $stageDir ".env.example"))) {
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $stageDir ".env.example")
}

# --- Safety scan: make sure no .env or token leaked in ------------------------
Say "Safety scan for leaked secrets..."
$leaks = Get-ChildItem $stageDir -Recurse -File | Where-Object {
    $_.Name -eq ".env" -or $_.Name -like "*token*.json" -or $_.Extension -eq ".db"
}
if ($leaks) {
    Write-Host "   [X] Secret-looking files found in staging - aborting:" -ForegroundColor Red
    $leaks | ForEach-Object { Write-Host "       $($_.FullName)" -ForegroundColor Red }
    exit 1
}
Write-Host "   clean - no .env / tokens / db in package" -ForegroundColor Green

Say "Zipping..."
Compress-Archive -Path (Join-Path $stageDir "*") -DestinationPath $zipPath -Force
Remove-Item $stageDir -Recurse -Force

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "`n==============================================" -ForegroundColor Green
Write-Host "  Package ready:" -ForegroundColor Green
Write-Host "  $zipPath  ($sizeMB MB)" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host "`nSend this zip to Chris. Tell him to unzip it and run:" -ForegroundColor Gray
Write-Host "    powershell -ExecutionPolicy Bypass -File install.ps1" -ForegroundColor Gray
Write-Host "...then open SETUP.md (or run /nwo-setup in Claude Code)." -ForegroundColor Gray
