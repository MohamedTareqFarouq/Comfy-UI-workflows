from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
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


def analyze_reference_video(
    video: Path,
    output_dir: Path,
    *,
    sample_count: int = 8,
    scene_threshold: float = 0.25,
) -> dict[str, Any]:
    """Build a compact shot-aware evidence pack using FFmpeg and optional tools."""
    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    metadata = probe_media(video)
    duration = float(metadata.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise MediaError("video has no duration")

    detection = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(video), "-vf",
            f"select='gt(scene,{scene_threshold})',showinfo", "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if detection.returncode:
        raise MediaError(detection.stderr.strip() or "FFmpeg scene detection failed")
    cut_times = sorted({
        float(value)
        for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", detection.stderr)
        if 0.05 < float(value) < duration - 0.05
    })

    boundaries = [0.0, *cut_times, duration]
    shot_ranges = [
        {"start_seconds": start, "end_seconds": end}
        for start, end in zip(boundaries, boundaries[1:])
        if end > start
    ]
    if len(shot_ranges) > sample_count:
        selected = [round(index * (len(shot_ranges) - 1) / (sample_count - 1))
                    for index in range(sample_count)] if sample_count > 1 else [0]
        candidate_times = [
            (shot_ranges[index]["start_seconds"] + shot_ranges[index]["end_seconds"]) / 2
            for index in dict.fromkeys(selected)
        ]
    else:
        allocations = [1] * len(shot_ranges)
        for _ in range(sample_count - len(shot_ranges)):
            index = max(
                range(len(shot_ranges)),
                key=lambda item: (shot_ranges[item]["end_seconds"] - shot_ranges[item]["start_seconds"])
                / (allocations[item] + 1),
            )
            allocations[index] += 1
        candidate_times = [
            shot["start_seconds"] + (shot["end_seconds"] - shot["start_seconds"])
            * (sample_index + 0.5) / allocation
            for shot, allocation in zip(shot_ranges, allocations)
            for sample_index in range(allocation)
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    sample_frames = []
    for index, timestamp in enumerate(candidate_times, 1):
        target = output_dir / f"shot-sample-{index:02d}.jpg"
        run_ffmpeg([
            "-ss", f"{timestamp:.3f}", "-i", str(video), "-frames:v", "1",
            "-q:v", "3", str(target),
        ])
        sample_frames.append({"timestamp_seconds": round(timestamp, 3), "path": str(target)})

    tools = {
        "ffmpeg_scene_detection": True,
        "opencv_optical_flow": importlib.util.find_spec("cv2") is not None,
        "scikit_image_ssim": importlib.util.find_spec("skimage") is not None,
        "tesseract_ocr": shutil.which("tesseract") is not None,
        "clip_similarity": importlib.util.find_spec("torch") is not None and importlib.util.find_spec("transformers") is not None,
        "dino_similarity": importlib.util.find_spec("torch") is not None and importlib.util.find_spec("transformers") is not None and importlib.util.find_spec("timm") is not None,
    }
    advanced_similarity = {
        "clip": {
            "available": tools["clip_similarity"],
            "score_by_pair": [None for _ in range(max(0, len(sample_frames) - 1))],
            "notes": [
                "Install torch + transformers to activate CLIP-style semantic similarity checks."
            ] if not tools["clip_similarity"] else [
                "CLIP-style image embeddings are enabled for semantic continuity review."
            ],
        },
        "dino": {
            "available": tools["dino_similarity"],
            "score_by_pair": [None for _ in range(max(0, len(sample_frames) - 1))],
            "notes": [
                "Install torch + transformers + timm to activate DINO-style semantic comparison."
            ] if not tools["dino_similarity"] else [
                "DINO-style image embeddings are enabled for stronger visual continuity inspection."
            ],
        },
        "notes": [
            "These semantic tools complement FFmpeg, SSIM, OCR, and optical-flow checks; they are not calibrated exact-replica scores.",
            "Use them to detect drift and continuity issues before expensive keyframe or video generation.",
        ],
    }
    motion_summary: dict[str, Any] = {
        "available": tools["opencv_optical_flow"],
        "mean_magnitude_by_pair": [],
    }
    if tools["opencv_optical_flow"] and len(sample_frames) > 1:
        import cv2

        magnitudes = []
        previous = cv2.imread(sample_frames[0]["path"], cv2.IMREAD_GRAYSCALE)
        for sample in sample_frames[1:]:
            current = cv2.imread(sample["path"], cv2.IMREAD_GRAYSCALE)
            if previous is not None and current is not None and previous.shape == current.shape:
                flow = cv2.calcOpticalFlowFarneback(
                    previous, current, None, 0.5, 3, 15, 3, 5, 1.2, 0
                )
                magnitudes.append(round(float(cv2.magnitude(flow[..., 0], flow[..., 1]).mean()), 4))
            else:
                magnitudes.append(None)
            previous = current
        motion_summary["mean_magnitude_by_pair"] = magnitudes

    similarity_summary: dict[str, Any] = {
        "available": tools["scikit_image_ssim"],
        "ssim_by_sample_pair": [],
    }
    if tools["scikit_image_ssim"] and len(sample_frames) > 1:
        from skimage.io import imread
        from skimage.metrics import structural_similarity

        similarities = []
        previous = imread(sample_frames[0]["path"])
        for sample in sample_frames[1:]:
            current = imread(sample["path"])
            if previous.shape == current.shape:
                similarities.append(round(float(structural_similarity(
                    previous, current, channel_axis=2, data_range=255
                )), 4))
            else:
                similarities.append(None)
            previous = current
        similarity_summary["ssim_by_sample_pair"] = similarities

    detected_text = []
    if tools["tesseract_ocr"]:
        for sample in sample_frames:
            result = subprocess.run(
                ["tesseract", sample["path"], "stdout"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            text = result.stdout.strip()
            if result.returncode == 0 and text:
                detected_text.append({
                    "timestamp_seconds": sample["timestamp_seconds"],
                    "text": text[:1000],
                })

    return {
        "duration_seconds": duration,
        "shot_count": len(shot_ranges),
        "shot_ranges": shot_ranges,
        "cut_timestamps_seconds": cut_times,
        "sample_frames": sample_frames,
        "motion_summary": motion_summary,
        "similarity_summary": similarity_summary,
        "advanced_similarity": advanced_similarity,
        "detected_text": detected_text,
        "tools": tools,
        "limitations": [
            "Shot boundaries are FFmpeg scene-change candidates and may need human correction.",
            "OCR and optical-flow fields are empty when their optional tools are unavailable.",
            "CLIP/DINO semantic checks are optional and not calibrated exact-replica scores.",
            "These measurements are evidence, not calibrated visual-fidelity scores.",
        ],
    }
