#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export VVS_USE_ACTIVE_ENV=1
export VVS_COMFY_URL="${VVS_COMFY_URL:-http://127.0.0.1:8989}"
export VVS_DATA_DIR="${VVS_DATA_DIR:-$ROOT/data-real}"
export VVS_EXPORT_DIR="${VVS_EXPORT_DIR:-$VVS_DATA_DIR/exports}"
exec "$ROOT/start.sh" --real --managed-qwen --host 0.0.0.0 "$@"
