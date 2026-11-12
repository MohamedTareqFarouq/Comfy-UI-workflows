# Vast.ai recovery and reinstall

Use this runbook when recreating the production machine from a fresh Vast.ai
instance. Prefer an Ubuntu 24.04 image with Python 3.12. Ubuntu 22.04 ships
Python 3.10, so it requires the Python 3.11 workaround below.

## 1. Connect and clone

```bash
ssh -p VAST_PORT root@VAST_IP

cd /tmp
git clone -b codex/capability-router-vast-ready \
  https://github.com/MohamedTareqFarouq/Comfy-UI-workflows.git vertical-bootstrap
cd vertical-bootstrap
```

## 2. Correct the model source

The installer in this branch is idempotent and safe on an existing host: it
reuses Python, ComfyUI, Ollama, and OpenCode, and downloads only the requested
model groups (`VVS_MODEL_GROUPS`, default `flux wan wan-lightning`). It also
configures Hermes 3 and Qwen3-VL through Ollama; see
[HERMES_OLLAMA.md](HERMES_OLLAMA.md). The Qwen Image 2.1 files are optional and
live in `config/models-qwen-image.json`.

The Flux.2 Klein workflow requires the Flux.2-packaged Qwen encoder. It must not
be downloaded from the Z-Image repository.

```bash
sed -i \
  's#Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors#Comfy-Org/flux2-klein/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors#' \
  config/models.json
```

Also apply the Ubuntu apt environment fix when using an older checkout:

```bash
sed -i \
  's/$SUDO DEBIAN_FRONTEND=noninteractive/$SUDO env DEBIAN_FRONTEND=noninteractive/' \
  packaging/vastai/setup-ubuntu.sh
```

## 3. Ubuntu 22.04: create the application environment with Python 3.11

Skip this section on Ubuntu 24.04 when `python3 --version` is already 3.11 or
newer.

```bash
apt-get update
apt-get install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update
apt-get install -y python3.11 python3.11-venv python3.11-dev

cd /tmp/vertical-bootstrap
mv .venv .venv-py310-failed 2>/dev/null || true
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/python -m pip install -e .
```

Verify the environment before continuing:

```bash
.venv/bin/python --version
.venv/bin/python -c 'import vertical_studio; print("MCP ready")'
```

The application environment must report Python 3.11 or newer. ComfyUI may use
its own separate Python 3.10 environment.

## 4. Run or resume the installer

```bash
export VVS_APP_DIR=/tmp/vertical-bootstrap
export VVS_INSTALL_ROOT=/opt/vertical-media
export VVS_BRANCH=codex/capability-router-vast-ready
export VVS_PYTHON="$(command -v python3.11 || command -v python3)"
export PYTHON_BIN="$VVS_PYTHON"

./packaging/vastai/setup-ubuntu.sh
```

The installer reuses the application `.venv` created above and downloads the
ComfyUI models. Downloads happen on the Vast server, not through the home
internet connection. Models must be downloaded again unless they are stored on
a persistent Vast volume.

## 5. Validate the GPU and services

```bash
nvidia-smi
.venv/bin/python -c 'import vertical_studio; print("MCP ready")'
```

If `nvidia-smi` reports `Driver/library version mismatch`, restart the entire
Vast instance. Do not install NVIDIA drivers inside the container. If the error
survives a full restart, move to another host.

Start the services in separate SSH terminals:

```bash
/opt/vertical-media/start-comfy.sh
```

```bash
/opt/vertical-media/start-opencode.sh
```

Keep the installer-created app directory consistent: with the settings above it
is `/tmp/vertical-bootstrap`. Run repository tests and static preflight there:

```bash
source /opt/vertical-media/env
cd /tmp/vertical-bootstrap
./run-tests.sh
./scripts/real-mode-preflight.sh /opt/vertical-media/ComfyUI
```

After ComfyUI is listening on `127.0.0.1:8188`, run diagnostics (no generation),
then the explicitly confirmed real smoke test:

```bash
VVS_EXECUTION_MODE=real ./scripts/qualify.sh diagnostics
./scripts/qualify.sh smoke
```

The smoke run does not use the OpenCode agent. It exercises Flux T2I, Klein
one-through-four-reference generation, Qwen Image 2.1 editing, Wan I2V, and Wan
FLF2V through the same adapters exposed by MCP. It performs real GPU generation
and consumes compute. The latest result is saved to
`/opt/vertical-media/data/qualification/latest.json`; an immutable report and
generated smoke artifacts are saved under the timestamped directory named in
that report. Only capabilities recorded as qualified are reported as qualified
by real-mode MCP. Technical reference analysis is separately available without
GPU qualification; Qwen visual QA remains distinct from technical QA.

Only after reviewing the smoke result, change `VVS_EXECUTION_MODE` from
`simulation` to `real` in `/opt/vertical-media/env`, then restart OpenCode so its
MCP server reads the qualified report and real-mode settings. Keep
`VVS_ALLOW_UNQUALIFIED_CAPABILITIES` disabled for normal production use.

To open the studio/dashboard from your local computer, run the app in another
SSH terminal after the mode switch:

```bash
source /opt/vertical-media/env
cd /tmp/vertical-bootstrap
./start.sh --host 127.0.0.1 --port 8765
```

Create an SSH tunnel from your local computer:

```bash
ssh -N -p VAST_PORT \
  -L 8765:127.0.0.1:8765 \
  -L 8188:127.0.0.1:8188 \
  root@VAST_IP
```

Open `http://127.0.0.1:8765/dashboard.html` for the live runtime/capability
panel and `http://127.0.0.1:8188` for ComfyUI. Keep both services bound to
localhost; use SSH forwarding rather than exposing their ports publicly.

If OpenCode cannot start the MCP server, ensure `opencode.json` points to:

```text
/tmp/vertical-bootstrap/.venv/bin/python
```

## 6. Verify the Klein encoder

Use the ComfyUI tunnel opened above. For Flux.2 Klein generation, the workflow's
`CLIPLoader` must use:

For Flux.2 Klein generation, the workflow's `CLIPLoader` must use:

```text
model: qwen_3_4b.safetensors
type:  flux2
```

The ComfyUI log should say `Requested to load Flux2TEModel_`. If it says
`ZImageTEModel_`, confirm that the encoder came from the Flux.2 Klein repository
and restart ComfyUI after replacing it.
