[CmdletBinding()]
param(
    [ValidateSet("simulation", "real")]
    [string]$Mode = "simulation",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8766,
    [string]$DataDirectory = ""
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Run packaging\windows\setup-headless.ps1 first."
}
if (-not $DataDirectory) {
    $DataDirectory = Join-Path $env:LOCALAPPDATA "VerticalVideoStudio"
}
$env:VVS_DATA_DIR = $DataDirectory
$env:VVS_EXECUTION_MODE = $Mode
$env:VVS_MCP_HOST = $HostAddress
$env:VVS_MCP_PORT = "$Port"

Set-Location $Repo
& $Python -m vertical_studio.mcp_server --transport streamable-http --host $HostAddress --port $Port
