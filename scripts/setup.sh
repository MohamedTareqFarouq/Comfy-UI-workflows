#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
command -v python3 >/dev/null
command -v ffmpeg >/dev/null
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
PY
python3 -m venv .venv
if [[ "${1:-}" == "--offline" ]]; then
  WHEELHOUSE="${2:-$ROOT/wheelhouse}"
  if [[ ! -d "$WHEELHOUSE" ]]; then
    echo "Wheelhouse not found: $WHEELHOUSE" >&2
    exit 2
  fi
  .venv/bin/python -m pip install     --no-index --find-links "$WHEELHOUSE"     "hatchling>=1.26"
  .venv/bin/python -m pip install     --no-index --find-links "$WHEELHOUSE"     --requirement requirements.lock
  .venv/bin/python -m pip install     --no-index --find-links "$WHEELHOUSE"     --no-build-isolation --no-deps --editable .
else
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install --requirement requirements.lock
  .venv/bin/python -m pip install --no-deps --editable .
fi
echo "Installed. Run: ./run-tests.sh && ./start.sh"
