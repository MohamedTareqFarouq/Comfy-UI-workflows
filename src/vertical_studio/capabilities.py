from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .headless_schemas import (
    ArtifactRef,
    Capability,
    MediaType,
    QualificationState,
    ReferenceRole,
)


class RoutingError(ValueError):
    def __init__(self, code: str, message: str, conflicts: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.conflicts = conflicts or []

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "conflicts": self.conflicts}


def _capabilities() -> list[Capability]:
    image_roles = [
        ReferenceRole.IDENTITY, ReferenceRole.ENVIRONMENT, ReferenceRole.PROP,
        ReferenceRole.STYLE, ReferenceRole.COMPOSITION, ReferenceRole.POSE,
        ReferenceRole.MOTION_CONTINUITY,
    ]
    common_image = dict(
        media_type=MediaType.IMAGE,
        optional_inputs=["negative_prompt", "seed", "candidate_count"],
        reference_roles=image_roles,
        model_family="Flux Klein 4B",
        model_files=["flux-klein-4b"],
        constraints={"width": [256, 2048], "height": [256, 2048], "max_candidates": 4},
        vram_profile="16gb",
        resource_class="gpu_image",
        execution_time_class="medium",
    )
    return [
        Capability(
            id="flux_klein.t2i.v1", version="1.0.0", mode="text_to_image",
            required_inputs=["prompt"], min_references=0, max_references=0,
            workflow_template="flux_t2i_api.json", node_map="flux_t2i_api",
            **common_image,
        ),
        Capability(
            id="flux_klein.single_reference.v1", version="1.0.0", mode="single_reference",
            required_inputs=["prompt", "references"], min_references=1, max_references=1,
            workflow_template="flux_klein_multiref_1_api.json", node_map="flux_klein_multiref_1_api",
            **common_image,
        ),
        Capability(
            id="flux_klein.multi_reference.v1", version="1.0.0", mode="multi_reference",
            required_inputs=["prompt", "references"], min_references=2, max_references=4,
            workflow_template="flux_klein_multireference_4b_api.json", node_map="flux_klein_multireference_4b_api",
            limitations=["Uses the registered one-through-four reference workflow family."],
            **common_image,
        ),
        Capability(
            id="wan22.i2v.v1", version="1.0.0", media_type=MediaType.VIDEO,
            mode="image_to_video", required_inputs=["prompt", "start_frame"],
            optional_inputs=["negative_prompt", "motion_description", "camera_description", "seed"],
            min_references=1, max_references=1, reference_roles=[ReferenceRole.START_FRAME],
            model_family="Wan 2.2", model_files=["wan2.2"],
            workflow_template="wan22_i2v_api.json", node_map="wan22_i2v_api",
            constraints={"width": [256, 1920], "height": [256, 1920], "frames": [9, 241], "max_candidates": 2},
            vram_profile="16gb", resource_class="gpu_video", execution_time_class="long",
            limitations=["Start-frame conditioning only; video extension and V2V are not supported."],
        ),
        Capability(
            id="wan22.flf2v.v1", version="1.0.0", media_type=MediaType.VIDEO,
            mode="first_last_frame_to_video", required_inputs=["prompt", "start_frame", "end_frame"],
            optional_inputs=["negative_prompt", "motion_description", "camera_description", "seed"],
            min_references=2, max_references=2,
            reference_roles=[ReferenceRole.START_FRAME, ReferenceRole.END_FRAME],
            model_family="Wan 2.2", model_files=["wan2.2"],
            workflow_template="wan14b_lightx2v_flf2v_api.json", node_map="wan14b_lightx2v_flf2v_api",
            constraints={"width": [256, 1920], "height": [256, 1920], "frames": [9, 241], "max_candidates": 2},
            vram_profile="16gb", resource_class="gpu_video", execution_time_class="long",
        ),
        Capability(
            id="qwen.reference_analysis.v1", version="1.0.0", media_type=MediaType.ANALYSIS,
            mode="reference_analysis", required_inputs=["artifacts"], model_family="Qwen Vision",
            model_files=["qwen-vl"], resource_class="gpu_analysis", execution_time_class="medium",
        ),
        Capability(
            id="media.visual_qa.v1", version="1.0.0", media_type=MediaType.EVALUATION,
            mode="technical_and_visual_qa", required_inputs=["artifacts"], model_family="FFmpeg + Qwen Vision",
            model_files=["qwen-vl"], resource_class="gpu_analysis", execution_time_class="medium",
        ),
        Capability(
            id="ffmpeg.sequence_render.v1", version="1.0.0", media_type=MediaType.RENDER,
            mode="ordered_concat", required_inputs=["approved_video_artifacts"], model_family="FFmpeg",
            resource_class="cpu_render", execution_time_class="medium",
            limitations=["Concatenates the supplied order and performs no shot selection."],
        ),
    ]


