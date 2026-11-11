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


class ProductionControlMode(StrEnum):
    HITL = "HITL"
    AUTOPILOT = "Autopilot"


class ProductionGoalMode(StrEnum):
    EXACT_REPLICA = "exact_replica"
    ADAPTATION = "adaptation"
    CREATIVE = "creative"


class ArtifactRef(BaseModel):
    artifact_id: str = Field(description="ID returned by media_import_reference or another artifact-producing tool.")
    role: ReferenceRole = Field(description="How this image is used. Video requires start_frame and may include end_frame.")


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, description="What the image should show. Required; do not leave blank.")
    negative_prompt: str = Field(default="", description="Optional visual content to avoid.")
    references: list[ArtifactRef] = Field(default_factory=list, max_length=4, description="Optional existing artifact IDs and roles. Empty selects text-to-image; 1 to 4 references select a matching edit capability.")
    width: int = Field(default=768, ge=256, le=2048, description="Output width in pixels, from 256 through 2048.")
    height: int = Field(default=1344, ge=256, le=2048, description="Output height in pixels, from 256 through 2048.")
    seed: int = Field(default=0, ge=0, lt=2**63, description="Non-negative reproducibility seed. Zero asks the service to choose a seed.")
    candidate_count: int = Field(default=1, ge=1, le=4, description="Number of image candidates to request, from 1 through 4.")
    preferred_capability: str | None = Field(default=None, description="Optional exact capability ID from media_list_capabilities. The service rejects incompatible choices.")
    production_session_id: str | None = Field(default=None, description="Optional ID returned by production_start. Omit for a standalone operation.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for this intended request; reuse it only to safely repeat the same request.")


class VideoGenerationRequest(BaseModel):
    prompt: str = Field(min_length=1, description="What should happen in the video. Required; do not leave blank.")
    negative_prompt: str = Field(default="", description="Optional visual content or motion to avoid.")
    references: list[ArtifactRef] = Field(min_length=1, max_length=2, description="One start_frame artifact, plus at most one end_frame artifact for first/last-frame generation.")
    motion_description: str = Field(default="", description="Optional description of subject movement.")
    camera_description: str = Field(default="", description="Optional description of camera movement or framing.")
    width: int = Field(default=768, ge=256, le=1920, description="Output width in pixels, from 256 through 1920.")
    height: int = Field(default=1344, ge=256, le=1920, description="Output height in pixels, from 256 through 1920.")
    frames: int = Field(default=81, ge=9, le=241, description="Number of output frames, from 9 through 241.")
    fps: int = Field(default=16, ge=1, le=60, description="Playback frame rate, from 1 through 60 frames per second.")
    seed: int = Field(default=0, ge=0, lt=2**63, description="Non-negative reproducibility seed. Zero asks the service to choose a seed.")
    candidate_count: int = Field(default=1, ge=1, le=2, description="Number of video candidates, either 1 or 2.")
    preferred_capability: str | None = Field(default=None, description="Optional exact capability ID from media_list_capabilities. The service rejects incompatible choices.")
    production_session_id: str | None = Field(default=None, description="Optional ID returned by production_start. Omit for a standalone operation.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for this intended request; reuse it only to safely repeat the same request.")

    @model_validator(mode="after")
    def validate_frames(self) -> "VideoGenerationRequest":
        roles = [item.role for item in self.references]
        if roles.count(ReferenceRole.START_FRAME) != 1:
            raise ValueError("video generation requires exactly one start_frame")
        if roles.count(ReferenceRole.END_FRAME) > 1:
            raise ValueError("video generation accepts at most one end_frame")
        return self


class EvaluationRequest(BaseModel):
    artifact_ids: list[str] = Field(min_length=1, description="One or more existing artifact IDs to evaluate.")
    goal_mode: ProductionGoalMode = Field(default=ProductionGoalMode.ADAPTATION, description="Goal-specific visual acceptance criteria; exact_replica requires human review until a calibrated evaluator is available.")
    requirements: dict[str, Any] = Field(default_factory=dict, description="Optional machine-checkable requirements, such as expected width, height, or duration.")
    use_visual_critique: bool = Field(default=False, description="Whether to request visual critique when that capability is available.")
    production_session_id: str | None = Field(default=None, description="Optional ID returned by production_start.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for this intended evaluation request.")


class RenderRequest(BaseModel):
    artifact_ids: list[str] = Field(min_length=1, description="Approved video artifact IDs in the exact order they should appear in the rendered sequence.")
    production_session_id: str | None = Field(default=None, description="Optional ID returned by production_start.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for this intended render request.")


class ApprovalPolicy(BaseModel):
    preset: Literal["autonomous", "approve_first_heavy", "approve_every_heavy", "custom"] = Field(default="approve_first_heavy", description="Approval behavior: autonomous, approve the first heavy job, approve every heavy job, or use custom rules.")
    rules: list[dict[str, Any]] = Field(default_factory=list, description="Rules used only with the custom preset.")


