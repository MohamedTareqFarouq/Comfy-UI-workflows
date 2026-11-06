#!/usr/bin/env bash
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
BRANCH="${VVS_BRANCH:-codex/capability-router}"
COMFY_DIR="${VVS_COMFY_DIR:-$ROOT/ComfyUI}"
APP_DIR="${VVS_APP_DIR:-$ROOT/vertical-video-studio}"
DATA_DIR="${VVS_DATA_DIR:-$ROOT/data}"
MODE="${VVS_EXECUTION_MODE:-simulation}"
PYTHON_BIN="${VVS_PYTHON:-python3}"

$SUDO apt-get update
$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git jq ripgrep ffmpeg python3 python3-venv python3-pip \
  build-essential aria2 software-properties-common

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  $SUDO add-apt-repository -y ppa:deadsnakes/ppa
  $SUDO apt-get update
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    python3.11 python3.11-venv python3.11-dev
  PYTHON_BIN=python3.11
fi

"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"'

$SUDO mkdir -p "$ROOT"
$SUDO chown -R "$TARGET_USER":"$TARGET_USER" "$ROOT"

if ! command -v nvidia-smi >/dev/null; then
  echo "WARNING: nvidia-smi is unavailable. Select a Vast.ai CUDA image with NVIDIA runtime support." >&2
fi

if [[ ! -d "$COMFY_DIR/.git" ]]; then
  git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$COMFY_DIR"
fi
"$PYTHON_BIN" -m venv "$COMFY_DIR/.venv"
"$COMFY_DIR/.venv/bin/pip" install --upgrade pip wheel
"$COMFY_DIR/.venv/bin/pip" install -r "$COMFY_DIR/requirements.txt"

if [[ -n "$REPO_URL" && ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
elif [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Set VVS_REPO_URL to this repository URL, or place the checkout at $APP_DIR." >&2
  exit 2
fi
git -C "$APP_DIR" switch "$BRANCH"
"$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

if ! command -v opencode >/dev/null; then
  curl -fsSL https://opencode.ai/v2/install | bash
fi
export PATH="$HOME/.opencode/bin:$PATH"
OPENCODE_BIN="$(command -v opencode)"

mkdir -p "$DATA_DIR" "$ROOT/logs"
cat > "$ROOT/env" <<ENV
export VVS_DATA_DIR="$DATA_DIR"
export VVS_EXECUTION_MODE="$MODE"
export VVS_COMFY_URL="http://127.0.0.1:8188"
ENV

MODEL_MANIFEST="${VVS_MODEL_MANIFEST:-$APP_DIR/config/models.json}"
if [[ "${VVS_SKIP_MODELS:-0}" != "1" ]]; then
  "$APP_DIR/scripts/install-model-manifest.sh" "$MODEL_MANIFEST" "$COMFY_DIR" all
fi
cat > "$ROOT/start-comfy.sh" <<SH
#!/usr/bin/env bash
exec "$COMFY_DIR/.venv/bin/python" "$COMFY_DIR/main.py" --listen 127.0.0.1 --port 8188
SH
cat > "$ROOT/start-opencode.sh" <<SH
#!/usr/bin/env bash
set -a
source "$ROOT/env"
set +a
cd "$APP_DIR"
exec "$OPENCODE_BIN"
SH
chmod +x "$ROOT/start-comfy.sh" "$ROOT/start-opencode.sh"

if [[ "$MODE" == "real" ]]; then
  "$APP_DIR/scripts/real-mode-preflight.sh" "$COMFY_DIR"
fi

echo "Installation complete."
echo "Terminal 1: $ROOT/start-comfy.sh"
echo "Terminal 2: $ROOT/start-opencode.sh"
echo "After ComfyUI is healthy: cd $APP_DIR && ./scripts/qualify.sh smoke"
echo "OpenCode will load the vertical-producer agent and local MCP server."