class CapabilityRegistry:
    def __init__(self, workflows_dir: Path, *, simulation: bool = True,
                 qualification_report: Path | None = None):
        self.workflows_dir = workflows_dir
        self.simulation = simulation
        self.qualification_report = qualification_report
        self._items = {item.id: item for item in _capabilities()}
        self._refresh_states()

    def _refresh_states(self) -> None:
        qualified: set[str] = set()
        if not self.simulation and self.qualification_report and self.qualification_report.exists():
            try:
                report = json.loads(self.qualification_report.read_text())
                qualified = set(report.get("qualified_capabilities", []))
            except (OSError, ValueError, TypeError):
                qualified = set()
        for item in self._items.values():
            if self.simulation:
                item.qualification_state = QualificationState.QUALIFIED
            elif item.workflow_template and not (self.workflows_dir / item.workflow_template).exists():
                item.qualification_state = QualificationState.UNAVAILABLE
            elif item.media_type in {MediaType.RENDER}:
                item.qualification_state = QualificationState.QUALIFIED
            elif item.id in qualified:
                item.qualification_state = QualificationState.QUALIFIED
            else:
                item.qualification_state = QualificationState.UNQUALIFIED

    def list(self) -> list[Capability]:
        self._refresh_states()
        return [deepcopy(item) for item in self._items.values()]

    def get(self, capability_id: str) -> Capability:
        self._refresh_states()
        try:
            return deepcopy(self._items[capability_id])
        except KeyError as exc:
            raise RoutingError("capability_not_found", f"unknown capability: {capability_id}") from exc

    def require_executable(self, capability_id: str) -> Capability:
        item = self.get(capability_id)
        if item.qualification_state != QualificationState.QUALIFIED:
            raise RoutingError(
                "capability_not_qualified",
                f"{capability_id} is {item.qualification_state}",
                ["Run explicit target-machine qualification before real execution."],
            )
        return item

    def route_image(self, references: list[ArtifactRef], preferred: str | None = None) -> Capability:
        count = len(references)
        selected = preferred or (
            "flux_klein.t2i.v1" if count == 0 else
            "flux_klein.single_reference.v1" if count == 1 else
            "flux_klein.multi_reference.v1"
        )
        capability = self.require_executable(selected)
        conflicts = []
        if capability.media_type != MediaType.IMAGE:
            conflicts.append("requested capability is not an image capability")
        if not capability.min_references <= count <= capability.max_references:
            conflicts.append(
                f"reference count {count} is outside {capability.min_references}..{capability.max_references}"
            )
        invalid = [item.role for item in references if item.role not in capability.reference_roles]
        if invalid:
            conflicts.append(f"unsupported reference roles: {', '.join(invalid)}")
        if conflicts:
            raise RoutingError("unsupported_request", "image request conflicts with the selected capability", conflicts)
        return capability

    def route_video(self, references: list[ArtifactRef], preferred: str | None = None) -> Capability:
        roles = [item.role for item in references]
        is_flf = ReferenceRole.END_FRAME in roles
        selected = preferred or ("wan22.flf2v.v1" if is_flf else "wan22.i2v.v1")
        capability = self.require_executable(selected)
        expected = ({ReferenceRole.START_FRAME, ReferenceRole.END_FRAME}
                    if capability.mode == "first_last_frame_to_video" else {ReferenceRole.START_FRAME})
        conflicts = []
        if capability.media_type != MediaType.VIDEO:
            conflicts.append("requested capability is not a video capability")
        if set(roles) != expected or len(roles) != len(expected):
            conflicts.append(f"{capability.mode} requires exactly: {', '.join(sorted(expected))}")
        if conflicts:
            raise RoutingError("unsupported_request", "video request conflicts with the selected capability", conflicts)
        return capability
