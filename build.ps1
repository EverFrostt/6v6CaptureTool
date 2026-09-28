# Builds dist\OWScoreboardCapture.exe (one file, no console window).
# Run from any directory:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m pip install --quiet -r requirements-build.txt
# --noupx: UPX-compressed executables are a common antivirus false positive
python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --name OWScoreboardCapture ow_capture.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built $PSScriptRoot\dist\OWScoreboardCapture.exe"
