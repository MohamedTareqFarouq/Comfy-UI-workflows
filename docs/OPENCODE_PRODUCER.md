# OpenCode producer and Vast.ai bootstrap

The project includes a primary OpenCode agent at `.opencode/agents/vertical-producer.md`.
It chooses whether to analyze, plan, generate, repair, render, or finish and uses the
typed `vertical-media-production` MCP tools and has unrestricted shell and filesystem
access for diagnosis and project work. Start OpenCode from the repository root.

The producer delegates bounded production stages to the subagents in
`.opencode/agents/`: `runtime-qualifier` checks readiness, `reference-analyst`
summarizes imported references, `shot-planner` produces the shot/continuity plan,
`asset-designer` creates image anchors, `video-director` generates Wan clips,
`qa-reviewer` evaluates artifacts, and `render-finisher` renders an explicitly
ordered approved sequence. The producer alone owns durable session/task state,
budgets, user approvals and artifact dispositions, recovery, and final delivery.
Each specialist has an MCP permission allowlist and is not permitted to change
those controller-owned decisions.

For reference-video recreation, the usual path is: import the source video into
managed storage; analyze it and inspect the completed analysis; plan shots and
continuity; create only necessary image anchors; generate shot clips; evaluate
the candidates; explicitly approve the selected artifacts; then render their
approved IDs in the intended order. Skip stages that the request and available
assets do not need. In real mode, readiness and capability qualification must
be confirmed before generation; queued work must be polled to a terminal state.
See [Vast recovery](VAST_RECOVERY.md) for server setup and
[reference-video recreation](VAST_RECOVERY.md#reference-video-recreation) for
the browser upload flow.
## Vast.ai / fresh Ubuntu
## Archived Nescafe recreation

The inspected [run archive](../nescafe_30s_recreation.zip) contains the source
screen recording, analysis boards, six extracted keyframes, six generated clips,
per-shot/final contact sheets, a manifest, logs, and the final render. The source
was a 65.4-second portrait screen recording with the desired 16:9 ad in its top
pane. The run sampled the ad at roughly two-second intervals, mapped its beats,
then used one 1024x576 start frame per shot.

All six successful shots used the real `wan22.i2v.v1` ComfyUI path with Wan 2.2
14B fp8 high/low-noise models, LightX2V four-step LoRAs, 16 fps, and one
candidate. Five clips were 81 frames; the ending was 69 frames. The final was
29.625 seconds, 1024x576, H.264, and silent. Contact sheets sampled every tenth
frame were reviewed manually because visual QA was unqualified. The basketball
clip passed weakly: the camera drifted to the hoop/fence and ended on the boy's
back.

The six independently generated I2V beats made a montage rather than a connected
chase. Future plans should include causal connective shots, incoming/outgoing
action states, screen direction and eyelines, and boundary-frame continuity when
a matching capability is qualified. Do not silently compress connective story
beats to meet a duration target; surface the tradeoff and budget options.

The run used nine video operations and generated 49.875 seconds against a
45-second generation budget because of retries. Its GPU generation took about
26.5 minutes; total wall time was about 55.5 minutes, including a ComfyUI/NVIDIA
driver mismatch recovery. Budget retries up front and pause for explicit budget
expansion rather than exceeding the session. The archive reports unloading and
reloading NVIDIA modules, but the supported recovery guidance in this runbook is
to restart the full Vast instance for a driver/library mismatch; do not repeat
that container-level module procedure by default.

The built-in `ffmpeg.sequence_render.v1` renderer outputs portrait 1080x1920 and
crops landscape clips. The archive correctly used an explicit FFmpeg concat at
native 16:9 and imported the final artifact. Check renderer constraints against
the target aspect ratio before rendering. The source audio was extracted but
not included, so confirm whether audio is part of the requested deliverable.

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

In the VVS app's reference-video path, the project request selects faithful
recreation or story adaptation; target visual style is handled independently.
The saved blueprint retains observed source style separately from any requested
target style, so approval does not replace an explicit visual direction with the
source look. In the standalone MCP path, carry this intent in the production
plan and generation prompts; its reference-analysis tool does not return the
app's structured blueprint fields.

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

For approvals, retain the exact `created_approval_id` returned by
`production_request_approval` and decide it with `production_resolve_approval`
using `decision="approved"` or `decision="rejected"`. This resolves the owning
session from the approval ID and safely returns the current state if the same
decision is repeated. Confirm the returned approval status before continuing.
If the older `production_approve` or `production_reject` call fails, refresh
with `production_list_approvals` and retry by approval ID; do not create a
duplicate request. Wrong-session, missing-ID, and already-decided errors now
state the specific problem.

If the MCP decision tool itself is unavailable, approve manually from an SSH
shell using the same installation environment and approval ID:

```bash
set -a
source /opt/vertical-media/env
set +a
cd /opt/vertical-media/vertical-video-studio
.venv/bin/vertical-studio approvals PRODUCTION_SESSION_ID
.venv/bin/vertical-studio approval APPROVAL_ID approved --reason "Approved by user"
```

Use `rejected` instead of `approved` to reject. The command prints the final
status and does not start the media worker. Confirm the session ID and exact
operation with the user before deciding; do not approve based only on a tool
failure.

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
