# Vertical Video Studio

A local-first, simulation-first agentic application for producing coherent 10-30 second vertical videos. It runs natively on Ubuntu Linux or Windows through WSL2 and does not require Docker.

The default mode is simulation. It never submits a real Flux or Wan job until real mode is explicitly configured and qualified.

## Clone and run

Requirements:

- Linux or WSL2.
- Python 3.11 or newer.
- FFmpeg and FFprobe.
- Git.
- Node.js is optional and is used only to syntax-check the PWA during tests.

On Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg
```

Clone the repository and install it:

```bash
git clone YOUR_REPOSITORY_URL vertical-video-studio
cd vertical-video-studio
./scripts/setup.sh
./run-tests.sh
./start.sh
```

Open <http://127.0.0.1:8765>.

The first three commands normally used after cloning are therefore:

| Command | Purpose |
| --- | --- |
| `./scripts/setup.sh` | Install into Lightning's active environment or create `.venv` on a laptop. |
| `./run-tests.sh` | Compile Python, check the PWA, run all tests and run simulation diagnostics. |
| `./start.sh` | Start the local web application in safe simulation mode. |

The start script accepts server options:

```bash
./start.sh --host 127.0.0.1 --port 8765
```

Press Ctrl+C to stop it.

## Download dependencies for an offline machine

On an internet-connected Linux machine with the same CPU architecture and Python version as the destination:

```bash
./download-reqs.sh
```

This downloads pinned Python wheels and build requirements into `wheelhouse/`. Copy the Git checkout, including `wheelhouse/`, to the offline machine, then run:

```bash
./scripts/setup.sh --offline ./wheelhouse
./run-tests.sh
./start.sh
```

Use a different wheel destination when needed:

```bash
./download-reqs.sh /path/to/wheelhouse
```

`requirements.lock` records the tested dependency versions. `pyproject.toml` remains the authoritative project dependency declaration.

## Lightning AI and laptop runtimes

The scripts automatically use Lightning's active Conda environment when the checkout
is under a Lightning Studio. On a normal Linux/WSL laptop they use `.venv`.

Lightning AI (one environment):

```bash
./scripts/setup.sh --active-env
./run-tests.sh
./scripts/qualify.sh smoke
./scripts/start-lightning.sh
```

Normal Linux/WSL laptop:

```bash
./scripts/setup.sh
./run-tests.sh
./start.sh --simulation
# After real workflows are qualified:
./start.sh --real --managed-qwen
```

The Lightning launcher uses Qwen3-VL-4B-Instruct Q4_K_M with a 16,384-token
runtime context. This gives reference-video analysis room for eight evidence
frames, its structured schema and the generated blueprint without reserving a
32K context window. Override
`VVS_QWEN_HF_REPO`, `VVS_QWEN_MODEL` or `VVS_QWEN_CONTEXT_SIZE` when needed.

Use `VVS_PYTHON=/path/to/python` or `VVS_USE_ACTIVE_ENV=1` to override automatic
runtime selection. `start.sh` also accepts `--managed-qwen`, `--no-managed-qwen`,
`--real`, and `--simulation` before the normal server arguments.

## Development workflow

After changing code:

```bash
./run-tests.sh
```

For a headless end-to-end simulation:

```bash
VVS_DATA_DIR=/tmp/vvs-demo VVS_USE_ACTIVE_ENV=1 python3 -m vertical_studio.cli demo
```

The automated suite covers complete 10-, 20- and 30-second projects, portrait media, API/PWA contracts, run history, final outputs, duplicate prevention, scene retry isolation, pause/cancel behavior, lease recovery, research persistence, malformed workflow rejection, simulated failures and guarded qualification.

## Configuration

The application reads environment variables. Important settings are:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VVS_DATA_DIR` | `~/.local/share/vertical-video-studio` | SQLite database, projects, attempts and qualification reports. |
| `VVS_EXPORT_DIR` | `$VVS_DATA_DIR/exports` | Completed MP4 files. |
| `VVS_EXECUTION_MODE` | `simulation` | `simulation` or explicitly configured `real`. |
| `VVS_HOST` | `127.0.0.1` | Web server address. |
| `VVS_PORT` | `8765` | Web server port. |
| `VVS_COMFY_URL` | `http://127.0.0.1:8188` | ComfyUI API endpoint. |
| `VVS_LLAMA_URL` | `http://127.0.0.1:8080` | llama.cpp Qwen endpoint. |
| `VVS_QWEN_MODEL` | `Qwen3-VL-4B-Instruct-GGUF` | llama.cpp model alias; Lightning also defaults to this faster 4B model. |
| `VVS_QWEN_HF_REPO` | `Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M` | Hugging Face GGUF source used by the default and Lightning launchers. |
| `VVS_BRAVE_API_KEY` | unset | Optional current/factual research provider. |
| `VVS_QWEN_CONTEXT_SIZE` | `16384` | Managed Qwen context window; reference-video analysis requires more than 8,192 tokens. |
| `VVS_QWEN_START_TIMEOUT` | `300` | Seconds allowed for a managed Qwen server to load. |
| `VVS_QWEN_REQUEST_TIMEOUT` | `600` | Seconds allowed for a Qwen completion, including long reference analysis. |

