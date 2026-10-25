# Build the distributable Website Downloader exe + (optionally) the installer.
# Usage:  .\packaging\build_windows.ps1 [-WithInstaller]
param(
    [switch]$WithInstaller   # also compile packaging\WebsiteDownloader.iss via ISCC
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== building WebsiteDownloader.exe (onefile, windowed)" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name WebsiteDownloader --collect-data qt_material run_gui.py
if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {   # PS wraps stderr noise as 1
    throw "PyInstaller build failed"
}

$exe = Join-Path $root "dist\WebsiteDownloader.exe"
if (-not (Test-Path $exe)) { throw "exe not produced" }
Write-Host "== built $exe" -ForegroundColor Green

# Playwright browsers are intentionally NOT bundled (~300 MB); the app shows
# a one-time prompt to run `playwright install chromium` when needed.

if ($WithInstaller) {
    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        throw "Inno Setup 6 not found at $iscc — install it (free) to build the installer"
    }
    Write-Host "== compiling the installer" -ForegroundColor Cyan
    & $iscc (Join-Path $PSScriptRoot "WebsiteDownloader.iss")
    Write-Host "== installer in dist\installer" -ForegroundColor Green
}
