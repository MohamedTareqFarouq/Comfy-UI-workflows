from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    ANALYSIS = "analysis"
    EVALUATION = "evaluation"
    RENDER = "render"


class QualificationState(StrEnum):
    UNAVAILABLE = "unavailable"
    UNQUALIFIED = "unqualified"
    QUALIFIED = "qualified"
    DISABLED = "disabled"


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReferenceRole(StrEnum):
    IDENTITY = "identity"
    ENVIRONMENT = "environment"
    PROP = "prop"
    STYLE = "style"
    COMPOSITION = "composition"
    POSE = "pose"
    START_FRAME = "start_frame"
    END_FRAME = "end_frame"
    MOTION_CONTINUITY = "motion_continuity"


class Capability(BaseModel):
    id: str
    version: str
    media_type: MediaType
    mode: str
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    min_references: int = 0
    max_references: int = 0
    reference_roles: list[ReferenceRole] = Field(default_factory=list)
    model_family: str
    model_files: list[str] = Field(default_factory=list)
    workflow_template: str | None = None
    node_map: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    vram_profile: str = "none"
    resource_class: str = "cpu"
    execution_time_class: str = "short"
    qualification_state: QualificationState = QualificationState.UNQUALIFIED
    limitations: list[str] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    artifact_id: str
    role: ReferenceRole


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    references: list[ArtifactRef] = Field(default_factory=list, max_length=4)
    width: int = Field(default=768, ge=256, le=2048)
    height: int = Field(default=1344, ge=256, le=2048)
    seed: int = Field(default=0, ge=0, lt=2**63)
    candidate_count: int = Field(default=1, ge=1, le=4)
    preferred_capability: str | None = None
    production_session_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)


class VideoGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    references: list[ArtifactRef] = Field(min_length=1, max_length=2)
    motion_description: str = ""
    camera_description: str = ""
    width: int = Field(default=768, ge=256, le=1920)
    height: int = Field(default=1344, ge=256, le=1920)
    frames: int = Field(default=81, ge=9, le=241)
    fps: int = Field(default=16, ge=1, le=60)
    seed: int = Field(default=0, ge=0, lt=2**63)
    candidate_count: int = Field(default=1, ge=1, le=2)
    preferred_capability: str | None = None
    production_session_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def validate_frames(self) -> "VideoGenerationRequest":
        roles = [item.role for item in self.references]
        if roles.count(ReferenceRole.START_FRAME) != 1:
            raise ValueError("video generation requires exactly one start_frame")
        if roles.count(ReferenceRole.END_FRAME) > 1:
            raise ValueError("video generation accepts at most one end_frame")
        return self


class EvaluationRequest(BaseModel):
    artifact_ids: list[str] = Field(min_length=1)
    requirements: dict[str, Any] = Field(default_factory=dict)
    use_visual_critique: bool = False
    production_session_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)


class RenderRequest(BaseModel):
    artifact_ids: list[str] = Field(min_length=1)
    production_session_id: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)


class ApprovalPolicy(BaseModel):
    preset: Literal["autonomous", "approve_first_heavy", "approve_every_heavy", "custom"] = "approve_first_heavy"
    rules: list[dict[str, Any]] = Field(default_factory=list)


class ProductionBudgets(BaseModel):
    max_image_operations: int = Field(default=12, ge=0)
    max_video_operations: int = Field(default=8, ge=0)
    max_candidates_per_operation: int = Field(default=2, ge=1, le=4)
    max_image_retries_per_intent: int = Field(default=2, ge=0)
    max_video_retries_per_intent: int = Field(default=1, ge=0)
    max_consecutive_qa_failures: int = Field(default=2, ge=1)
    max_generated_video_seconds: float = Field(default=30, gt=0)
    max_generation_minutes: float = Field(default=120, gt=0)
    max_storage_bytes: int | None = Field(default=None, gt=0)


class ProductionStartRequest(BaseModel):
    goal: str = Field(min_length=1)
    user_prompt: str = Field(min_length=1)
    reference_artifact_ids: list[str] = Field(default_factory=list)
    target_duration: float = Field(default=10, gt=0, le=3600)
    output_format: str = "mp4"
    style_requirements: list[str] = Field(default_factory=list)
    continuity_requirements: list[str] = Field(default_factory=list)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    budgets: ProductionBudgets = Field(default_factory=ProductionBudgets)
    preferred_capabilities: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=8, max_length=200)


class CancelRequest(BaseModel):
    operation_id: str | None = None
    active_scope: Literal["none", "current", "image", "video", "all"] = "all"
    queued_scope: Literal["preserve", "selected", "image", "video", "all"] = "all"
    queued_operation_ids: list[str] = Field(default_factory=list)
    controller_disposition: Literal["continue", "pause_generation", "pause_session"] = "pause_session"
    reason: str = "cancelled by user"
    actor: str = "user"
    idempotency_key: str = Field(min_length=8, max_length=200)
