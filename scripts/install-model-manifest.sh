#!/usr/bin/env bash
set -euo pipefail
MANIFEST="${1:?usage: install-model-manifest.sh MANIFEST.json COMFYUI_DIR [all|flux|wan|wan-lightning]}"
COMFY_DIR="${2:?usage: install-model-manifest.sh MANIFEST.json COMFYUI_DIR [all|flux|wan|wan-lightning]}"
GROUP="${3:-all}"

case "$GROUP" in
  all|flux|wan|wan-lightning) ;;
  *) echo "Choose all, flux, wan, or wan-lightning" >&2; exit 2 ;;
esac
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }
command -v aria2c >/dev/null || { echo "aria2c is required" >&2; exit 1; }
[[ -d "$COMFY_DIR/models" ]] || { echo "$COMFY_DIR/models not found" >&2; exit 1; }

if jq -e 'type == "array"' "$MANIFEST" >/dev/null; then
  FILTER='.'
else
  jq -e '.comfy_models | type == "array"' "$MANIFEST" >/dev/null
  FILTER='.comfy_models'
fi

auth_args=()
if [[ -n "${HF_TOKEN:-}" ]]; then
  auth_args+=(--header="Authorization: Bearer $HF_TOKEN")
fi

jq -c "$FILTER[]" "$MANIFEST" | while read -r item; do
  item_group="$(jq -r '.group // "all"' <<<"$item")"
  if [[ "$GROUP" != "all" && "$item_group" != "$GROUP" ]]; then
    continue
  fi
  relative="$(jq -r 'if .path then .path else (.directory + "/" + .file) end' <<<"$item")"
  url="$(jq -r '.url' <<<"$item")"
  sha="$(jq -r '.sha256 // empty' <<<"$item")"
  [[ "$relative" != /* && "$relative" != *".."* ]] || { echo "unsafe model path: $relative" >&2; exit 1; }
  [[ "$url" == https://* ]] || { echo "model URL must use HTTPS: $url" >&2; exit 1; }
  target="$COMFY_DIR/models/$relative"
  mkdir -p "$(dirname "$target")"
  echo "Downloading $relative"
  aria2c -c -x 8 -s 8 --file-allocation=none \
    "${auth_args[@]}" -d "$(dirname "$target")" -o "$(basename "$target")" "$url"
  if [[ ! -s "$target" ]]; then
    echo "downloaded model is empty: $target" >&2; exit 1
  fi
  if [[ -n "$sha" ]]; then
    printf '%s  %s\n' "$sha" "$target" | sha256sum -c -
  fi
done
echo "Requested model group '$GROUP' is installed."
