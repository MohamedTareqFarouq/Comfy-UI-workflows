from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionMode(StrEnum):
    SIMULATION = "simulation"
    REAL = "real"


class SourceMode(StrEnum):
    DESCRIPTION = "description"
    REFERENCE = "reference"


class ProjectState(StrEnum):
    DRAFT = "draft"
    REFERENCE_ANALYSIS = "reference_analysis"
    REFERENCE_REVIEW = "reference_review"
    RESEARCH_NEEDED = "research_needed"
    PLANNING = "planning"
    STORYBOARD_REVIEW = "storyboard_review"
    REFERENCES = "references"
    CONTACT_SHEET_REVIEW = "contact_sheet_review"
    SCENES = "scenes"
    FINAL_REVIEW = "final_review"
    RENDERING = "rendering"
    COMPLETE = "complete"
    FAILED = "failed"
    PAUSED = "paused"


class SceneState(StrEnum):
    PLANNED = "planned"
    KEYFRAMES_QUEUED = "keyframes_queued"
    KEYFRAMES_READY = "keyframes_ready"
    KEYFRAMES_APPROVED = "keyframes_approved"
    VIDEO_QUEUED = "video_queued"
    VIDEO_READY = "video_ready"
    QA_FAILED = "qa_failed"
    APPROVED = "approved"
    FAILED = "failed"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    REFERENCE_ANALYSIS = "reference_analysis"
    CANONICAL_ASSET = "canonical_asset"
    CHARACTER_SHEET = "character_sheet"
    KEYFRAME = "keyframe"
    VIDEO = "video"
    QA = "qa"
    RENDER = "render"


class ArtifactKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    REPORT = "report"
    RENDER = "render"


class ProjectCreate(BaseModel):
    idea: str = Field(min_length=3, max_length=4000)
    title: str | None = Field(default=None, max_length=160)
    duration_seconds: int = Field(default=15, ge=10, le=30)
    visual_style: str = Field(default="cinematic 3D cartoon", max_length=500)
    research_required: bool = False
    reference_paths: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    source_mode: SourceMode = SourceMode.DESCRIPTION


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    idea: str | None = Field(default=None, min_length=3, max_length=4000)
    duration_seconds: int | None = Field(default=None, ge=10, le=30)
    visual_style: str | None = Field(default=None, min_length=1, max_length=500)


class PlanRequest(BaseModel):
    feedback: str = Field(default="", max_length=4000)


