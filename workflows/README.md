# Workflow API templates

Files in the repository root are the original ComfyUI UI graphs and remain unchanged. Real execution requires API-format exports:

- `flux_t2i_api.json`
- `flux_edit_api.json`
- `wan14b_lightx2v_flf2v_api.json`
- Optional explicit fallback: `wan5b_flf2v_api.json`

Export them from the tested ComfyUI installation using **File > Export (API)**. The real adapter rejects UI-format graphs containing a top-level `nodes` array.

The Wan 14B export must retain:

- High-noise and low-noise Wan 2.2 14B FP8 models.
- Matching high/low four-step LightX2V LoRAs.
- The non-LoRA sampling branch disabled.
- FLF2V start and end frames.

## Explicit parameter patches

Copy `workflow_maps.example.json` to `workflow_maps.json` and replace every placeholder with node IDs from the exact API export. Runtime values are deterministic patches, never language-model graph edits:

```json
{
  "patches": [
    {"node_id": "6", "input": "text", "value": "scene prompt"},
    {"node_id": "31", "input": "seed", "value": 12345},
    {"node_id": "40", "input": "width", "value": 384},
    {"node_id": "40", "input": "height", "value": 672}
  ]
}
```

Copy `qualification_payloads.example.json` to `qualification_payloads.json` only after verifying the exact node IDs on the final ComfyUI/custom-node commits. It must define the Flux smoke payload and each Wan candidate's prompt, seed, dimensions, frame count, input-image nodes and output nodes. This file is machine/revision-specific and must be reviewed before a real qualification command.

Templates and profiles remain `unqualified` until an explicitly confirmed target-machine run records the driver, GPU, versions, model hashes, peak memory, duration, output metadata and any recovery actions.


## Continuity-chain image contract

`flux_t2i_api` creates optional character sheets and the first anchor frame of each
continuity group. `flux_edit_api` creates every connected end frame and must expose
the previous-frame input plus the optional character-sheet input in `workflow_maps.json`:

- `source_image`: the immediately preceding frame.
- `character_sheet`: the persistent identity reference when Qwen requires one.

The project `visual_style` is injected into character-sheet, T2I, Flux Edit and Wan
prompts. Stylized projects also add photorealism/live-action terms to negative prompts.
When a two-image edit template is always enabled but Qwen does not request a character
sheet, the application repeats `source_image` in that slot so a sample placeholder can
never leak into generation. A single-input edit template also remains valid.

The application uploads and patches these inputs deterministically. At a continuity
break it creates a new T2I anchor instead of passing unrelated frames to Flux Edit or Wan.
