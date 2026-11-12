from __future__ import annotations

import base64
import math
import mimetypes
from pathlib import Path

from ..schemas import (
    CanonicalAssetSpec,
    Project,
    ReferenceBlueprint,
    ReferenceMotionProfile,
    ReferenceStyleProfile,
    SceneSpec,
)
from .model_gateway import ModelGateway


class ReferenceAnalyzer:
    """Convert timestamped source evidence into the existing storyboard vocabulary."""

    def __init__(self, model: ModelGateway, *, simulation: bool = False):
        self.model = model
        self.simulation = simulation

    def analyze(
        self,
        project: Project,
        frames: list[Path],
        *,
        source_name: str,
        technical_evidence: dict | None = None,
    ) -> tuple[ReferenceBlueprint, dict]:
        evidence = technical_evidence or {}
        if self.simulation:
            blueprint = self._simulation_blueprint(project, source_name, frames)
            return blueprint, {
                "model": "simulation-reference-analyzer",
                "source_name": source_name,
                "evidence_frames": [str(path) for path in frames],
                "technical_evidence": evidence,
                "parsed_response": blueprint.model_dump(mode="json"),
            }

        blueprint = self.model.analyze_images(
            ReferenceBlueprint,
            system=(
                "Analyze the source as evidence, then choose the production path from the user's request. "
                "Set story_mode='recreate' when the user asks for a faithful remake or gives no story-change direction. "
                "Set story_mode='adapt' when the user asks to retell, remix, reinterpret, reorder, or change the story. "
                "For adapt, use the requested new narrative direction rather than copying the source scene order; "
                "retain only source-supported characters, props, and locations the user wants preserved. "
                "Write a concise transformation_summary. Keep style analysis separate: style describes the observed "
                "source, while target_visual_style is empty when preserving it and contains the requested target "
                "visual treatment when the user asks for a style change. A non-default project visual style is an "
                "explicit target; do not let an unspecified generic default override the source style. Translate "
                "named-style shorthand into general visual attributes rather than imitating a named artist or studio. "
                "Then produce a reusable production blueprint from ordered frames "
                "sampled across one source video. Identify recurring physical characters as "
                "separate canonical character assets, each distinct environment as a separate "
                "environment asset, and important props as prop assets. Infer the rendering "
                "medium, palette, materials, lighting and animation language from visible "
                "evidence. Keep that source profile in style. Build 3-10 scenes whose durations sum to the requested "
                "duration. For recreate, preserve chronological source beats. For adapt, make the scenes advance "
                "the requested changed story while preserving the selected continuity anchors. The first scene starts a continuity group. Start a new group for a "
                "cut, location change, time jump, or radical composition change. Use only known "
                "canonical asset keys in scene references. Preserve source evidence rather than "
                "inventing unsupported details. Frames are numbered in chronological order starting "
                "at 1. For every canonical asset, set evidence_frame_indices to the 1-2 frame numbers "
                "that most clearly show that specific asset. Never cite a frame that does not visibly "
                "support the asset. Keep every non-prompt string under 25 words and every canonical "
                "asset prompt under 70 words. Do not repeat facts across fields or add redundant list "
                "items. Return one complete JSON object and always close every array and object."
            ),
            prompt=(
                f"Source: {source_name}. Target duration: {project.duration_seconds} seconds. "
                f"Project request: {project.idea}. Requested project visual style: {project.visual_style}. "
                f"Measured reference evidence: {evidence}. "
                "Frames are chronological, shot-aware samples; use measured cut boundaries, timestamps, motion, "
                "and detected text as cues to inspect rather than unquestionable labels. Decide recreate versus adapt from the request. "
                "Describe the observed source style and motion, then produce the requested scene plan, cast, "
                "environments, props, compositions, actions, camera language and continuity."
            ),
            image_urls=[self._data_url(path) for path in frames],
            max_tokens=6144,
        )
        return blueprint, {
            "model": getattr(self.model, "vision_model", self.model.model),
            "source_name": source_name,
            "evidence_frames": [str(path) for path in frames],
            "technical_evidence": evidence,
            "parsed_response": blueprint.model_dump(mode="json"),
        }

    @staticmethod
    def _data_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _simulation_blueprint(
        project: Project, source_name: str, frames: list[Path]
    ) -> ReferenceBlueprint:
        count = max(3, min(10, math.ceil(project.duration_seconds / 3)))
        duration = project.duration_seconds / count
        style = ReferenceStyleProfile(
            medium="stylized 3D animation",
            visual_language=(
                "Rounded character forms, simplified readable silhouettes, restrained detail "
                "and a coherent cinematic cartoon finish inferred from the reference."
            ),
            palette=["warm amber", "muted teal", "soft cream"],
            lighting="Soft motivated lighting with broad shadows and consistent color temperature.",
            materials="Matte stylized materials with restrained highlights.",
            avoid=["photorealistic skin", "live action", "style drift"],
        )
        motion = ReferenceMotionProfile(
            animation_style=(
                "Readable staged actions with stable silhouettes, gentle anticipation and "
                "controlled follow-through."
            ),
            cadence="Smooth continuous movement with one clear action beat per shot.",
            physicality="Moderate stylized weight without extreme squash or stretch.",
            camera_language="Stable cinematic framing with restrained pushes and pans.",
            transition_language="Direct cuts between distinct story beats.",
        )
        evidence_indices = list(dict.fromkeys([
            1,
            len(frames),
        ]))[:2]
        request = project.idea.lower()
        adaptation_cues = (
            "adapt", "reimagine", "re-imagine", "reinterpret", "retell", "remix",
            "alternate story", "different story", "change the story", "what if",
        )
        story_mode = "adapt" if any(cue in request for cue in adaptation_cues) else "recreate"
        requested_style = project.visual_style.strip()
        target_visual_style = (
            requested_style
            if requested_style and requested_style.lower() != "cinematic 3d cartoon"
            else ""
        )
        assets = [
            CanonicalAssetSpec(
                key="protagonist",
                role="character",
                label="Reference protagonist",
                prompt=(
                    f"{style.medium}; {style.visual_language}. One clean three-quarter full-body "
                    "identity reference matching the source protagonist, consistent face, hair, "
                    "wardrobe and proportions, plain background, one person only."
                ),
                evidence_frame_indices=evidence_indices,
                description="The recurring main character reconstructed from source evidence.",
            ),
            CanonicalAssetSpec(
                key="primary_environment",
                role="environment",
                label="Reference environment",
                prompt=(
                    f"{style.medium}; {style.visual_language}. Clean empty establishing view of "
                    "the recurring source environment, preserving architecture, palette and lighting."
                ),
                description="The recurring source environment and lighting anchor.",
                evidence_frame_indices=evidence_indices,
            ),
        ]
        scenes = [
            SceneSpec(
                title=f"Reference shot {index}",
                narrative_purpose=f"Reconstruct chronological source beat {index} from {source_name}",
                duration_seconds=duration,
                start_composition=(
                    f"Vertical 9:16 {style.medium}; match the opening composition of source beat {index}"
                ),
                end_composition=(
                    f"Vertical 9:16 {style.medium}; match the closing composition of source beat {index}"
                ),
                motion=(
                    f"{motion.animation_style} Recreate the principal action progression of "
                    f"source beat {index}."
                ),
                camera=motion.camera_language,
                continuity=["same identity", "same wardrobe", "source-derived palette"],
                negative_constraints=style.avoid,
                continuity_group="reference-sequence",
                starts_new_continuity=index == 1,
                continuity_reason=(
                    "Opening source anchor" if index == 1 else "Chronological source continuity"
                ),
                transition_from_previous="new_sequence" if index == 1 else "continuous",
                reference_keys=["protagonist", "primary_environment"],
                characters_present=["protagonist"],
                environment_key="primary_environment",
                opening_environment_key="primary_environment",
                closing_environment_key="primary_environment",
                character_count=1,
                opening_state=f"The protagonist begins source beat {index}.",
                closing_state=f"The protagonist completes source beat {index}.",
            )
            for index in range(1, count + 1)
        ]
        return ReferenceBlueprint(
            summary=f"Reference-grounded reconstruction of {source_name}",
            story_mode=story_mode,
            transformation_summary=(
                project.idea if story_mode == "adapt" else ""
            ),
            target_visual_style=target_visual_style,
            style=style,
            motion=motion,
            character_bible={
                "identity_rule": "Preserve source-derived face, silhouette, wardrobe and proportions.",
                "evidence_frame_count": len(frames),
            },
            canonical_assets=assets,
            scenes=scenes,
            evidence_summary=[
                f"{len(frames)} chronological source frames were inspected.",
                "Character, environment, style and motion descriptions remain reviewable.",
            ],
            ambiguities=[],
        )
