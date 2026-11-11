from __future__ import annotations

from pathlib import Path

from vertical_studio.services.media import analyze_reference_video, run_ffmpeg
from vertical_studio.services.model_gateway import ModelUnavailable
from vertical_studio.services.qa import QualityService


def test_reference_analysis_extracts_shot_aware_evidence(tmp_path: Path) -> None:
    source = tmp_path / "two-shots.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])

    evidence = analyze_reference_video(source, tmp_path / "evidence", sample_count=6)

    assert evidence["shot_count"] == 2
    assert evidence["cut_timestamps_seconds"]
    assert len(evidence["sample_frames"]) == 6
    assert any(item["timestamp_seconds"] < 1 for item in evidence["sample_frames"])
    assert any(item["timestamp_seconds"] > 1 for item in evidence["sample_frames"])
    assert all(Path(item["path"]).is_file() for item in evidence["sample_frames"])
    assert {
        "ffmpeg_scene_detection", "opencv_optical_flow",
        "scikit_image_ssim", "tesseract_ocr",
    } <= set(evidence["tools"])
    assert "similarity_summary" in evidence
    assert "advanced_similarity" in evidence
    assert {"clip", "dino", "notes"}.issubset(evidence["advanced_similarity"])


def test_exact_replica_qa_never_auto_passes_without_calibration(tmp_path: Path) -> None:
    source = tmp_path / "portrait.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=270x480:r=10:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])

    report = QualityService().inspect_video(
        source, tmp_path / "qa", goal_mode="exact_replica"
    )

    assert report.technical_pass
    assert not report.advisory_pass
    assert report.calibration_status == "uncalibrated"
    assert report.threshold_required == 0.95
    assert any("human review" in reason for reason in report.reasons)


def test_qa_review_chooses_fallback_when_model_unavailable(tmp_path: Path) -> None:
    class BrokenGateway:
        model = "broken-qwen"

        def analyze_images(self, *args, **kwargs):
            raise ModelUnavailable("Qwen is unavailable at http://127.0.0.1:8080")

    source = tmp_path / "portrait.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=270x480:r=10:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])

    report = QualityService(model=BrokenGateway(), enable_visual_review=True).inspect_video(
        source, tmp_path / "qa-fallback", goal_mode="adaptation"
    )

    assert report.evaluator == "manual_review_required"
    assert any("choose between available options" in reason.lower() for reason in report.reasons)
    assert not report.advisory_pass