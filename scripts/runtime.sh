#!/usr/bin/env bash

vvs_is_lightning() {
  [[ -d /teamspace/studios/this_studio ]] ||
    [[ -n "${LIGHTNING_CLOUD_PROJECT_ID:-}" ]] ||
    [[ -n "${LIGHTNING_STUDIO_ID:-}" ]]
}

vvs_select_runtime() {
  local root="$1"
  if [[ -n "${VVS_PYTHON:-}" ]]; then
    VVS_PYTHON_BIN="$(command -v "$VVS_PYTHON" 2>/dev/null || true)"
    if [[ -z "$VVS_PYTHON_BIN" && -x "$VVS_PYTHON" ]]; then
      VVS_PYTHON_BIN="$VVS_PYTHON"
    fi
  elif vvs_is_lightning || [[ "${VVS_USE_ACTIVE_ENV:-0}" == "1" ]]; then
    VVS_PYTHON_BIN="$(command -v python3)"
  elif [[ -x "$root/.venv/bin/python" ]]; then
    VVS_PYTHON_BIN="$root/.venv/bin/python"
  else
    echo "No project virtual environment found. Run ./scripts/setup.sh first." >&2
    echo "For an existing Conda environment, set VVS_USE_ACTIVE_ENV=1." >&2
    return 2
  fi
  if [[ ! -x "$VVS_PYTHON_BIN" ]]; then
    echo "Python runtime is not executable: $VVS_PYTHON_BIN" >&2
    return 2
  fi
  export VVS_PYTHON_BIN
  export PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"
}
