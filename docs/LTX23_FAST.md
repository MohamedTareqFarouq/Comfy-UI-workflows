# LTX 2.3 Fast integration

The capability router contains `ltx23.fast.i2v.v1` and
`ltx23.fast.flf2v.v1`. In real mode they remain unavailable until their API
workflows exist and target-machine qualification succeeds. Once qualified,
automatic video routing prefers LTX and retains Wan as a fallback.

## Fast target-machine preparation

Install the official custom nodes and the two-file FP8 model pack:

```bash
cd /opt/vertical-media/vertical-video-studio
./scripts/install-ltx23.sh /opt/vertical-media/ComfyUI
```

Restart ComfyUI. Load the official LTX 2.3 distilled image-to-video workflow
from the custom node's `example_workflows/2.3` directory. First validate a
short 9:16 clip manually using the same image and prompt that performed well in
LTX Studio.

For a fresh Vast installation, set `VVS_INSTALL_LTX23=1` before running the
bootstrap script. This deliberately leaves the existing Wan files installed
until LTX passes qualification.

Export the successful graph in ComfyUI API format as:

```text
workflows/ltx23_fast_i2v_api.json
```

If the tested graph supports first-and-last-frame interpolation, export that
variant as:

```text
workflows/ltx23_fast_flf2v_api.json
```

Add reviewed entries to `workflows/workflow_maps.json`. Each map uses the same
stable names as the existing video adapter:

```json
{
  "ltx23_fast_i2v_api": {
    "prompt": {"node_id": "...", "input": "text"},
    "negative_prompt": {"node_id": "...", "input": "text"},
    "seed": {"node_id": "...", "input": "noise_seed"},
    "width": {"node_id": "...", "input": "width"},
    "height": {"node_id": "...", "input": "height"},
    "frames": {"node_id": "...", "input": "length"},
    "start_image": {"node_id": "...", "input": "image"},
    "output_prefix": {"node_id": "...", "input": "filename_prefix"}
  }
}
```

The first/last-frame map additionally requires `end_image`. Never guess node
IDs: use the exact API export that passed the manual calibration.

LTX dimensions must be divisible by 32 and its frame count follows `8n+1`.
Start with 540p/720p and 41-81 frames on a 16 GB streamed setup.

## Disk planning

The minimal LTX pack in `config/models-ltx23.json` is about 42.7 GB. Reserve
50-60 GB after caches, custom-node dependencies, temporary downloads and test
outputs. A fuller two-stage pack with spatial upscalers needs roughly another
1-3 GB; retaining alternate checkpoints can add 30-46 GB each.

Do not use a 100 GB Vast disk while retaining the full Wan installation. Use
at least 200 GB, with 250 GB preferred for both model families, caches and
production outputs. If Wan is removed after LTX qualification, 150-200 GB is
normally sufficient.
