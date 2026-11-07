# OpenCode producer and Vast.ai bootstrap

The project includes a primary OpenCode agent at `.opencode/agents/vertical-producer.md`.
It chooses whether to analyze, plan, generate, repair, render, or finish and uses the
typed `vertical-media-production` MCP tools and has unrestricted shell and filesystem
access for diagnosis and project work. Start OpenCode from the repository root.

## Vast.ai / fresh Ubuntu

Choose a current Ubuntu CUDA image whose NVIDIA runtime works, then:

```bash
export VVS_REPO_URL="YOUR_GIT_URL"
export VVS_BRANCH="codex/capability-router-vast-ready"
curl -O YOUR_RAW_REPOSITORY_URL/packaging/vastai/setup-ubuntu.sh
chmod +x setup-ubuntu.sh
./setup-ubuntu.sh
```

The default is simulation, which is fully usable immediately. Start ComfyUI and OpenCode in
separate terminals using the commands printed by the installer.

By default the Vast installer downloads all nine Flux Klein and Wan/LightX2V files listed in `config/models.json`. Set `VVS_SKIP_MODELS=1` only for simulation-only installations. Set `HF_TOKEN` when the model host requires authentication.

For a custom mirror, supply a JSON model manifest and the reviewed API exports/node map:

```json
[
  {
    "path": "diffusion_models/model.safetensors",
    "url": "https://authorized-host/model.safetensors",
    "sha256": "optional-sha256"
  }
]
```

Then set `VVS_MODEL_MANIFEST` only when overriding the default manifest, and set `VVS_EXECUTION_MODE=real` before running the installer.
Real mode deliberately fails preflight when NVIDIA, models, API workflow exports, or reviewed
node maps are missing. Run `scripts/qualify.sh smoke` after ComfyUI starts.

OpenCode still requires a configured reasoning-model provider. Run `/connect` in OpenCode or
provide the relevant provider environment variable. The media models used by ComfyUI are
separate from the reasoning model used by OpenCode.

## Recommended Vast.ai instance

Use the Vast base CUDA image rather than a stock Ubuntu image:

```text
vastai/base-image:cuda-12.8.1-cudnn-devel-ubuntu24.04-py312
```

If Vast offers an `[Automatic]` CUDA tag, choose the Ubuntu 24.04 / Python 3.12
variant compatible with the selected host. The installer requires Python 3.11 or
newer. Avoid Python 3.10 images.

Recommended offer:

| Resource | Minimum | Recommended |
| --- | ---: | ---: |
| GPU VRAM | 16 GB with offloading | 24 GB |
| GPU | RTX 4080 16 GB | RTX 3090/4090, A5000, or better |
| System RAM | 32 GB | 64 GB |
| Container disk | 100 GB | 150 GB |
| Internet | 300 Mbps | 500 Mbps or faster |

The nine ComfyUI models use approximately 45-55 GB. ComfyUI, Python environments,
OpenCode, caches, temporary files, and outputs need additional capacity. A 48 GB
GPU such as an RTX A6000 or L40 is preferable for Wan 14B if the price is acceptable.

## Vast.ai console setup

1. Add your SSH public key to the Vast account.
2. Search offers for a 24 GB or larger NVIDIA GPU.
3. Set disk space to 150 GB.
4. Select the CUDA 12.8 / Ubuntu 24.04 / Python 3.12 base image above.
5. Use SSH launch mode. Port 22 is mapped automatically.
6. Rent the instance and copy its SSH command from **Connect**.
7. Connect and verify the runtime:

   ```bash
   nvidia-smi
   python3 --version
   df -h
   ```

8. Clone this repository, or export its URL for the bootstrap:

   ```bash
   export VVS_REPO_URL="https://YOUR_GIT_HOST/YOUR_ACCOUNT/YOUR_REPOSITORY.git"
  export VVS_BRANCH="codex/capability-router-vast-ready"
   export VVS_INSTALL_ROOT="/opt/vertical-media"
   export VVS_EXECUTION_MODE="simulation"
   ```

9. Run the installer from the checkout:

   ```bash
   git clone --branch "$VVS_BRANCH" "$VVS_REPO_URL" /tmp/vertical-bootstrap
   cd /tmp/vertical-bootstrap
   ./packaging/vastai/setup-ubuntu.sh
   ```

The default install downloads all models. Set `HF_TOKEN` first if Hugging Face
requires authentication for the Flux repository:

```bash
export HF_TOKEN="YOUR_HUGGING_FACE_TOKEN"
```

To test the controller without downloading approximately 45-55 GB:

```bash
export VVS_SKIP_MODELS=1
export VVS_EXECUTION_MODE=simulation
```

## Start and access the applications

On the Vast instance, use two terminal sessions or a `tmux` session:

```bash
/opt/vertical-media/start-comfy.sh
/opt/vertical-media/start-opencode.sh
```

The configured OpenCode agent starts the media MCP backend automatically over
stdio. No separate MCP-server terminal is required.

## Debug the MCP server

Start an isolated simulation server in a separate terminal so its stderr stays
visible and debug data does not mix with production data:

```bash
VVS_EXECUTION_MODE=simulation \
VVS_DATA_DIR="$PWD/data-mcp-debug" \
"$PWD/.venv/bin/python" -m vertical_studio.mcp_server --transport streamable-http \
  --host 127.0.0.1 --port 8766
```

