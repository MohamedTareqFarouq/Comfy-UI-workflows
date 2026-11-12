#!/usr/bin/env bash
# Idempotent installer for Vertical Media Studio on an existing Ubuntu host.
#
# Unlike the original fresh-Vast bootstrap this script:
#   * reuses an existing Python/ComfyUI/Ollama/OpenCode installation,
#   * never replaces system Python or the NVIDIA driver,
#   * downloads only the requested ComfyUI model groups (default: flux + wan),
#   * configures Hermes 3 (text) and Qwen3-VL (vision) through Ollama,
#   * writes start scripts for a systemd-less container or VM.
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
  TARGET_USER="${SUDO_USER:-root}"
else
  SUDO="sudo"
  TARGET_USER="${USER}"
fi

ROOT="${VVS_INSTALL_ROOT:-/opt/vertical-media}"
REPO_URL="${VVS_REPO_URL:-}"
APP_SOURCE="${VVS_APP_SOURCE:-}"
BRANCH="${VVS_BRANCH:-codex/capability-router-vast-ready}"
COMFY_DIR="${VVS_COMFY_DIR:-$ROOT/ComfyUI}"
APP_DIR="${VVS_APP_DIR:-$ROOT/vertical-video-studio}"
DATA_DIR="${VVS_DATA_DIR:-$ROOT/data}"
MODE="${VVS_EXECUTION_MODE:-real}"
MODEL_GROUPS="${VVS_MODEL_GROUPS:-flux wan wan-lightning}"
PYTHON_BIN="${VVS_PYTHON:-python3}"
OLLAMA_MODELS="${OLLAMA_MODELS:-/root/.ollama/models}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

log() { printf '==> %s\n' "$*"; }

log "Installing host packages (already-present packages are untouched)"
$SUDO apt-get update -qq
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates curl git jq ripgrep ffmpeg build-essential aria2 zstd

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
PY
if ! "$PYTHON_BIN" -c 'import ensurepip' >/dev/null 2>&1; then
  PY_VENV_PKG="$("$PYTHON_BIN" -c 'import sys; print(f"python3.{sys.version_info[1]}-venv")')"
  log "Installing $PY_VENV_PKG for venv support"
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$PY_VENV_PKG"
fi

$SUDO mkdir -p "$ROOT" "$ROOT/logs" "$DATA_DIR"
$SUDO chown -R "$TARGET_USER":"$TARGET_USER" "$ROOT"

if ! command -v nvidia-smi >/dev/null; then
  echo "WARNING: nvidia-smi is unavailable; ComfyUI will not have GPU acceleration." >&2
fi

# ---------------------------------------------------------------- ComfyUI ----
if [[ ! -d "$COMFY_DIR/.git" ]]; then
  log "Cloning ComfyUI"
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
fi
if [[ ! -x "$COMFY_DIR/.venv/bin/python" ]]; then
  log "Creating ComfyUI virtualenv"
  "$PYTHON_BIN" -m venv "$COMFY_DIR/.venv"
fi
if [[ ! -f "$COMFY_DIR/.venv/.vvs-deps" ]]; then
  log "Installing ComfyUI dependencies (this downloads PyTorch)"
  "$COMFY_DIR/.venv/bin/pip" install --upgrade pip wheel
  "$COMFY_DIR/.venv/bin/pip" install -r "$COMFY_DIR/requirements.txt"
  touch "$COMFY_DIR/.venv/.vvs-deps"
fi
mkdir -p "$COMFY_DIR"/models/{diffusion_models,text_encoders,vae,loras,checkpoints,clip,unet}

# ------------------------------------------------------------ application ----
if [[ -n "$APP_SOURCE" && -d "$APP_SOURCE" ]]; then
  log "Syncing application checkout from $APP_SOURCE"
  mkdir -p "$APP_DIR"
  if command -v rsync >/dev/null; then
    rsync -a --exclude .venv "$APP_SOURCE/" "$APP_DIR/"
  else
    tar -C "$APP_SOURCE" --exclude=.venv -cf - . | tar -C "$APP_DIR" -xf -
  fi
elif [[ ! -d "$APP_DIR/.git" ]]; then
  if [[ -z "$REPO_URL" ]]; then
    echo "Set VVS_REPO_URL or VVS_APP_SOURCE, or place a checkout at $APP_DIR." >&2
    exit 2
  fi
  log "Cloning application"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
if git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$APP_DIR" switch "$BRANCH" >/dev/null 2>&1 || true
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  log "Creating application virtualenv"
  "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
fi
if ! "$APP_DIR/.venv/bin/python" -c 'import vertical_studio' >/dev/null 2>&1; then
  log "Installing application dependencies"
  "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.lock"
  "$APP_DIR/.venv/bin/pip" install --no-deps -e "$APP_DIR"
fi

# ---------------------------------------------------------------- Ollama -----
if ! command -v ollama >/dev/null; then
  log "Installing Ollama"
  curl -fsSL https://ollama.com/install.sh | sh
fi
if ! curl -sf -m 3 "$OLLAMA_URL/api/version" >/dev/null; then
  log "Starting Ollama server on $OLLAMA_URL"
  nohup env OLLAMA_HOST=0.0.0.0:11434 OLLAMA_MODELS="$OLLAMA_MODELS" \
    OLLAMA_KEEP_ALIVE=30m ollama serve > "$ROOT/logs/ollama.log" 2>&1 &
  for _ in $(seq 1 60); do
    curl -sf -m 2 "$OLLAMA_URL/api/version" >/dev/null && break
    sleep 1
  done
