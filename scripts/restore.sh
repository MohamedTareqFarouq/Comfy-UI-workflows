#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/runtime.sh"
vvs_select_runtime "$ROOT"
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 BACKUP.sqlite3" >&2
  exit 2
fi
exec "$VVS_PYTHON_BIN" -m vertical_studio.cli restore "$1"
