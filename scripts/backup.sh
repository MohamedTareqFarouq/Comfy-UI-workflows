#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/runtime.sh"
vvs_select_runtime "$ROOT"
exec "$VVS_PYTHON_BIN" -m vertical_studio.cli backup "$@"
