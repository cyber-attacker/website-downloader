# Build the distributable Website Downloader exe + (optionally) the installer.
# Usage:  .\packaging\build_windows.ps1 [-WithInstaller]
param(
    [switch]$WithInstaller   # also compile packaging\WebsiteDownloader.iss via ISCC
)

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
# NOTE: PyInstaller writes its progress to stderr; $ErrorActionPreference
# must stay 'Continue' or PowerShell aborts on the first INFO line.
$ErrorActionPreference = "Continue"

Write-Host "== building WebsiteDownloader.exe (onefile, windowed)" -ForegroundColor Cyan
$before = if (Test-Path (Join-Path $root "dist\WebsiteDownloader.exe")) {
    (Get-Item (Join-Path $root "dist\WebsiteDownloader.exe")).LastWriteTime
} else { [datetime]::MinValue }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name WebsiteDownloader --collect-data qt_material run_gui.py | Out-Null
$exe = Join-Path $root "dist\WebsiteDownloader.exe"
if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -le $before) {
    throw "PyInstaller produced no fresh exe - build failed"
}
Write-Host "== built dist\WebsiteDownloader.exe ($([math]::Round((Get-Item $exe).Length/1MB,1)) MB)" -ForegroundColor Green

# Playwright browsers are intentionally NOT bundled (~300 MB); the app shows
# a one-time prompt to run `playwright install chromium` when needed.

if ($WithInstaller) {
    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        throw "Inno Setup 6 not found at $iscc - install it (free) to build the installer"
    }
    Write-Host "== compiling the installer" -ForegroundColor Cyan
    & $iscc (Join-Path $PSScriptRoot "WebsiteDownloader.iss")
    Write-Host "== installer in dist\installer" -ForegroundColor Green
}
