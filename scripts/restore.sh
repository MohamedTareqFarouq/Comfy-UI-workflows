#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 BACKUP.sqlite3" >&2
  exit 2
fi
exec "$ROOT/.venv/bin/vertical-studio" restore "$1"
