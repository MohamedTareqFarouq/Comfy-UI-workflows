from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionMode(StrEnum):
    SIMULATION = "simulation"
    REAL = "real"


class ProjectState(StrEnum):
    DRAFT = "draft"
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
            known = set(asset_keys)
            for scene in self.scenes:
                unknown = set(scene.reference_keys) - known
                if unknown:
                    raise ValueError(
                        f"scene references unknown canonical assets: {chr(44).join(sorted(unknown))}"
                    )
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


class RetryRequest(BaseModel):
    changed_prompt: str | None = Field(default=None, max_length=4000)
    new_seed: bool = True


class RenderRequest(BaseModel):
    export_path: str | None = None
