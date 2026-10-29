[CmdletBinding()]
param(
    [switch]$Offline,
    [string]$Wheelhouse = "wheelhouse"
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Repo

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher was not found. Install 64-bit Python 3.11 or newer from python.org and enable the launcher."
}
$Version = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$Version -lt [version]"3.11") {
    throw "Python 3.11 or newer is required; found $Version."
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or
    -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw "FFmpeg and FFprobe must be installed and available on PATH."
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & py -3 -m venv .venv
}
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
if ($Offline) {
    $ResolvedWheelhouse = (Resolve-Path $Wheelhouse).Path
    & $Python -m pip install --no-index --find-links $ResolvedWheelhouse -e ".[dev]"
} else {
    & $Python -m pip install -e ".[dev]"
}
& $Python -m pytest -q

Write-Host "Headless backend installed."
Write-Host "Start HTTP: .\packaging\windows\start-media-mcp.ps1"
Write-Host "Stdio command: $Python -m vertical_studio.mcp_server --transport stdio"
