# Workflow API templates

Files in the repository root are the original ComfyUI UI graphs and remain unchanged. Real execution requires API-format exports:

- `flux_t2i_api.json`
- `image_flux2_klein_image_edit_4b_distilled_api.json` (one reference)
- `flux_edit_api.json` (two references)
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

Copy `qualification_payloads.example.json` to `qualification_payloads.json` only after verifying the exact node IDs on the final ComfyUI/custom-node commits. It must define the T2I, one-reference Edit, two-reference Edit, and Wan smoke payloads, including prompts, seeds, dimensions, input-image nodes and output nodes. This file is machine/revision-specific and must be reviewed before a real qualification command.

Templates and profiles remain `unqualified` until an explicitly confirmed target-machine run records the driver, GPU, versions, model hashes, peak memory, duration, output metadata and any recovery actions.


## Continuity-chain image contract

Text-only assets and anchors use `flux_t2i_api`. Reference-derived canonical assets and
keyframes use FLUX Edit with no more than two distinct images:

- `source_image`: source evidence, the opening frame, or the immediately preceding frame.
- `reference_image`: one optional canonical character or environment reference.

The one-reference template is selected when no second image is needed. The two-reference
`flux_edit_api` is selected only when `reference_image` is present, so sample placeholders
and duplicate references cannot leak into generation. For a stable location the primary
character is preferred; for a location transition the destination environment is preferred.

The project `visual_style` is injected into character-sheet, T2I, Flux Edit and Wan
prompts. Stylized projects also add photorealism/live-action terms to negative prompts.
The application uploads and patches every reference deterministically.
