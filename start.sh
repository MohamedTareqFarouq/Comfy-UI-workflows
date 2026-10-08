#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$ROOT/.venv/bin/vertical-studio" ]]; then
  echo "Virtual environment not found. Run ./scripts/setup.sh first." >&2
  exit 2
fi
export VVS_EXECUTION_MODE="${VVS_EXECUTION_MODE:-simulation}"
exec "$ROOT/.venv/bin/vertical-studio" serve "$@"
