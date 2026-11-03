#!/usr/bin/env bash
set -euo pipefail
COMFY_DIR="${1:?usage: real-mode-preflight.sh COMFYUI_DIR}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v nvidia-smi >/dev/null || { echo "nvidia-smi missing" >&2; exit 1; }
nvidia-smi >/dev/null
[[ -d "$COMFY_DIR/models" ]] || { echo "ComfyUI models directory missing" >&2; exit 1; }
[[ -f "$ROOT/workflows/workflow_maps.json" ]] || {
  echo "workflows/workflow_maps.json is missing; copy and review the example for this exact ComfyUI export." >&2
  exit 1
}
if rg -q '"REPLACE"' "$ROOT/workflows/workflow_maps.json"; then
  echo "workflow_maps.json still contains REPLACE placeholders" >&2
  exit 1
fi
for name in \
  flux_t2i_api.json \
  flux_klein_multiref_1_api.json \
  flux_klein_multiref_2_api.json \
  flux_klein_multiref_3_api.json \
  flux_klein_multiref_4_api.json \
  wan22_i2v_api.json \
  wan14b_lightx2v_flf2v_api.json; do
  [[ -f "$ROOT/workflows/$name" ]] || { echo "missing reviewed API workflow: $name" >&2; exit 1; }
done
for mapping in \
  flux_t2i_api \
  flux_klein_multiref_1_api \
  flux_klein_multiref_2_api \
  flux_klein_multiref_3_api \
  flux_klein_multiref_4_api \
  wan22_i2v_api \
  wan14b_lightx2v_flf2v_api; do
  jq -e --arg mapping "$mapping" '.[$mapping] | type == "object"' \
    "$ROOT/workflows/workflow_maps.json" >/dev/null || {
      echo "missing workflow mapping: $mapping" >&2
      exit 1
    }
done
while IFS=$'\t' read -r directory file; do
  [[ -s "$COMFY_DIR/models/$directory/$file" ]] || {
    echo "missing model: $directory/$file" >&2
    exit 1
  }
done < <(jq -r '.comfy_models[] | [.directory, .file] | @tsv' "$ROOT/config/models.json")
echo "Static real-mode preflight passed. Run scripts/qualify.sh smoke before production."
