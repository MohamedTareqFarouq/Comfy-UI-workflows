from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

from .config import Settings
from .diagnostics import diagnose
from .schemas import utc_now
from .services.comfy import ComfyClient, GenerationError
from .services.gpu import GpuCoordinator
from .services.media import probe_media, sha256_file
from .services.qa import QualityService


QualificationLevel = Literal["diagnostics", "smoke", "full"]


class QualificationError(RuntimeError):
    pass


class ResourceSampler:
    def __init__(self) -> None:
        self.peak_gpu_mib = 0
        self.peak_system_ram_mib = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ResourceSampler":
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def metrics(self) -> dict[str, int]:
        return {
            "peak_gpu_memory_mib": self.peak_gpu_mib,
            "peak_system_ram_mib": self.peak_system_ram_mib,
        }

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    values = [int(value.strip()) for value in result.stdout.splitlines()]
                    if values:
                        self.peak_gpu_mib = max(self.peak_gpu_mib, *values)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            try:
                values = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, value = line.split(":", 1)
                    values[key] = int(value.strip().split()[0])
                used_kib = values["MemTotal"] - values["MemAvailable"]
                self.peak_system_ram_mib = max(
                    self.peak_system_ram_mib, used_kib // 1024
                )
            except (OSError, KeyError, ValueError):
                pass
            self._stop.wait(0.25)