Example:

```bash
export VVS_DATA_DIR="$HOME/.local/share/vertical-video-studio"
export VVS_EXECUTION_MODE=simulation
./start.sh
```

Do not expose port 8765 directly to the public internet; the MVP has no login layer. For access from another computer, keep the server bound to localhost and use SSH:

```bash
ssh -L 8765:127.0.0.1:8765 USER@LINUX_MACHINE
```

Then open <http://127.0.0.1:8765> on the client computer.

## Run as an Ubuntu user service

The provided service expects the checkout at `~/vertical-video-studio`:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/vertical-video-studio.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vertical-video-studio
systemctl --user status vertical-video-studio
```

Logs:

```bash
journalctl --user -u vertical-video-studio -f
```

To allow the user service to remain active after logout:

```bash
loginctl enable-linger "$USER"
```

The supplied service forces simulation mode. Change that only after real hardware qualification succeeds.

## Git repository and publishing

This checkout uses the `main` branch. To publish it to an empty GitHub, GitLab or other Git server:

```bash
git remote add origin YOUR_REPOSITORY_URL
git push -u origin main
```

Machine-specific files are intentionally ignored:

- Virtual environments and caches.
- Downloaded wheelhouse contents.
- API workflow exports.
- `workflow_maps.json`.
- `qualification_payloads.json`.
- Runtime databases, projects, logs and generated media.

The example workflow mapping files remain tracked.

## What is implemented

- FastAPI backend and local PWA.
- SQLite WAL state for projects, character bibles, scenes, jobs, attempts, artifacts, QA, approvals, render manifests, research sources and append-only events.
- Run history, earlier media attempts, errors, approvals and final MP4 playback/download.
- Validated state transitions, idempotent submissions, expiring worker leases and restart recovery.
- One GPU-intensive job at a time, pause, cancellation and isolated scene retries.
- Mock ComfyUI generation for development without the target GPU.
- Real ComfyUI transport with explicit node maps, generated-frame upload, WebSocket progress, history reconciliation, interruption and unloading.
- Flux image and Wan 14B four-step LightX2V profiles. Wan 5B is opt-in only.
- Technical QA and optional local Qwen sampled-frame critique.
- FFmpeg 1080x1920 H.264/yuv420p rendering without an audio track.
- Ubuntu and WSL2 launchers, guarded qualification, backup and recoverable restore.

Audio, narration, captions and music are outside this visual-only MVP.

### Reference remake mode

New projects can start from a 10-30 second uploaded MP4, MOV, WebM, MKV or M4V
reference instead of a text-only idea. The initial implementation:

1. Stores and hashes the immutable source in managed project storage.
2. Validates it with FFprobe and derives the project duration.
3. Extracts eight chronological evidence frames.
4. Uses Qwen to produce a typed visual-style, motion-style, canonical-asset and
   scene blueprint. Simulation mode produces the same deterministic contract.
5. Presents the source, evidence and blueprint for approval.
6. Converts the approved blueprint into the existing storyboard and canonical
   asset pipeline.

This first slice accepts local file uploads. Direct remote video URLs, shot-aware
sampling, editable entity consolidation and source-frame-conditioned canonical
asset generation remain follow-up work. The Wan stage still interprets textual
motion descriptions; reference mode is a generative remake, not an exact clone.


## Backup and migration

Stop the service before restoring or moving live data.

Database backup:

```bash
./scripts/backup.sh /path/to/studio-backup.sqlite3
```

Recoverable restore:

```bash
./scripts/restore.sh /path/to/studio-backup.sqlite3
```

For complete migration, copy the entire data directory because it also contains images, video attempts, QA frames and final exports:

```bash
rsync -av "$HOME/.local/share/vertical-video-studio/" \
  USER@NEW_MACHINE:~/.local/share/vertical-video-studio/
