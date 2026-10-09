#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/runtime.sh"
vvs_select_runtime "$ROOT"

LEVEL="${1:-diagnostics}"
case "$LEVEL" in
  diagnostics)
    export VVS_EXECUTION_MODE="${VVS_EXECUTION_MODE:-real}"
    exec "$VVS_PYTHON_BIN" -m vertical_studio.cli qualify diagnostics
    ;;
  smoke|full)
    export VVS_EXECUTION_MODE=real
    exec "$VVS_PYTHON_BIN" -m vertical_studio.cli qualify "$LEVEL" --confirm-real-generation
    ;;
  *)
    echo "Usage: $0 [diagnostics|smoke|full]" >&2
    exit 2
    ;;
esac
