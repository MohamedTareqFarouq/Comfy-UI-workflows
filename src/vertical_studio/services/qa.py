from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from ..schemas import QAReport, VisualReview
from .media import MediaError, extract_frames, probe_media
from .model_gateway import ModelGateway, ModelUnavailable


class QualityService:
    def __init__(
        self,
        model: ModelGateway | None = None,
        *,
        enable_visual_review: bool = False,
    ):
        self.model = model
        self.enable_visual_review = enable_visual_review

    def inspect_video(
        self,
        video: Path,
        sample_dir: Path,
        *,
        context: dict[str, Any] | None = None,
        reference_images: list[Path] | None = None,
        goal_mode: str = "adaptation",
    ) -> QAReport:
        reasons: list[str] = []
        scores = {"portrait": 0.0, "decodable": 0.0, "duration": 0.0}
        samples: list[str] = []
        advisory_pass = True
        threshold_required = None
        calibration_status = "uncalibrated"
        evaluator = "technical_only"
        try:
            metadata = probe_media(video)
            video_stream = next(
                stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"
            )
            width = int(video_stream.get("width") or 0)
            height = int(video_stream.get("height") or 0)
            duration = float(metadata.get("format", {}).get("duration") or 0)
            scores["decodable"] = 1.0
            scores["portrait"] = 1.0 if height > width else 0.0
            scores["duration"] = 1.0 if duration > 0 else 0.0
            if height <= width:
                reasons.append("video is not portrait-oriented")
            if duration <= 0:
                reasons.append("video duration is invalid")
            sample_paths = extract_frames(video, sample_dir, 5)
            samples = [str(path) for path in sample_paths]

            if self.enable_visual_review and self.model is not None:
                images = [*(reference_images or []), *sample_paths]
                review = self.model.analyze_images(
                    VisualReview,
                    system=(
                        "Review a vertical video using its reference images and sampled frames. "
                        "Judge prompt adherence, identity, wardrobe, environment, anatomy, unwanted "
                        "text, temporal consistency and start/end-frame continuity. Be conservative."
                    ),
                    prompt=json.dumps(context or {}, default=str),
                    image_urls=[self._data_url(path) for path in images],
                )
                advisory_pass = review.advisory_pass
                scores.update(review.scores)
                reasons.extend(review.reasons)
                evaluator = self.model.model

            if goal_mode == "exact_replica":
                threshold_required = 0.95
                reasons.append(
                    "exact replica QA is uncalibrated; require human review before acceptance"
                )
                advisory_pass = False
        except ModelUnavailable as exc:
            advisory_pass = False
            evaluator = "manual_review_required"
            reasons.append(
                f"visual review unavailable: {exc}; choose between available options "
                "(technical-only pass, human contact-sheet review, or retry when Qwen is available)."
            )
        except (MediaError, StopIteration, ValueError, TypeError) as exc:
            reasons.append(str(exc))
        technical = all(scores.get(name, 0.0) >= 1.0 for name in ("portrait", "decodable", "duration"))
        return QAReport(
            technical_pass=technical,
            advisory_pass=advisory_pass and technical,
            scores=scores,
            reasons=reasons,
            sampled_frames=samples,
            goal_mode=goal_mode,
            calibration_status=calibration_status,
            threshold_required=threshold_required,
            evaluator=evaluator,
            reference_images=[str(path) for path in (reference_images or [])],
        )

    @staticmethod
    def _data_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{encoded}"