```

## Continuity-aware keyframe chains

Qwen marks each storyboard scene with a continuity group and decides whether the
project needs a character sheet. After storyboard approval, the image stage is
deterministic:

1. Generate the optional character sheet with Flux T2I.
2. Generate the first frame of each continuity group with Flux T2I.
3. Generate every connected end frame with Flux Edit, using the immediately
   previous frame and the character sheet when required.
4. Reuse each exact end-frame file as the next connected scene's start frame.
5. Wait until all keyframe pairs are approved, unload Flux, then queue Wan for
   `F1 -> F2`, `F2 -> F3`, and so on.

The requested project style is a hard constraint at every stage. It is repeated in
the character-sheet, T2I, Flux Edit and Wan prompts; stylized projects explicitly
reject photorealistic and live-action drift.

A new location, main character, time jump, radical composition, or deliberate cut
starts a new continuity group and therefore a fresh T2I anchor. Real execution
requires both `flux_t2i_api.json` and `flux_edit_api.json`; see
[workflows/README.md](workflows/README.md) for the two-reference Flux Edit map.

## Automatic GPU sharing with Qwen

On a single 16 GB GPU, Vertical Studio can own the llama.cpp process and load Qwen
only for storyboard planning and visual review. ComfyUI models are freed before Qwen
starts; Qwen is stopped before Flux or Wan runs.

```bash
export VVS_MANAGE_QWEN=true
export VVS_LLAMA_EXECUTABLE=llama
export VVS_QWEN_GPU_LAYERS=99
```

Do not start a separate llama.cpp server when managed mode is enabled. Logs are saved
under `$VVS_DATA_DIR/logs/`.

## Real ComfyUI setup and qualification

The original repository workflows are ComfyUI UI graphs. Real mode deliberately rejects them until they are exported in API format.

1. Keep simulation mode enabled during installation.
2. Install and pin ComfyUI and required custom nodes.
3. Place Flux, Wan and LightX2V LoRA files in their required ComfyUI directories.
4. Export the tested graphs through **File > Export (API)**.
5. Save the templates using the names in [workflows/README.md](workflows/README.md).
6. Copy `workflow_maps.example.json` to the ignored `workflow_maps.json` and enter exact node IDs.
7. Copy `qualification_payloads.example.json` to the ignored `qualification_payloads.json`.
8. Populate and verify hashes in [config/models.json](config/models.json).
9. Start ComfyUI on the configured local endpoint. Managed mode starts Qwen on the GPU only when planning needs it.
10. Run qualification explicitly.

```bash
./scripts/qualify.sh diagnostics
./scripts/qualify.sh smoke
./scripts/qualify.sh full
```

Wan candidates are tested in this order:

1. 384x672, 49 frames.
2. 480x848, 49 frames.
3. 480x848, 81 frames.

No static inspection or simulated run marks a real profile as qualified. See [workflows/README.md](workflows/README.md) for the exact workflow contract.

## Windows / WSL2

Keep models, SQLite and active media in the WSL Linux filesystem. Use the Windows NVIDIA driver exposed to WSL2; do not install a separate Linux NVIDIA kernel driver inside WSL.

Set `VVS_WSL_PROJECT` when the checkout is not at the default location, then run:

```powershell
packaging/windows/start-vertical-studio.ps1
```

Windows export paths are translated through `wslpath`.
