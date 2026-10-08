from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class MediaError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type,width,height,r_frame_rate,pix_fmt,nb_frames",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise MediaError(result.stderr.strip() or f"ffprobe failed for {path}")
    return json.loads(result.stdout)


def run_ffmpeg(arguments: list[str], timeout: int = 180) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise MediaError(result.stderr.strip() or "ffmpeg failed")


def extract_frames(video: Path, output_dir: Path, count: int = 5) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = probe_media(video)
    duration = float(metadata.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise MediaError("video has no duration")
    frames: list[Path] = []
    for index in range(count):
        # Do not seek to the exact container duration: there may be no frame at EOF.
        timestamp = duration * index / max(1, count)
        target = output_dir / f"sample-{index + 1:02d}.jpg"
        run_ffmpeg(["-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(target)])
        frames.append(target)
    return frames
