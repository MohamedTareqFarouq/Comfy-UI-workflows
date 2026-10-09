#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/runtime.sh"
vvs_select_runtime "$ROOT"
cd "$ROOT"
"$VVS_PYTHON_BIN" -m compileall -q src tests
if command -v node >/dev/null 2>&1; then
  node --check src/vertical_studio/static/app.js
fi
"$VVS_PYTHON_BIN" -m pytest -o addopts="" -ra
TEST_DATA="$(mktemp -d)"
trap 'rm -rf "$TEST_DATA"' EXIT
VVS_DATA_DIR="$TEST_DATA" VVS_EXECUTION_MODE=simulation \
  "$VVS_PYTHON_BIN" -m vertical_studio.cli diagnose
