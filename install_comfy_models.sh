#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFY="${1:-$HOME/ComfyUI}"
GROUP="${2:-all}"
exec "$ROOT/scripts/install-model-manifest.sh" "$ROOT/config/models.json" "$COMFY" "$GROUP"
