#!/usr/bin/env bash
set -euo pipefail

# Usage: bash install_comfy_models.sh /path/to/ComfyUI [all|flux|wan|wan-lightning]
COMFY="${1:-$HOME/ComfyUI}"
GROUP="${2:-all}"
if [[ ! -d "$COMFY/models" ]]; then
  echo "ERROR: $COMFY/models not found. Pass the actual ComfyUI folder as first argument." >&2
  exit 1
fi
command -v wget >/dev/null || { echo 'Install wget first: sudo apt-get update && sudo apt-get install -y wget'; exit 1; }
fetch() {
  local subdir="$1" url="$2" name="${2##*/}"
  mkdir -p "$COMFY/models/$subdir"
  echo "Downloading $name -> $COMFY/models/$subdir/"
  wget --continue --tries=10 --waitretry=5 --timeout=60 --show-progress -O "$COMFY/models/$subdir/$name" "$url"
}
flux() {
  fetch diffusion_models 'https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8/resolve/main/flux-2-klein-4b-fp8.safetensors'
  fetch text_encoders 'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors'
  fetch vae 'https://huggingface.co/Comfy-Org/flux2-dev/resolve/main/split_files/vae/flux2-vae.safetensors'
}
wan() {
  fetch diffusion_models 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors'
  fetch diffusion_models 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors'
  fetch text_encoders 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors'
  fetch vae 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors'
}
lightning() {
  fetch loras 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors'
  fetch loras 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors'
}
case "$GROUP" in
  all) flux; wan; lightning;;
  flux) flux;;
  wan) wan;;
  wan-lightning) lightning;;
  *) echo 'Choose all, flux, wan, or wan-lightning' >&2; exit 2;;
esac
echo 'Downloads complete. Refresh model lists or restart ComfyUI.'