fi
curl -sf -m 3 "$OLLAMA_URL/api/version" >/dev/null || {
  echo "Ollama failed to start; see $ROOT/logs/ollama.log" >&2
  exit 1
}
for model in hermes3:8b qwen3-vl:8b; do
  if ! ollama list | awk '{print $1}' | grep -qx "$model"; then
    log "Pulling $model"
    ollama pull "$model"
  fi
done
log "Building tuned controller and vision profiles"
ollama create hermes3-t4 -f "$APP_DIR/packaging/ollama/Modelfile.hermes3"
ollama create qwen3vl-t4 -f "$APP_DIR/packaging/ollama/Modelfile.qwen3vl"

# ------------------------------------------------------------- OpenCode ------
if ! command -v opencode >/dev/null && [[ ! -x "$HOME/.opencode/bin/opencode" ]]; then
  log "Installing OpenCode"
  curl -fsSL https://opencode.ai/v2/install | bash
fi
export PATH="$HOME/.opencode/bin:$PATH"
OPENCODE_BIN="$(command -v opencode || echo "$HOME/.opencode/bin/opencode")"

# ---------------------------------------------------------- model manifest ---
if [[ "${VVS_SKIP_MODELS:-0}" != "1" ]]; then
  for group in $MODEL_GROUPS; do
    log "Installing ComfyUI model group: $group"
    "$APP_DIR/scripts/install-model-manifest.sh" \
      "${VVS_MODEL_MANIFEST:-$APP_DIR/config/models.json}" "$COMFY_DIR" "$group"
  done
else
  log "Skipping ComfyUI model downloads (VVS_SKIP_MODELS=1)"
fi

# ------------------------------------------------------------- runtime env ---
log "Writing $ROOT/env"
cat > "$ROOT/env" <<ENV
export VVS_DATA_DIR="$DATA_DIR"
export VVS_EXECUTION_MODE="$MODE"
export VVS_COMFY_URL="http://127.0.0.1:8188"
export VVS_MODEL_PROVIDER="ollama"
export VVS_LLAMA_URL="$OLLAMA_URL"
export VVS_CONTROLLER_MODEL="hermes3-t4:latest"
export VVS_VISION_MODEL="qwen3vl-t4:latest"
export VVS_QWEN_MODEL="qwen3vl-t4:latest"
export VVS_OLLAMA_EVICT="true"
export VVS_ENABLE_QWEN_IMAGE="false"
export VVS_KEYFRAME_IMAGE_BACKEND="klein"
export VVS_ALLOW_UNQUALIFIED_CAPABILITIES="${VVS_ALLOW_UNQUALIFIED_CAPABILITIES:-false}"
ENV

cat > "$ROOT/start-comfy.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
export CUDA_VISIBLE_DEVICES="\${VVS_COMFY_GPU:-0}"
exec "$COMFY_DIR/.venv/bin/python" "$COMFY_DIR/main.py" --listen 127.0.0.1 --port 8188
SH

cat > "$ROOT/start-studio.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
set -a
source "$ROOT/env"
set +a
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/vertical-studio" serve "\$@"
SH

cat > "$ROOT/start-opencode.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
set -a
source "$ROOT/env"
set +a
cd "$APP_DIR"
exec "$OPENCODE_BIN" "\$@"
SH

cat > "$ROOT/start-mcp.sh" <<SH
#!/usr/bin/env bash
# Expose the media MCP over Streamable HTTP for external/Hermes clients.
set -euo pipefail
set -a
source "$ROOT/env"
set +a
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" -m vertical_studio.mcp_server \\
  --transport streamable-http --host 127.0.0.1 --port "\${VVS_MCP_PORT:-8766}" "\$@"
SH

cat > "$ROOT/free-vram.sh" <<SH
#!/usr/bin/env bash
# Ask Ollama to evict both tuned models and print the resulting GPU memory.
set -euo pipefail
for model in hermes3-t4:latest qwen3vl-t4:latest; do
  curl -sf "$OLLAMA_URL/api/generate" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"\$model\",\"keep_alive\":0}" >/dev/null || true
done
nvidia-smi --query-gpu=index,memory.used --format=csv
SH

cat > "$ROOT/start-stack.sh" <<SH
#!/usr/bin/env bash
# Start ComfyUI in the background, then the studio UI in the foreground.
set -euo pipefail
ROOT="$ROOT"
nohup "\$ROOT/start-comfy.sh" > "\$ROOT/logs/comfy.log" 2>&1 &
for _ in \$(seq 1 120); do
  curl -sf http://127.0.0.1:8188/system_stats >/dev/null && break
  sleep 2
done
exec "\$ROOT/start-studio.sh" "\$@"
SH

chmod +x "$ROOT"/start-*.sh "$ROOT"/free-vram.sh

if [[ "$MODE" == "real" && "${VVS_SKIP_PREFLIGHT:-0}" != "1" ]]; then
  log "Running real-mode preflight"
  "$APP_DIR/scripts/real-mode-preflight.sh" "$COMFY_DIR"
fi

echo
echo "Installation complete."
echo "  Environment : $ROOT/env"
echo "  ComfyUI     : $ROOT/start-comfy.sh   (http://127.0.0.1:8188)"
echo "  Studio UI   : $ROOT/start-studio.sh  (http://127.0.0.1:8765)"
echo "  OpenCode    : $ROOT/start-opencode.sh"
echo "  MCP (HTTP)  : $ROOT/start-mcp.sh      (http://127.0.0.1:8766/mcp)"
echo "  Free VRAM   : $ROOT/free-vram.sh"
echo "OpenCode is configured for Ollama Hermes 3 (text) and Qwen3-VL (vision)."
echo "Run ComfyUI, then: cd $APP_DIR && ./scripts/qualify.sh smoke"
