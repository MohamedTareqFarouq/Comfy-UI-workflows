#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$ROOT/.venv/bin/pytest" ]]; then
  echo "Test environment not found. Run ./scripts/setup.sh first." >&2
  exit 2
fi
cd "$ROOT"
"$ROOT/.venv/bin/python" -m compileall -q src tests
if command -v node >/dev/null 2>&1; then
  node --check src/vertical_studio/static/app.js
fi
"$ROOT/.venv/bin/pytest" -o addopts="" -ra
TEST_DATA="$(mktemp -d)"
trap 'rm -rf "$TEST_DATA"' EXIT
VVS_DATA_DIR="$TEST_DATA" VVS_EXECUTION_MODE=simulation   "$ROOT/.venv/bin/vertical-studio" diagnose
