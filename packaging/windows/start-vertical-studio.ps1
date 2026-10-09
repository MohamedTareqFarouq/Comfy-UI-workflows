$ErrorActionPreference = "Stop"
$Distro = if ($env:VVS_WSL_DISTRO) { $env:VVS_WSL_DISTRO } else { "Ubuntu" }
$Project = if ($env:VVS_WSL_PROJECT) { $env:VVS_WSL_PROJECT } else { "~/vertical-video-studio" }
wsl.exe -d $Distro -- bash -lc "cd $Project && test -x start.sh && (pgrep -f 'vertical_studio.cli serve' >/dev/null || nohup ./start.sh >~/.local/share/vertical-video-studio/server.log 2>&1 &)"
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:8765"