class QualificationService:
    """Guarded entry point for target-machine hardware qualification."""

    CANDIDATES = [
        {"width": 384, "height": 672, "frames": 49},
        {"width": 480, "height": 848, "frames": 49},
        {"width": 480, "height": 848, "frames": 81},
    ]

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self,
        level: QualificationLevel,
        *,
        confirmed_real_generation: bool = False,
    ) -> dict[str, Any]:
        manifest_path = self.settings.workflows_dir.parent / "config" / "models.json"
        model_manifest = (
            json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        )
        report: dict[str, Any] = {
            "created_at": utc_now(),
            "model_manifest": model_manifest,
            "level": level,
            "execution_mode": self.settings.execution_mode,
            "diagnostics": diagnose(self.settings),
            "profiles": self.CANDIDATES,
            "qualified": False,
            "runs": [],
        }
        if level == "diagnostics":
            report["message"] = "Diagnostics completed; no model was loaded."
            return self._save(report)

        if not confirmed_real_generation:
            raise QualificationError(
                "real smoke/full qualification requires --confirm-real-generation"
            )
        if self.settings.is_simulation:
            raise QualificationError(
                "real qualification is disabled in simulation mode"
            )

        required = [
            self.settings.workflows_dir / name
            for name in (
                "flux_t2i_api.json",
                "flux_klein_multiref_1_api.json",
                "flux_klein_multiref_2_api.json",
                "qwen_image21_edit_api.json",
                "wan22_i2v_api.json",
                "wan14b_lightx2v_flf2v_api.json",
                "workflow_maps.json",
            )
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise QualificationError(
                "export and configure the required ComfyUI API templates first: "
                + ", ".join(missing)
            )

        generator = ComfyClient(self.settings.comfy_url, self.settings.workflows_dir)
        health = generator.health()
        if not health.get("available"):
            raise QualificationError(f"ComfyUI is unavailable: {health}")

        run_dir = self.settings.data_dir / "qualification" / utc_now().replace(":", "-")
        qualified: list[str] = []
        try:
            common = {
                "prompt": "Qualification image: a red sphere on a neutral background",
                "negative_prompt": "text, watermark, distortion",
                "seed": 12345, "width": 384, "height": 672,
                "candidate_count": 1,
            }
            t2i_id = "flux_klein.t2i.v1"
            t2i_run, reference = self._run_registered(
                generator, t2i_id,
                {**common, "operation_id": "qualification-t2i", "capability_id": t2i_id,
                 "resolved_references": []},
                run_dir / "flux-t2i", video=False,
            )
            report["runs"].append(t2i_run)
            if t2i_run["qualified"]:
                qualified.append(t2i_id)

            if reference is not None:
                reference_item = {
                    "artifact_id": "qualification-reference", "role": "identity",
                    "path": str(reference), "sha256": sha256_file(reference),
                }
                single_id = "flux_klein.single_reference.v1"
                run, _ = self._run_registered(
                    generator, single_id,
                    {**common, "operation_id": f"qualification-{single_id}",
                     "capability_id": single_id, "resolved_references": [reference_item]},
                    run_dir / single_id, video=False,
                )
                report["runs"].append(run)
                if run["qualified"]:
                    qualified.append(single_id)

                qwen_id = "qwen_image21.multi_reference.v1"
                qwen_run, _ = self._run_registered(
                    generator, qwen_id,
                    {**common, "operation_id": f"qualification-{qwen_id}",
                     "capability_id": qwen_id, "resolved_references": [reference_item]},
                    run_dir / qwen_id, video=False,
                )
                report["runs"].append(qwen_run)
                if qwen_run["qualified"]:
                    qualified.append(qwen_id)

                multi_id = "flux_klein.multi_reference.v1"
                multi_passed = True
                roles = ("identity", "style", "environment", "prop")
                for count in (2, 3, 4):
                    references = [
                        {**reference_item, "artifact_id": f"qualification-reference-{index}",
                         "role": roles[index - 1]}
                        for index in range(1, count + 1)
                    ]
                    run, _ = self._run_registered(
                        generator, multi_id,
                        {**common, "operation_id": f"qualification-{multi_id}-{count}",
                         "capability_id": multi_id, "resolved_references": references},
                        run_dir / f"{multi_id}-{count}", video=False,
                    )
                    report["runs"].append(run)
                    multi_passed = multi_passed and bool(run["qualified"])
                if multi_passed:
                    qualified.append(multi_id)

                video_common = {
                    "prompt": "The sphere rotates slowly", "negative_prompt": "jitter, text",
                    "motion_description": "gentle rotation", "camera_description": "locked camera",
                    "seed": 12345, "width": 384, "height": 672, "frames": 49,
                    "fps": 16, "candidate_count": 1,
                }
                start = {**reference_item, "role": "start_frame"}
                for capability_id, references in (
                    ("wan22.i2v.v1", [start]),
                    ("wan22.flf2v.v1", [start, {**reference_item, "role": "end_frame"}]),
                ):
                    generator.free_memory()
                    run, _ = self._run_registered(
                        generator, capability_id,
                        {**video_common, "operation_id": f"qualification-{capability_id}",
                         "capability_id": capability_id, "resolved_references": references},
                        run_dir / capability_id, video=True,
                    )
                    report["runs"].append(run)
                    if run["qualified"]:
                        qualified.append(capability_id)
        finally:
            generator.free_memory()

        expected = {
            "flux_klein.t2i.v1", "flux_klein.single_reference.v1",
            "flux_klein.multi_reference.v1", "wan22.i2v.v1", "wan22.flf2v.v1",
            "qwen_image21.multi_reference.v1",
        }
        report["qualified_capabilities"] = sorted(qualified)
        report["qualified"] = expected <= set(qualified)
        report["message"] = (
            "Selected qualification completed."
            if report["qualified"]
            else "Qualification completed with one or more failed profiles."
        )
        return self._save(report)

    def _run_registered(
        self, generator: ComfyClient, capability_id: str, payload: dict[str, Any],
        output: Path, *, video: bool,
    ) -> tuple[dict[str, Any], Path | None]:
        """Run the same registered adapter used by the MCP, without involving an agent."""
        started = time.monotonic()
        sampler = ResourceSampler()
        try:
            with sampler:
                # Exercise the same GPU-ownership boundary used by HeadlessMediaService,
                # not only the lower-level Comfy adapter.
                with GpuCoordinator(generator, None, simulation=False).comfy(reset_models=False):
                    result = (
                        generator.generate_capability_video(output, payload)
                        if video else generator.generate_capability_image(output, payload)
                    )
            first = result.files[0] if result.files else None
            return ({
                "kind": "video" if video else "image",
                "capability_id": capability_id,
                "qualified": first is not None,
                "duration_seconds": time.monotonic() - started,
                "resources": sampler.metrics,
                "outputs": [
                    {"path": str(path), "sha256": sha256_file(path), "probe": probe_media(path)}
                    for path in result.files
                ],
                "comfy_prompt_id": result.metadata.get("prompt_id"),
            }, first)
        except (GenerationError, OSError) as exc:
            return ({
                "kind": "video" if video else "image",
                "capability_id": capability_id,
                "qualified": False,
                "duration_seconds": time.monotonic() - started,
                "resources": sampler.metrics,
                "error": {"code": getattr(exc, "code", "io_error"), "message": str(exc)},
            }, None)

    def _run_flux(
        self, generator: ComfyClient, payload: dict[str, Any], output: Path,
        kind: str = "flux",
    ) -> dict[str, Any]:
        started = time.monotonic()
        sampler = ResourceSampler()
        try:
            with sampler:
                result = generator.generate_keyframes(output, payload)
            return {
                "kind": kind,
                "qualified": bool(result.files),
                "duration_seconds": time.monotonic() - started,
                "resources": sampler.metrics,
                "outputs": [
                    {
                        "path": str(path),
                        "sha256": sha256_file(path),
                        "probe": probe_media(path),
                    }
                    for path in result.files
                ],
                "comfy_prompt_id": result.metadata.get("prompt_id"),
            }
        except GenerationError as exc:
            return {
                "kind": kind,
                "qualified": False,
                "duration_seconds": time.monotonic() - started,
                "resources": sampler.metrics,
                "error": {"code": exc.code, "message": str(exc)},
            }

    def _run_wan(
        self, generator: ComfyClient, payload: dict[str, Any], output: Path
    ) -> dict[str, Any]:
        started = time.monotonic()
        attempts = []
        for ordinal in (1, 2):
            sampler = ResourceSampler()
            try:
                with sampler:
                    result = generator.generate_video(
                        output / f"attempt-{ordinal}", payload
                    )
                video = result.files[0]
                qa = QualityService().inspect_video(video, output / f"qa-{ordinal}")
                return {
                    "kind": "wan",
                    "profile": {
                        key: payload[key] for key in ("width", "height", "frames")
                    },
                    "qualified": qa.technical_pass,
                    "duration_seconds": time.monotonic() - started,
                    "resources": sampler.metrics,
                    "attempts": [*attempts, {"ordinal": ordinal, "status": "succeeded"}],
                    "output": {
                        "path": str(video),
                        "sha256": sha256_file(video),
                        "probe": probe_media(video),
                    },
                    "qa": qa.model_dump(),
                    "comfy_prompt_id": result.metadata.get("prompt_id"),
                }
            except GenerationError as exc:
                attempts.append(
                    {
                        "ordinal": ordinal,
                        "status": "failed",
                        "error": {"code": exc.code, "message": str(exc)},
                        "resources": sampler.metrics,
                    }
                )
                if exc.code != "oom" or ordinal == 2:
                    return {
                        "kind": "wan",
                        "profile": {
                            key: payload[key] for key in ("width", "height", "frames")
                        },
                        "qualified": False,
                        "duration_seconds": time.monotonic() - started,
                        "attempts": attempts,
                    }
                generator.free_memory()
        raise AssertionError("unreachable")

    def _save(self, report: dict[str, Any]) -> dict[str, Any]:
        directory = self.settings.data_dir / "qualification"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "latest.json"
        target.write_text(json.dumps(report, indent=2))
        report["report_path"] = str(target)
        return report
