from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

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


class Storyboard(BaseModel):
    summary: str
    character_bible: dict[str, Any] = Field(default_factory=dict)
    scenes: list[SceneSpec] = Field(min_length=3, max_length=10)

    @model_validator(mode="after")
    def validate_duration(self) -> "Storyboard":
        total = sum(scene.duration_seconds for scene in self.scenes)
        if not 9.0 <= total <= 33.0:
            raise ValueError("storyboard duration must be approximately 10-30 seconds")
        return self


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
