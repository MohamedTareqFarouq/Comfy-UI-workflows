#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/runtime.sh"
cd "$ROOT"

MODE=auto
OFFLINE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --active-env) MODE=active; shift ;;
    --venv) MODE=venv; shift ;;
    --offline)
      if [[ $# -lt 2 || "$2" == --* ]]; then
        echo "--offline requires a wheelhouse path" >&2
        exit 2
      fi
      OFFLINE="$2"
      shift 2
      ;;
    *) echo "Usage: $0 [--active-env|--venv] [--offline WHEELHOUSE]" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" == auto ]]; then
  if vvs_is_lightning; then MODE=active; else MODE=venv; fi
fi
PYTHON_BIN="${VVS_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null
command -v ffmpeg >/dev/null
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
PY

if [[ "$MODE" == venv ]]; then
  "$PYTHON_BIN" -m venv .venv
  INSTALL_PYTHON="$ROOT/.venv/bin/python"
else
  INSTALL_PYTHON="$(command -v "$PYTHON_BIN")"
  echo "Using the active Python environment: $INSTALL_PYTHON"
fi

if [[ -n "$OFFLINE" ]]; then
  if [[ ! -d "$OFFLINE" ]]; then
    echo "Wheelhouse not found: $OFFLINE" >&2
    exit 2
  fi
  "$INSTALL_PYTHON" -m pip install --no-index --find-links "$OFFLINE" "hatchling>=1.26"
  "$INSTALL_PYTHON" -m pip install --no-index --find-links "$OFFLINE" --requirement requirements.lock
  "$INSTALL_PYTHON" -m pip install --no-index --find-links "$OFFLINE" --no-build-isolation --no-deps --editable .
else
  if [[ "$MODE" == venv ]]; then "$INSTALL_PYTHON" -m pip install --upgrade pip; fi
  "$INSTALL_PYTHON" -m pip install --requirement requirements.lock
  "$INSTALL_PYTHON" -m pip install --no-deps --editable .
fi
echo "Installed with $MODE mode. Run: ./run-tests.sh && ./start.sh"
