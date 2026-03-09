$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$AppName = "MultiUserGoogleMeetJoiner"
$PythonCmd = "python"

Write-Host "Using Python command: $PythonCmd"
& $PythonCmd -m pip install --upgrade pip
& $PythonCmd -m pip install -r requirements-packaging.txt

& $PythonCmd -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name $AppName `
  --collect-all selenium `
  --collect-all webdriver_manager `
  main.py

$ExePath = Join-Path $RootDir "dist\$AppName\$AppName.exe"
if (Test-Path $ExePath) {
  Write-Host "Build successful: $ExePath"
} else {
  Write-Host "Build output created under dist\."
}
