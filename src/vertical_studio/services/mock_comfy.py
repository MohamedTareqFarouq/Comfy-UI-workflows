from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path
from typing import Any

from .comfy import GenerationError, GenerationResult
from .media import probe_media, run_ffmpeg


class MockComfyClient:
    """Deterministic media generator with the same contract as real ComfyUI."""

    def health(self) -> dict[str, Any]:
        return {"available": True, "mode": "simulation"}

    @staticmethod
    def _maybe_fail(payload: dict[str, Any]) -> None:
        failure = payload.get("simulate_failure")
        if failure:
            raise GenerationError(str(failure), f"simulated {failure}", transient=failure in {"timeout", "oom"})

    @staticmethod
    def _color(key: str, offset: int = 0) -> str:
        value = int(hashlib.sha256(f"{key}:{offset}".encode()).hexdigest()[:8], 16)
        hue = (value % 360) / 360
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.45, 0.72)
        return f"0x{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"

    def generate_reference_asset(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        self._maybe_fail(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        width = int(payload.get("width", 384))
        height = int(payload.get("height", 672))
        key = str(payload.get("asset_key", "reference"))
        path = output_dir / f"canonical-{key}.png"
        run_ffmpeg([
            "-f", "lavfi", "-i", f"color=c={self._color(key)}:s={width}x{height}:d=0.1",
            "-frames:v", "1", "-threads", "1", str(path),
        ])
        return GenerationResult(
            files=[path], metadata={"simulated": True, "asset_key": key}
        )

    def generate_character_sheet(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        self._maybe_fail(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        width = int(payload.get("width", 384))
        height = int(payload.get("height", 672))
        path = output_dir / "character-sheet.png"
        run_ffmpeg([
            "-f", "lavfi", "-i", f"color=c={self._color('character-sheet')}:s={width}x{height}:d=0.1",
            "-frames:v", "1", "-threads", "1", str(path),
        ])
        return GenerationResult(files=[path], metadata={"simulated": True, "role": "character_sheet"})

    def generate_keyframes(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        self._maybe_fail(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        width = int(payload.get("width", 384))
        height = int(payload.get("height", 672))
        candidates = int(payload.get("candidates", 2))
        scene_id = str(payload.get("scene_id", "scene"))
        files: list[Path] = []
        mode = payload.get("generation_mode", "anchor")
        roles = ("start", "end") if mode == "anchor" else ("end",)
        for candidate in range(1, candidates + 1):
            for role_index, role in enumerate(roles):
                path = output_dir / f"candidate-{candidate}-{role}.png"
                color = self._color(scene_id, candidate * 2 + role_index)
                run_ffmpeg(
                    [
                        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d=0.1",
                        "-frames:v", "1", "-threads", "1", str(path),
                    ]
                )
                files.append(path)
        return GenerationResult(files=files, metadata={"simulated": True, "width": width, "height": height})

    def generate_video(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        self._maybe_fail(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        width = int(payload.get("width", 384))
        height = int(payload.get("height", 672))
        fps = int(payload.get("fps", 16))
        duration = float(payload.get("duration_seconds", 3.0))
        scene_id = str(payload.get("scene_id", "scene"))
        target = output_dir / "video.mp4"
        color = self._color(scene_id, int(payload.get("attempt", 1)))
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}:d={duration}",
                "-vf", "format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-an", "-movflags", "+faststart", str(target),
            ],
            timeout=120,
        )
        return GenerationResult(files=[target], metadata={"simulated": True, "probe": probe_media(target)})

    def interrupt(self) -> None:
        return None

    def free_memory(self) -> None:
        return None