class ProductionBudgets(BaseModel):
    max_image_operations: int = Field(default=12, ge=0, description="Maximum image operations allowed in this production session.")
    max_video_operations: int = Field(default=16, ge=0, description="Maximum video operations allowed in this production session.")
    max_candidates_per_operation: int = Field(default=2, ge=1, le=4, description="Maximum candidates in one generation operation, from 1 through 4.")
    max_image_retries_per_intent: int = Field(default=2, ge=0, description="Maximum image retries for the same intent.")
    max_video_retries_per_intent: int = Field(default=1, ge=0, description="Maximum video retries for the same intent.")
    max_consecutive_qa_failures: int = Field(default=2, ge=1, description="Pause or repair after this many consecutive QA failures.")
    max_generated_video_seconds: float = Field(default=75, gt=0, description="Maximum total generated video duration, in seconds.")
    max_generation_minutes: float = Field(default=120, gt=0, description="Maximum generation time budget, in minutes.")
    max_storage_bytes: int | None = Field(default=None, gt=0, description="Optional maximum managed storage in bytes. Omit for no explicit byte cap.")


class ProductionStartRequest(BaseModel):
    goal: str = Field(min_length=1, description="Short outcome the production session should deliver.")
    user_prompt: str = Field(min_length=1, description="The user's creative instructions. Required; preserve the requested content and constraints.")
    reference_artifact_ids: list[str] = Field(default_factory=list, description="Optional existing artifact IDs to use as references.")
    target_duration: float = Field(default=10, gt=0, le=3600, description="Requested final duration in seconds, greater than 0 and no more than 3600.")
    output_format: str = Field(default="mp4", description="Requested final container format; currently use mp4.")
    style_requirements: list[str] = Field(default_factory=list, description="Optional visual style constraints.")
    continuity_requirements: list[str] = Field(default_factory=list, description="Optional requirements for consistent characters, props, or locations.")
    control_mode: ProductionControlMode = Field(default=ProductionControlMode.HITL, description="HITL pauses at expensive gates; Autopilot may auto-advance only when acceptance criteria are satisfied.")
    goal_mode: ProductionGoalMode = Field(default=ProductionGoalMode.ADAPTATION, description="Whether the task is exact replica, adaptation, or a creative brief. Exact replica requires stronger visual evidence and stricter thresholds.")
    reference_analysis_required: bool = Field(default=True, description="If a reference exists, the system must produce an evidence-based analysis pass before storyboards or video are approved.")
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy, description="When the service must wait for human approval before expensive work.")
    budgets: ProductionBudgets = Field(default_factory=ProductionBudgets, description="Limits for generation counts, retries, duration, and storage.")
    preferred_capabilities: list[str] = Field(default_factory=list, description="Optional exact capability IDs from media_list_capabilities.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for creating this session; reuse only to safely repeat the same start request.")

    @model_validator(mode="after")
    def default_autopilot_approval_policy(self) -> "ProductionStartRequest":
        if (
            self.control_mode == ProductionControlMode.AUTOPILOT
            and "approval_policy" not in self.model_fields_set
        ):
            self.approval_policy = ApprovalPolicy(preset="autonomous")
        return self


class ProductionTaskRequest(BaseModel):
    stage: Literal[
        "analyze", "plan", "asset", "image", "video", "qa", "repair", "render", "finish"
    ] = Field(description="Task stage: analyze, plan, asset, image, video, qa, repair, render, or finish.")
    objective: str = Field(min_length=1, description="One specific outcome for this stage. Required.")
    dependency_task_ids: list[str] = Field(default_factory=list, description="Task IDs that must complete before this task is ready.")
    input_artifact_ids: list[str] = Field(default_factory=list, description="Existing artifact IDs this task should use.")
    acceptance_criteria: list[str] = Field(default_factory=list, description="Observable conditions that define task completion.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for creating this task; reuse only to safely repeat the same request.")


class CancelRequest(BaseModel):
    operation_id: str | None = Field(default=None, description="Optional specific operation ID to cancel.")
    active_scope: Literal["none", "current", "image", "video", "all"] = Field(default="all", description="Which running work to cancel: none, current, image, video, or all.")
    queued_scope: Literal["preserve", "selected", "image", "video", "all"] = Field(default="all", description="Which queued work to remove: preserve, selected IDs, image, video, or all.")
    queued_operation_ids: list[str] = Field(default_factory=list, description="Operation IDs used when queued_scope is selected.")
    controller_disposition: Literal["continue", "pause_generation", "pause_session"] = Field(default="pause_session", description="After cancellation, continue, pause generation, or pause the whole production session.")
    reason: str = Field(default="cancelled by user", description="Human-readable reason recorded with the cancellation.")
    actor: str = Field(default="user", description="Who requested cancellation, usually user.")
    idempotency_key: str = Field(min_length=8, max_length=200, description="Unique key for this cancellation request.")
