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
            self.settings.workflows_dir / "flux_t2i_api.json",
            self.settings.workflows_dir / "wan14b_lightx2v_flf2v_api.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise QualificationError(
                "export and configure the required ComfyUI API templates first: "
                + ", ".join(missing)
            )

        payload_path = self.settings.workflows_dir / "qualification_payloads.json"
        if not payload_path.exists():
            raise QualificationError(
                "qualification_payloads.json is required to map prompts, dimensions, "
                "start/end images, seeds and output nodes for this exact ComfyUI export"
            )
        payloads = json.loads(payload_path.read_text())
        flux_payload = payloads.get("flux")
        wan_payloads = payloads.get("wan_candidates", [])
        if not isinstance(flux_payload, dict) or not wan_payloads:
            raise QualificationError(
                "qualification_payloads.json must contain flux and wan_candidates"
            )
        if level == "full" and len(wan_payloads) < len(self.CANDIDATES):
            raise QualificationError(
                "full qualification requires all three ordered Wan candidate payloads"
            )

        generator = ComfyClient(self.settings.comfy_url, self.settings.workflows_dir)
        health = generator.health()
        if not health.get("available"):
            raise QualificationError(f"ComfyUI is unavailable: {health}")

        run_dir = self.settings.data_dir / "qualification" / utc_now().replace(":", "-")
        try:
            report["runs"].append(
                self._run_flux(generator, flux_payload, run_dir / "flux")
            )
            selected = wan_payloads[:1] if level == "smoke" else wan_payloads
            for index, payload in enumerate(selected):
                expected = self.CANDIDATES[index]
                actual = {
                    key: int(payload.get(key, -1))
                    for key in ("width", "height", "frames")
                }
                if actual != expected:
                    raise QualificationError(
                        f"Wan candidate {index + 1} must be {expected}, got {actual}"
                    )
                generator.free_memory()
                report["runs"].append(
                    self._run_wan(
                        generator,
                        payload,
                        run_dir / f"wan-{actual['width']}x{actual['height']}-{actual['frames']}",
                    )
                )
        finally:
            generator.free_memory()

        report["qualified"] = all(run.get("qualified") for run in report["runs"])
        report["message"] = (
            "Selected qualification completed."
            if report["qualified"]
            else "Qualification completed with one or more failed profiles."
        )
        return self._save(report)

    def _run_flux(
        self, generator: ComfyClient, payload: dict[str, Any], output: Path
    ) -> dict[str, Any]:
        started = time.monotonic()
        sampler = ResourceSampler()
        try:
            with sampler:
                result = generator.generate_keyframes(output, payload)
            return {
                "kind": "flux",
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
                "kind": "flux",
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
