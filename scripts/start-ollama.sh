#!/usr/bin/env bash
# Ollama (Hermes agent inference) start, pinned to its own GPU so ComfyUI keeps the
# generation GPU exclusively (ComfyUI -> CUDA_VISIBLE_DEVICES=${VVS_COMFY_GPU:-0}).
# Default: Ollama runs on CUDA device 1 (VVS_OLLAMA_CUDA_DEVICE).
set -euo pipefail
export CUDA_VISIBLE_DEVICES="${VVS_OLLAMA_CUDA_DEVICE:-1}"
export NVIDIA_VISIBLE_DEVICES="${VVS_OLLAMA_CUDA_DEVICE:-1}"
export OLLAMA_HOST="${VVS_OLLAMA_HOST:-0.0.0.0:11434}"
export OLLAMA_CONTEXT_LENGTH="${VVS_OLLAMA_CONTEXT_LENGTH:-65536}"
export OLLAMA_KV_CACHE_TYPE="${VVS_OLLAMA_KV_CACHE_TYPE:-q4_0}"
export OLLAMA_FLASH_ATTENTION="${VVS_OLLAMA_FLASH_ATTENTION:-1}"
exec /usr/local/bin/ollama serve
