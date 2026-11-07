# Headless media backend on a fresh Windows system

The media backend does not require the PWA. OpenCode is the primary creative
orchestrator: it selects its own reasoning model, calls typed MCP tools, and
stores role handoffs in production sessions. The backend owns workflow
validation, queues, artifacts, approvals, budgets, retries, cancellation, and
GPU serialization.

Start in simulation mode. Real Flux Klein and Wan 2.2 capabilities remain
unavailable or unqualified until their API workflow exports, node maps, model
files, and target-machine qualification are present. Discovery reports that
state and execution rejects an unqualified capability.

## Native Windows 11 setup

Install these prerequisites:

1. Git for Windows.
2. 64-bit Python 3.11 or newer from python.org. Enable the Python Launcher.
3. FFmpeg with both `ffmpeg.exe` and `ffprobe.exe` on `PATH`.
4. For real generation, NVIDIA drivers, a CUDA-compatible ComfyUI installation,
   Flux Klein 4B, Wan 2.2, and the exact custom nodes used by the qualified
   workflow exports.

Open PowerShell in a normal user account:

```powershell
git clone YOUR_REPOSITORY_URL vertical-video-studio
cd vertical-video-studio
git switch codex/capability-router-vast-ready
Set-ExecutionPolicy -Scope Process Bypass
.\packaging\windows\setup-headless.ps1
.\packaging\windows\start-media-mcp.ps1
```

The persistent MCP endpoint is `http://127.0.0.1:8766/mcp`. Runtime data is
stored under `%LOCALAPPDATA%\VerticalVideoStudio` by default. Keep the service
bound to `127.0.0.1`; it has no public-network authentication layer.

To use another data directory or port:

```powershell
.\packaging\windows\start-media-mcp.ps1 `
  -DataDirectory "D:\VerticalMediaData" -Port 8766
```

For an offline destination, first populate `wheelhouse` on an internet-connected
Windows machine with the same Python version and architecture, copy the checkout,
then run:

```powershell
.\packaging\windows\setup-headless.ps1 -Offline -Wheelhouse .\wheelhouse
```

## WSL2 alternative

Install WSL2 and Ubuntu from an elevated PowerShell prompt:

```powershell
wsl --install -d Ubuntu
```

Restart if requested, open Ubuntu, then run:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg
git clone YOUR_REPOSITORY_URL vertical-video-studio
cd vertical-video-studio
git switch codex/capability-router-vast-ready
./scripts/setup.sh
VVS_EXECUTION_MODE=simulation .venv/bin/vertical-studio-mcp \
  --transport streamable-http --host 127.0.0.1 --port 8766
```

Windows clients normally reach the WSL2 localhost endpoint at
`http://127.0.0.1:8766/mcp`. If ComfyUI runs on Windows while the MCP backend
runs in WSL2, set `VVS_COMFY_URL` to the Windows host address visible from WSL2
and verify `curl "$VVS_COMFY_URL/system_stats"` before real qualification.

## OpenCode configuration

Use HTTP for a durable service whose jobs survive OpenCode restarts. Add a remote
MCP server named `vertical-media-production` with this URL:

```text
http://127.0.0.1:8766/mcp
```

The exact OpenCode configuration key can vary by OpenCode release. Confirm it
with `opencode mcp --help`, then use the Streamable HTTP server form for the URL
above.

For stdio, do not start the HTTP launcher. Configure OpenCode to launch:

```text
C:\path\to\vertical-video-studio\.venv\Scripts\python.exe
```

with arguments:

```text
-m vertical_studio.mcp_server --transport stdio
```

Set `VVS_DATA_DIR` and `VVS_EXECUTION_MODE=simulation` in the MCP process
environment. Stdio is convenient for one client; HTTP is preferred because the
service and its asynchronous queue remain alive independently of OpenCode.

## First backend-only smoke test

1. Call `media_list_capabilities`. In simulation, every v1 capability reports
   `qualified`; in real mode, missing and unqualified workflows remain visible.
2. Call `media_generate_image` with a prompt, portrait dimensions, and a unique
   idempotency key.
3. Poll `media_get_operation` until it reaches `succeeded`.
4. Inspect the returned artifact with `media_get_artifact`.
5. Start a production session with `production_start`. OpenCode should process
   the first `ready` role task and persist each typed handoff with
   `production_update_task`.

Every mutating generation, retry, render, import, and session-start request uses
an idempotency key. Reusing a key with different inputs is rejected.

## Real-mode preparation

Set the service environment before starting it:

```powershell
$env:VVS_COMFY_URL = "http://127.0.0.1:8188"
$env:VVS_EXECUTION_MODE = "real"
.\packaging\windows\start-media-mcp.ps1 -Mode real
```

Real mode intentionally rejects generation until each workflow is independently
qualified. Export ComfyUI workflows in API format, install the expected files in
`workflows`, provide reviewed node maps, and run guarded qualification. Never
paste workflow JSON or node IDs into MCP generation calls; only registered input
slots are accepted.

## Operations and recovery

The headless tables live in the existing managed SQLite database, while media is
stored below `VVS_DATA_DIR\headless`. The service requeues an operation that was
`running` when the process stopped. Completed artifacts, failures, cancellation
records, approval decisions, task handoffs, and event history remain intact.

Use `production_emergency_stop` to interrupt the current ComfyUI prompt, cancel
queued generation, pause the session, and preserve evidence. An explicit
`production_resume` is required afterward. A cancelled operation can only be
recreated through `media_retry_operation` or `production_retry_cancelled` with a
new idempotency key.
