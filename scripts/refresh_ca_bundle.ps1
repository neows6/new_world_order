# scripts/refresh_ca_bundle.ps1
#
# Rebuilds data/ca_bundle_with_norton.pem by:
#   1. Reading the latest certifi default bundle (from the installed Python package)
#   2. Extracting Norton (or any local AV) root certs from the Windows cert store
#   3. Combining them into a single PEM bundle
#
# Run this whenever:
#   - Norton/Symantec rotates its inspection root cert
#   - certifi is upgraded and you want the newer bundle as the base
#   - You start seeing "unable to get local issuer certificate" errors again
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\refresh_ca_bundle.ps1

$ErrorActionPreference = "Stop"

Write-Host "Refreshing CA bundle for Norton/AV SSL-inspected environments..."

# Locate certifi's bundle
$certifiPath = & python -c "import certifi; print(certifi.where())"
if (-not (Test-Path $certifiPath)) {
    Write-Error "Could not locate certifi bundle at $certifiPath"
    exit 1
}
Write-Host "  certifi bundle: $certifiPath"

# Find any AV-installed root certs in either machine or user store
$avPatterns = @("*Norton*", "*Symantec*", "*Zscaler*", "*ESET*", "*Kaspersky*", "*Bitdefender*", "*Avast*", "*McAfee*")
$avCerts = @()
foreach ($store in @("Cert:\LocalMachine\Root", "Cert:\CurrentUser\Root")) {
    foreach ($pattern in $avPatterns) {
        $found = Get-ChildItem -Path $store -Recurse -ErrorAction SilentlyContinue |
                 Where-Object { $_.Subject -like $pattern }
        if ($found) { $avCerts += $found }
    }
}

# De-dupe by thumbprint
$avCerts = $avCerts | Sort-Object Thumbprint -Unique

if ($avCerts.Count -eq 0) {
    Write-Host "  No AV inspection roots detected - using certifi only"
} else {
    Write-Host "  Found $($avCerts.Count) AV inspection root(s):"
    foreach ($c in $avCerts) {
        Write-Host "    - $($c.Subject)"
    }
}

# Build combined bundle
$repoRoot = Split-Path -Parent $PSScriptRoot
$outPath  = Join-Path $repoRoot "data\ca_bundle_with_norton.pem"

$content = Get-Content -Path $certifiPath -Raw -Encoding ASCII
foreach ($c in $avCerts) {
    $b64 = [System.Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
    $pem = "`n# AV SSL-inspection root: $($c.Subject)`n-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----`n"
    $content += $pem
}

$content | Out-File -FilePath $outPath -Encoding ASCII -NoNewline
Write-Host "  Combined bundle: $outPath ($((Get-Item $outPath).Length) bytes)"
Write-Host "Done."