class Project(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    idea: str
    duration_seconds: int
    visual_style: str
    research_required: bool
    source_mode: SourceMode = SourceMode.DESCRIPTION
    state: ProjectState
    execution_mode: ExecutionMode
    created_at: str
    updated_at: str


class SceneSpec(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    narrative_purpose: str = Field(min_length=1, max_length=1200)
    duration_seconds: float = Field(ge=1.0, le=6.0)
    start_composition: str = Field(min_length=1, max_length=1600)
    end_composition: str = Field(min_length=1, max_length=1600)
    subject_placement: str = Field(default="centered within the vertical safe area")
    motion: str = Field(min_length=1, max_length=1600)
    camera: str = Field(default="stable camera")
    continuity: list[str] = Field(default_factory=list)
    negative_constraints: list[str] = Field(default_factory=list)
    safe_area: str = Field(default="keep essential subjects inside the center 80%")
    continuity_group: str = Field(default="main", min_length=1, max_length=80)
    starts_new_continuity: bool = False
    continuity_reason: str = Field(default="continuous visual action", max_length=500)
    transition_from_previous: Literal["continuous", "cut", "crossfade", "new_sequence"] = "continuous"
    reference_keys: list[str] = Field(default_factory=list)
    characters_present: list[str] = Field(default_factory=list)
    environment_key: str | None = None
    opening_environment_key: str | None = None
    closing_environment_key: str | None = None
    important_props: list[str] = Field(default_factory=list)
    character_count: int | None = Field(default=None, ge=0, le=8)
    opening_state: str = Field(default="", max_length=1200)
    closing_state: str = Field(default="", max_length=1200)
    object_owner: str = Field(default="", max_length=500)
    handoff_to_next_scene: str = Field(default="", max_length=1200)


class CanonicalAssetSpec(BaseModel):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    role: Literal["character", "environment", "prop", "style"]
    label: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=2000)
    description: str = Field(default="", max_length=1200)


class ReferenceStyleProfile(BaseModel):
    medium: str = Field(min_length=1, max_length=300)
    visual_language: str = Field(min_length=1, max_length=1600)
    palette: list[str] = Field(default_factory=list, max_length=12)
    lighting: str = Field(default="", max_length=800)
    materials: str = Field(default="", max_length=800)
    avoid: list[str] = Field(default_factory=list, max_length=16)


class ReferenceMotionProfile(BaseModel):
    animation_style: str = Field(min_length=1, max_length=1000)
    cadence: str = Field(default="", max_length=500)
    physicality: str = Field(default="", max_length=800)
    camera_language: str = Field(default="", max_length=800)
    transition_language: str = Field(default="", max_length=500)


class ReferenceBlueprint(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    style: ReferenceStyleProfile
    motion: ReferenceMotionProfile
    character_bible: dict[str, Any] = Field(default_factory=dict)
    canonical_assets: list[CanonicalAssetSpec] = Field(default_factory=list, max_length=16)
    scenes: list[SceneSpec] = Field(min_length=3, max_length=10)
    evidence_summary: list[str] = Field(default_factory=list, max_length=32)
    ambiguities: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def normalize_continuity_boundaries(self) -> "ReferenceBlueprint":
        normalized: list[SceneSpec] = []
        seen: set[str] = set()
        previous: str | None = None
        for index, scene in enumerate(self.scenes):
            group = scene.continuity_group
            if group != previous and group in seen:
                base = group[:72]
                suffix = index + 1
                candidate = f"{base}-{suffix}"
                while candidate in seen:
                    suffix += 1
                    candidate = f"{base}-{suffix}"
                group = candidate
            starts_new = index == 0 or group != previous
            transition = scene.transition_from_previous
            if index == 0:
                transition = "new_sequence"
            elif starts_new and transition == "continuous":
                transition = "new_sequence"
            elif not starts_new and transition == "new_sequence":
                transition = "continuous"
            normalized.append(
                scene.model_copy(
                    update={
                        "continuity_group": group,
                        "starts_new_continuity": starts_new,
                        "transition_from_previous": transition,
                    }
                )
            )
            seen.add(group)
            previous = group
        self.scenes = normalized
        return self


    @model_validator(mode="after")
    def reconcile_canonical_asset_keys(self) -> "ReferenceBlueprint":
        assets = list(self.canonical_assets)
        aliases: dict[str, str] = {}

        def token(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", value.casefold())

        def resolve(key: str | None, role: str) -> str | None:
            if not key:
                return None
            if key in aliases:
                return aliases[key]
            existing = next((asset for asset in assets if asset.key == key), None)
            if existing is not None and existing.role == role:
                return key
            candidates = [asset for asset in assets if asset.role == role]
            normalized = token(key)
            matches = [
                asset for asset in candidates
                if normalized in {token(asset.key), token(asset.label)}
            ]
            if len(matches) == 1:
                aliases[key] = matches[0].key
                return matches[0].key
            if len(candidates) == 1:
                aliases[key] = candidates[0].key
                return candidates[0].key
            if len(assets) >= 16:
                return candidates[0].key if candidates else None

            safe_key = re.sub(r"[^a-z0-9_-]+", "_", key.casefold()).strip("_-")
            safe_key = (safe_key or f"{role}_{len(assets) + 1:03d}")[:80]
            base = safe_key[:72]
            suffix = 2
            while any(asset.key == safe_key for asset in assets):
                safe_key = f"{base}_{suffix}"
                suffix += 1
            label = key.replace("_", " ").replace("-", " ").title()[:160]
            directive = {
                "character": "one full-body character on a plain background",
                "environment": "one clean empty establishing view",
                "prop": "one isolated prop on a plain background",
            }[role]
            asset = CanonicalAssetSpec(
                key=safe_key,
                role=role,
                label=label or role.title(),
                prompt=(
                    f"{self.style.medium}; {self.style.visual_language}. {directive}; "
                    f"reconstruct {label or key} faithfully from the source evidence."
                ),
                description=f"Source-derived {role} referenced by the analyzed scenes as {key}.",
            )
            assets.append(asset)
            aliases[key] = asset.key
            return asset.key

        repaired: list[SceneSpec] = []
        for scene in self.scenes:
            characters = [
                value for key in scene.characters_present
                if (value := resolve(key, "character"))
            ]
            props = [
                value for key in scene.important_props
                if (value := resolve(key, "prop"))
            ]
            environment = resolve(scene.environment_key, "environment")
            opening = resolve(
                scene.opening_environment_key or scene.environment_key,
                "environment",
            )
            closing = resolve(
                scene.closing_environment_key
                or scene.opening_environment_key
                or scene.environment_key,
                "environment",
            )
            references = []
            for key in scene.reference_keys:
                resolved = aliases.get(key)
                if resolved is None and any(asset.key == key for asset in assets):
                    resolved = key
                if resolved and resolved not in references:
                    references.append(resolved)
            for key in [*characters, *props, opening, closing]:
                if key and key not in references:
                    references.append(key)
            repaired.append(scene.model_copy(update={
                "reference_keys": references,
                "characters_present": list(dict.fromkeys(characters)),
                "important_props": list(dict.fromkeys(props)),
                "environment_key": environment or opening,
                "opening_environment_key": opening,
                "closing_environment_key": closing,
            }))
        self.canonical_assets = assets
        self.scenes = repaired
        return self

    def as_storyboard(self) -> "Storyboard":
        style_text = "; ".join(filter(None, [
            self.style.medium,
            self.style.visual_language,
            f"palette: {', '.join(self.style.palette)}" if self.style.palette else "",
            f"lighting: {self.style.lighting}" if self.style.lighting else "",
            f"materials: {self.style.materials}" if self.style.materials else "",
        ]))
        character_assets = [
            asset for asset in self.canonical_assets if asset.role == "character"
        ]
        return Storyboard(
            summary=self.summary,
            requires_character_sheet=bool(character_assets),
            character_sheet_prompt=(
                "Create a consistent turnaround sheet for the recurring characters, "
                f"faithfully preserving the analyzed reference style: {style_text}"
                if character_assets else ""
            ),
            character_bible={
                **self.character_bible,
                "reference_visual_style": self.style.model_dump(),
                "reference_motion_style": self.motion.model_dump(),
            },
            canonical_assets=self.canonical_assets,
            scenes=self.scenes,
        )


class Storyboard(BaseModel):
    summary: str
    requires_character_sheet: bool
    character_sheet_prompt: str = Field(max_length=2000)
    character_bible: dict[str, Any] = Field(default_factory=dict)
    canonical_assets: list[CanonicalAssetSpec] = Field(default_factory=list, max_length=16)
    scenes: list[SceneSpec] = Field(min_length=3, max_length=10)

    @model_validator(mode="after")
    def validate_duration(self) -> "Storyboard":
        total = sum(scene.duration_seconds for scene in self.scenes)
        if not 9.0 <= total <= 33.0:
            raise ValueError("storyboard duration must be approximately 10-30 seconds")
        seen: set[str] = set()
        previous = ""
        for index, scene in enumerate(self.scenes):
            group = scene.continuity_group
            if index == 0 and not scene.starts_new_continuity:
                raise ValueError("the first scene must start a new continuity group")
            if index and group != previous and not scene.starts_new_continuity:
                raise ValueError("a changed continuity group must start with a T2I anchor")
            if index and group == previous and scene.starts_new_continuity:
                raise ValueError("a new T2I anchor must use a new continuity group")
            if group in seen and group != previous:
                raise ValueError("continuity groups must be contiguous")
            seen.add(group)
            previous = group
        if self.requires_character_sheet and not self.character_sheet_prompt.strip():
            raise ValueError("character_sheet_prompt is required when a character sheet is needed")
        asset_keys = [asset.key for asset in self.canonical_assets]
        if len(asset_keys) != len(set(asset_keys)):
            raise ValueError("canonical asset keys must be unique")
        if asset_keys:
            assets_by_key = {asset.key: asset for asset in self.canonical_assets}
            known = set(assets_by_key)
            previous_closing_environment: str | None = None
            previous_group: str | None = None
            for scene in self.scenes:
                opening_environment = (
                    scene.opening_environment_key or scene.environment_key
                )
                closing_environment = (
                    scene.closing_environment_key or opening_environment
                )
                declared = set(scene.reference_keys)
                declared.update(scene.characters_present)
                declared.update(scene.important_props)
                declared.update(
                    key for key in (opening_environment, closing_environment) if key
                )
                unknown = declared - known
                if unknown:
                    raise ValueError(
                        f"scene references unknown canonical assets: {chr(44).join(sorted(unknown))}"
                    )
                for key in scene.characters_present:
                    if assets_by_key[key].role != "character":
                        raise ValueError(f"scene character {key} must reference a character asset")
                for key in scene.important_props:
                    if assets_by_key[key].role != "prop":
                        raise ValueError(f"scene prop {key} must reference a prop asset")
                for key in (opening_environment, closing_environment):
                    if key and assets_by_key[key].role != "environment":
                        raise ValueError(f"scene environment {key} must reference an environment asset")
                if any(asset.role == "environment" for asset in self.canonical_assets):
                    if not opening_environment or not closing_environment:
                        raise ValueError(
                            "every scene must define opening and closing environment locks"
                        )
                if (
                    previous_group == scene.continuity_group
                    and not scene.starts_new_continuity
                    and previous_closing_environment
                    and opening_environment != previous_closing_environment
                ):
                    raise ValueError(
                        "a connected scene opening environment must match the previous closing environment"
                    )
                opening_required = set(scene.characters_present) | set(scene.important_props)
                if opening_environment:
                    opening_required.add(opening_environment)
                if len(opening_required) > 4:
                    raise ValueError(
                        "opening keyframe exceeds the four-reference model limit; split the scene"
                    )
                closing_required = set(scene.characters_present) | set(scene.important_props)
                if closing_environment != opening_environment and closing_environment:
                    closing_required.add(closing_environment)
                if len(closing_required) > 3:
                    raise ValueError(
                        "closing keyframe exceeds three canonical references plus the opening frame; split the scene"
                    )
                previous_closing_environment = closing_environment
                previous_group = scene.continuity_group
        return self


class CanonicalAsset(BaseModel):
    id: str
    project_id: str
    key: str
    role: Literal["character", "environment", "prop", "style"]
    label: str
    prompt: str
    description: str = ""
    artifact_id: str | None = None
    approved: bool = False
    created_at: str
    updated_at: str


class Scene(BaseModel):
    id: str
    project_id: str
    position: int
    revision: int
    state: SceneState
    spec: SceneSpec
    continuity_stale: bool = False
    selected_start_artifact_id: str | None = None
    selected_end_artifact_id: str | None = None
    selected_video_artifact_id: str | None = None
    created_at: str
    updated_at: str


class Job(BaseModel):
    id: str
    project_id: str
    scene_id: str | None
    kind: JobKind
    state: JobState
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str


class Artifact(BaseModel):
    id: str
    project_id: str
    scene_id: str | None
    attempt_id: str | None
    kind: ArtifactKind
    path: str
    sha256: str
    metadata: dict[str, Any]
    created_at: str


class VisualReview(BaseModel):
    advisory_pass: bool
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class QAReport(BaseModel):
    technical_pass: bool
    advisory_pass: bool
    scores: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    sampled_frames: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    approved: bool
    notes: str = Field(default="", max_length=2000)


class ArtifactSelection(BaseModel):
    start_artifact_id: str | None = None
    end_artifact_id: str | None = None
    video_artifact_id: str | None = None


class VideoRetryPromptProposal(BaseModel):
    stage: Literal["video"]
    video_prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=1800)
    rationale: str = Field(default="", max_length=600)


class KeyframeRetryPromptProposal(BaseModel):
    stage: Literal["keyframe"]
    start_prompt: str | None = Field(default=None, max_length=4000)
    end_prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(default="", max_length=1800)
    rationale: str = Field(default="", max_length=600)


class RetryPromptBundle(BaseModel):
    stage: Literal["keyframe", "video"]
    start_prompt: str | None = Field(default=None, max_length=8000)
    end_prompt: str | None = Field(default=None, max_length=8000)
    video_prompt: str | None = Field(default=None, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)
    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_stage_prompts(self) -> "RetryPromptBundle":
        if self.stage == "keyframe" and not self.end_prompt:
            raise ValueError("keyframe prompt bundles require an end prompt")
        if self.stage == "video" and not self.video_prompt:
            raise ValueError("video prompt bundles require a video prompt")
        return self


class RetryPromptRevisionRequest(BaseModel):
    stage: Literal["keyframe", "video"]
    feedback: str = Field(min_length=1, max_length=4000)
    start_prompt: str | None = Field(default=None, max_length=8000)
    end_prompt: str | None = Field(default=None, max_length=8000)
    video_prompt: str | None = Field(default=None, max_length=8000)
    negative_prompt: str = Field(default="", max_length=4000)


class CanonicalAssetPromptProposal(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(default="", max_length=600)


class CanonicalAssetPromptRevisionRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=4000)
    prompt: str = Field(min_length=1, max_length=8000)


class CanonicalAssetRetryRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    feedback: str = Field(default="", max_length=4000)
    revision_rationale: str = Field(default="", max_length=2000)


class RetryRequest(BaseModel):
    changed_prompt: str | None = Field(default=None, max_length=8000)
    changed_start_prompt: str | None = Field(default=None, max_length=8000)
    changed_end_prompt: str | None = Field(default=None, max_length=8000)
    changed_negative_prompt: str | None = Field(default=None, max_length=4000)
    feedback: str = Field(default="", max_length=4000)
    revision_rationale: str = Field(default="", max_length=2000)
    new_seed: bool = True


class RenderRequest(BaseModel):
    export_path: str | None = None
