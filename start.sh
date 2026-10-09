#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/runtime.sh"

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --real) export VVS_EXECUTION_MODE=real; shift ;;
    --simulation) export VVS_EXECUTION_MODE=simulation; shift ;;
    --managed-qwen) export VVS_MANAGE_QWEN=true; shift ;;
    --no-managed-qwen) export VVS_MANAGE_QWEN=false; shift ;;
    --active-env) export VVS_USE_ACTIVE_ENV=1; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

vvs_select_runtime "$ROOT"
export VVS_EXECUTION_MODE="${VVS_EXECUTION_MODE:-simulation}"
if vvs_is_lightning; then
  export VVS_COMFY_URL="${VVS_COMFY_URL:-http://127.0.0.1:8989}"
  export VVS_DATA_DIR="${VVS_DATA_DIR:-$ROOT/data-real}"
  export VVS_EXPORT_DIR="${VVS_EXPORT_DIR:-$VVS_DATA_DIR/exports}"
fi
if [[ "${VVS_MANAGE_QWEN:-false}" == true ]]; then
  LLAMA_BIN="${VVS_LLAMA_EXECUTABLE:-llama}"
  if ! command -v "$LLAMA_BIN" >/dev/null 2>&1; then
    echo "Managed Qwen requested, but '$LLAMA_BIN' was not found." >&2
    exit 2
  fi
fi
exec "$VVS_PYTHON_BIN" -m vertical_studio.cli serve "${ARGS[@]}"
