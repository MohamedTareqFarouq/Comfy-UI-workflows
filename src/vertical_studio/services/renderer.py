from __future__ import annotations

import json
import uuid
from pathlib import Path

from .media import probe_media, run_ffmpeg


class Renderer:
    def __init__(self, width: int = 1080, height: int = 1920):
        self.width = width
        self.height = height

    def render(self, clips: list[Path], target: Path) -> dict:
        if not clips:
            raise ValueError("at least one approved clip is required")
        target.parent.mkdir(parents=True, exist_ok=True)
        normalized: list[Path] = []
        for index, clip in enumerate(clips, 1):
            part = target.parent / f".{target.stem}-{uuid.uuid4().hex[:8]}-{index}.mp4"
            run_ffmpeg(
                [
                    "-i", str(clip),
                    "-vf",
                    f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,crop={self.width}:{self.height},setsar=1,format=yuv420p",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-an", "-movflags", "+faststart", str(part),
                ],
                timeout=600,
            )
            normalized.append(part)
        list_path = target.parent / f".{target.stem}-{uuid.uuid4().hex[:8]}.ffconcat"
        list_path.write_text("ffconcat version 1.0\n" + "\n".join(f"file '{path.as_posix()}'" for path in normalized) + "\n")
        try:
            run_ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", "-movflags", "+faststart", str(target)],
                timeout=600,
            )
            return {"target": str(target), "clips": [str(path) for path in clips], "probe": probe_media(target)}
        finally:
            list_path.unlink(missing_ok=True)
            for path in normalized:
                path.unlink(missing_ok=True)
