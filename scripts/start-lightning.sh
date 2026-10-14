#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export VVS_USE_ACTIVE_ENV=1
export VVS_COMFY_URL="${VVS_COMFY_URL:-http://127.0.0.1:8989}"
export VVS_DATA_DIR="${VVS_DATA_DIR:-$ROOT/data-real}"
export VVS_EXPORT_DIR="${VVS_EXPORT_DIR:-$VVS_DATA_DIR/exports}"
export VVS_QWEN_MODEL="${VVS_QWEN_MODEL:-Qwen3-VL-4B-Instruct-GGUF}"
export VVS_QWEN_HF_REPO="${VVS_QWEN_HF_REPO:-Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M}"
export VVS_QWEN_CONTEXT_SIZE="${VVS_QWEN_CONTEXT_SIZE:-16384}"
exec "$ROOT/start.sh" --real --managed-qwen --host 0.0.0.0 "$@"
