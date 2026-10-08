#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/wheelhouse}"
PYTHON_BIN="${VVS_PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "$PYTHON_BIN was not found." >&2
  exit 2
fi
mkdir -p "$DEST"
TEMP_ENV="$(mktemp -d)"
trap 'rm -rf "$TEMP_ENV"' EXIT
"$PYTHON_BIN" -m venv "$TEMP_ENV/venv"
"$TEMP_ENV/venv/bin/python" -m pip install --upgrade pip
"$TEMP_ENV/venv/bin/python" -m pip download   --dest "$DEST"   --requirement "$ROOT/requirements.lock"   "hatchling>=1.26"
echo "Downloaded Linux/Python-compatible wheels to: $DEST"
echo "On the matching offline machine run: ./scripts/setup.sh --offline $DEST"