Connect an MCP Inspector to `http://127.0.0.1:8766/mcp`, or configure a
temporary OpenCode HTTP MCP entry pointing there. HTTP transport sessions are
enabled by default; pass `--stateless-http` only when testing stateless clients.
The OpenCode project configuration uses stdio instead, so its server errors are
reported by the OpenCode MCP process/logs.

Every MCP call is also written as JSON Lines to
`$VVS_DATA_DIR/logs/mcp-calls.jsonl`. If `VVS_DATA_DIR` is unset, the default is
`~/.local/share/vertical-video-studio/logs/mcp-calls.jsonl`. Watch the live trace
in another terminal with:

```bash
tail -f "${VVS_DATA_DIR:-$HOME/.local/share/vertical-video-studio}/logs/mcp-calls.jsonl"
```

Each call has a `trace_id`, tool name, arguments, result identifiers or error,
and duration. Sensitive-looking argument fields such as tokens and API keys are
redacted; prompts and local file paths are retained for debugging. The
`mcp_get_recent_calls` tool returns recent records to the agent. When a call
fails, its error includes the trace ID; use that ID to find the matching
`started` and `failed` records. Queued generation errors are recorded on the
operation, so inspect it with `media_get_operation` and read its `failure` field.

Tool input schemas include field descriptions, allowed enum values, and numeric
bounds. Argument validation errors happen before a tool runs, but are still
recorded with a trace ID. The trace query itself is read-only and is not added
to its own trace.

Before generation, call `mcp_get_status`. It reports the MCP process's actual
`execution_mode`, selected backend, ComfyUI reachability, and qualified
capabilities. In simulation, generation uses a local mock and never contacts
ComfyUI; queued operations and artifacts are marked `backend=mock_simulator`
and `simulated=true`. Real ComfyUI artifacts are marked `backend=comfyui` and
include the ComfyUI `prompt_id`. Do not describe a result as a ComfyUI
generation unless the artifact metadata confirms that provenance.

Smoke-test the registered tools in order: call `media_list_capabilities`, call
`production_start` with a fresh idempotency key, retain the returned session
ID, then call `production_get_situation` and `production_inspect` with that ID.
For generation, pass the same value as `production_session_id` and poll the
returned operation with `media_get_operation`. This distinguishes MCP transport
initialization/session errors from application errors in a production
operation. Record the exact tool name, arguments, error text, operation ID, and
the server stderr before retrying. For real-mode failures, also check the
ComfyUI log and rerun `scripts/real-mode-preflight.sh` and
`VVS_EXECUTION_MODE=real ./scripts/qualify.sh smoke`.

ComfyUI is intentionally bound to localhost. From your own computer, use the SSH
host and port shown by Vast:

```bash
ssh -N \
  -p VAST_SSH_PORT \
  -L 8188:127.0.0.1:8188 \
  root@VAST_PUBLIC_IP
```

Open <http://127.0.0.1:8188> locally for ComfyUI.

For OpenCode, the simplest interface is its terminal UI over SSH. To use the web
interface, run `opencode pair` on the instance. It prints the exact local port
and a temporary connection URL. Add that printed port to the SSH tunnel, for
example:

```bash
ssh -N \
  -p VAST_SSH_PORT \
  -L 8188:127.0.0.1:8188 \
  -L 49374:127.0.0.1:49374 \
  root@VAST_PUBLIC_IP
```

Replace `49374` if `opencode pair` prints another port. SSH forwarding is
preferred to exposing ComfyUI or OpenCode publicly.

## Reference-video recreation

OpenCode's web chat accepts still-image attachments, but it does not pass video
or audio attachments to a model. For a video reference, start the VVS browser
application on the instance and forward its local-only port in the same SSH
tunnel:

```bash
cd /opt/vertical-media/vertical-video-studio
VVS_EXECUTION_MODE=real ./start.sh --host 127.0.0.1 --port 8765
```

```bash
ssh -N -p VAST_SSH_PORT \
  -L 8188:127.0.0.1:8188 \
  -L 8765:127.0.0.1:8765 \
  -L 49374:127.0.0.1:49374 \
  root@VAST_PUBLIC_IP
```

Open `http://127.0.0.1:8765`, create a project with **Start from a reference
video**, and select the local MP4, MOV, WebM, MKV, or M4V file. The service
accepts an approximately 10–30 second video (up to 250 MB), copies it into
managed Vast storage, samples evidence frames, and performs reference analysis
before it creates the storyboard and Wan generation plan. The OpenCode MCP
server remains unchanged: it is started automatically when OpenCode starts.

For still-image visual planning, pair OpenCode as above, open its local web UI,
and drag the image into the prompt using a vision-capable model. Uploading an
image to OpenCode is separate from registering it as a generation reference;
use the VVS project workflow or `media_import_reference` for the latter.

## Enable real generation

After every model download completes:

1. Install the reviewed `workflow_maps.json` and required API workflow exports.
2. Start ComfyUI and confirm <http://127.0.0.1:8188/system_stats> responds.
3. Run:

   ```bash
   cd /opt/vertical-media/vertical-video-studio
   ./scripts/real-mode-preflight.sh /opt/vertical-media/ComfyUI
   VVS_EXECUTION_MODE=real ./scripts/qualify.sh smoke
   ```

4. Change `VVS_EXECUTION_MODE` in `/opt/vertical-media/env` to `real` only
   after qualification succeeds.
