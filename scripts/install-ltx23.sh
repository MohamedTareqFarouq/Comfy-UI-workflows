#!/usr/bin/env bash
set -euo pipefail

COMFY_DIR="${1:?usage: install-ltx23.sh COMFYUI_DIR}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_DIR="$COMFY_DIR/custom_nodes/ComfyUI-LTXVideo"

[[ -x "$COMFY_DIR/.venv/bin/python" ]] || {
  echo "ComfyUI virtual environment is missing: $COMFY_DIR/.venv" >&2
  exit 1
}

if [[ ! -d "$NODE_DIR/.git" ]]; then
  git clone --depth 1 https://github.com/Lightricks/ComfyUI-LTXVideo.git "$NODE_DIR"
else
  git -C "$NODE_DIR" pull --ff-only
fi

if [[ -f "$NODE_DIR/requirements.txt" ]]; then
  "$COMFY_DIR/.venv/bin/pip" install -r "$NODE_DIR/requirements.txt"
fi

"$ROOT/scripts/install-model-manifest.sh" \
  "$ROOT/config/models-ltx23.json" "$COMFY_DIR" ltx23

echo "LTX 2.3 nodes and base FP8 model pack are installed."
echo "Restart ComfyUI, calibrate the official 2.3 distilled I2V workflow, then export API JSON."
