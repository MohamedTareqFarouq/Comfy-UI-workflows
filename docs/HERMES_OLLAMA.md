# Hermes 3 + Qwen3-VL through Ollama

This deployment uses Ollama as the reasoning gateway instead of a standalone
llama.cpp server. Two local models share the role:

| Role | Ollama model | Built from | Used by |
| --- | --- | --- | --- |
| Text controller | `hermes3-t4:latest` | `hermes3:8b` | OpenCode primary/subagents, studio planning and storyboards |
| Vision analyst | `qwen3vl-t4:latest` | `qwen3-vl:8b-instruct` | reference analysis, visual QA, image evidence |

The tuned profiles live in `packaging/ollama/Modelfile.hermes3` and
`packaging/ollama/Modelfile.qwen3vl`. Both are created by
`packaging/vastai/setup-ubuntu.sh`.

## Environment

`/opt/vertical-media/env` is written by the installer:

```bash
export VVS_DATA_DIR="/opt/vertical-media/data"
export VVS_EXECUTION_MODE="real"
export VVS_COMFY_URL="http://127.0.0.1:8188"
export VVS_MODEL_PROVIDER="ollama"
export VVS_LLAMA_URL="http://127.0.0.1:11434"
export VVS_CONTROLLER_MODEL="hermes3-t4:latest"
export VVS_VISION_MODEL="qwen3vl-t4:latest"
export VVS_QWEN_MODEL="qwen3vl-t4:latest"
export VVS_OLLAMA_EVICT="true"
export VVS_ENABLE_QWEN_IMAGE="false"
export VVS_KEYFRAME_IMAGE_BACKEND="klein"
export VVS_ALLOW_UNQUALIFIED_CAPABILITIES="false"
```

- `VVS_MODEL_PROVIDER=ollama` switches health checks from `/health` to
  `/api/tags` and disables managed llama.cpp startup.
- `VVS_CONTROLLER_MODEL` handles JSON planning and review.
- `VVS_VISION_MODEL` handles image-input calls; it must be a vision model.
- `VVS_OLLAMA_EVICT=true` asks Ollama to unload both models when ComfyUI takes
  the GPU. With a 15 GB card this avoids OOM during Wan/Flux work; set it to
  `false` when you have headroom and want to keep Hermes warm.
- `VVS_ENABLE_QWEN_IMAGE=false` hides the optional Qwen Image 2.1 capability.
  Its three heavy files are kept in `config/models-qwen-image.json`.
- `VVS_ALLOW_UNQUALIFIED_CAPABILITIES=true` lets real generation run before the
  smoke qualification gate passes. The override is reported per capability in
  `/api/status` under `unqualified_override_capabilities`; run
  `scripts/qualify.sh smoke` when the compute budget allows and set it back to
  `false`.

## OpenCode provider and agents

`opencode.json` registers Ollama through the OpenAI-compatible endpoint and
sets Hermes as the default model:

```json
{
  "model": "ollama/hermes3-t4:latest",
  "providers": {
    "ollama": {
      "settings": { "baseURL": "http://127.0.0.1:11434/v1" }
    }
  }
}
```

Agent assignments:

| Agent | Model |
| --- | --- |
| `vertical-producer`, `hermes-operator`, `shot-planner`, `asset-designer`, `video-director`, `render-finisher`, `runtime-qualifier` | `ollama/hermes3-t4:latest` |
| `reference-analyst`, `qa-reviewer` | `ollama/qwen3vl-t4:latest` |

`hermes-operator` is a general primary agent for environment, repository, and
MCP work. The media MCP server runs over stdio and inherits every `VVS_*`
variable from `opencode.json`; it therefore sees the same Ollama provider and
model split as the studio app.

## Commands

```bash
# Start / verify the server
curl -s http://127.0.0.1:11434/api/version
ollama list
ollama ps

# Chat with each model
ollama run hermes3-t4 "Summarize the MCP status fields."
ollama run qwen3vl-t4 "Read the text in /kaggle/working/ollama/test-image.png"

# Free the T4s before a long Wan render
/opt/vertical-media/free-vram.sh
```

## VRAM note

Two T4s provide 29.1 GiB. Both 8B models together use about 15 GB while loaded.
Real Wan 2.2 14B fp8 work needs most of one card, so the studio evicts the
Ollama models at the ComfyUI boundary and reloads them afterwards. Reload after
eviction is typically a few seconds once the model files are in page cache.

If you need both models resident during a session, set
`VVS_OLLAMA_EVICT=false` and prefer 384×672 smoke generations before committing
to a long queue.
