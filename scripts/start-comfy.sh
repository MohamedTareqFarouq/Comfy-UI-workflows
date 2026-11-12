#!/usr/bin/env bash
# ComfyUI (media generation) start with GPU pinned for the Hermes + Qwen3-VL experiment.
# Generation owns the GPU exclusively: CUDA_VISIBLE_DEVICES=${VVS_COMFY_GPU:-0} and
# ComfyUI sees only that device (--cuda-device), so agent inference (Ollama -> CUDA 1)
# never competes for generation VRAM and generation never exceeds the allowed memory.
set -euo pipefail
export CUDA_VISIBLE_DEVICES="${VVS_COMFY_GPU:-0}"
exec "/opt/vertical-media/ComfyUI/.venv/bin/python" "/opt/vertical-media/ComfyUI/main.py" --listen 127.0.0.1 --port 8188 --cuda-device "${VVS_COMFY_GPU:-0}"
