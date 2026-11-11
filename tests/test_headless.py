from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from vertical_studio.capabilities import CapabilityRegistry, RoutingError
from vertical_studio.cli import main as cli_main
from vertical_studio.config import Settings
from vertical_studio.headless import HeadlessMediaService, ProductionService
from vertical_studio.headless_schemas import (
    ProductionBudgets,
    ProductionTaskRequest,
    ArtifactRef,
    EvaluationRequest,
    ImageGenerationRequest,
    ProductionStartRequest,
    ReferenceRole,
    VideoGenerationRequest,
)
from vertical_studio.services.media import run_ffmpeg
from vertical_studio.services.gpu import GpuCoordinator
from vertical_studio.services.model_gateway import ModelUnavailable
from vertical_studio.mcp_server import build_mcp
from vertical_studio.schemas import QAReport


@pytest.fixture
def headless(tmp_path: Path) -> HeadlessMediaService:
    repo = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data, db_path=data / "studio.sqlite3", projects_dir=data / "projects",
        export_dir=data / "exports", static_dir=repo / "src" / "vertical_studio" / "static",
        workflows_dir=repo / "workflows", execution_mode="simulation",
    )
    settings.ensure_directories()
    return HeadlessMediaService(settings)


def wait_for(service: HeadlessMediaService, operation_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = service.store.operation(operation_id)
        if operation["state"] in {"succeeded", "failed", "cancelled"}:
            return operation
        service.run_once()
    raise AssertionError("operation did not finish")


def test_default_production_budget_covers_sixty_second_video() -> None:
    budgets = ProductionBudgets()
    assert budgets.max_video_operations == 16
    assert budgets.max_generated_video_seconds == 75


def test_production_start_records_control_and_goal_modes() -> None:
    request = ProductionStartRequest(
        goal="Recreate the exact source shot list",
        user_prompt="Keep motion, props, and lighting aligned to the reference.",
        control_mode="HITL",
        goal_mode="exact_replica",
        idempotency_key="production-mode-0001",
    )
    assert request.control_mode == "HITL"
    assert request.goal_mode == "exact_replica"


def test_autopilot_defaults_to_autonomous_approvals_and_keeps_hard_gates(headless) -> None:
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Autopilot test",
        user_prompt="Proceed only when evidence meets acceptance criteria.",
        control_mode="Autopilot",
        idempotency_key="production-autopilot-0001",
    ))

    assert session["approval_policy"]["preset"] == "autonomous"
    instructions = session["tasks"][0]["requirements"]["instructions"]
    assert any("does not bypass reference analysis" in item for item in instructions)
    assert any("evidence is missing" in item for item in instructions)

    situation = production.create_task(session["id"], ProductionTaskRequest(
        stage="plan",
        objective="Plan from source evidence",
        idempotency_key="autopilot-plan-task-0001",
    ))
    task = next(item for item in situation["active_tasks"] if item["role"] == "plan")
    assert any(
        "does not bypass reference analysis" in item
        for item in task["requirements"]["instructions"]
    )


def test_video_generation_requires_reference_analysis_and_approved_anchor(headless, tmp_path: Path) -> None:
    image = tmp_path / "anchor.png"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=270x480:d=0.1", "-frames:v", "1", str(image)])
    imported = headless.import_reference(str(image), "start_frame", "import-gate-0001")
    with headless.store.connect() as connection:
        connection.execute("UPDATE media_artifacts SET disposition='approved' WHERE id=?", (imported["id"],))

    session = ProductionService(headless).start(ProductionStartRequest(
        goal="Exact recreation gate",
        user_prompt="Recreate the reference faithfully.",
        reference_artifact_ids=[imported["id"]],
        reference_analysis_required=True,
        control_mode="HITL",
        goal_mode="exact_replica",
        idempotency_key="production-gate-0001",
    ))

    with pytest.raises(ValueError, match="reference analysis is required"):
        headless.generate_video(VideoGenerationRequest(
            prompt="subtle motion from the anchor",
            references=[ArtifactRef(artifact_id=imported["id"], role="start_frame")],
            width=270,
            height=480,
            frames=16,
            fps=8,
            production_session_id=session["id"],
            idempotency_key="video-gate-0001",
        ))


def test_video_generation_waits_for_created_storyboard_task_approval(headless, tmp_path: Path) -> None:
    image = tmp_path / "approved-anchor.png"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=270x480:d=0.1", "-frames:v", "1", str(image)])
    anchor = headless.import_reference(str(image), "start_frame", "import-plan-gate-0001")
    with headless.store.connect() as connection:
        connection.execute(
            "UPDATE media_artifacts SET disposition='approved', user_notes='human-approved anchor' WHERE id=?",
            (anchor["id"],),
        )
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Create a planned clip",
        user_prompt="Use an accepted storyboard before generating the clip.",
        idempotency_key="production-plan-gate-0001",
    ))
    plan = production.create_task(session["id"], ProductionTaskRequest(
        stage="plan",
        objective="Plan the video shot",
        idempotency_key="plan-gate-task-0001",
    ))["active_tasks"][0]
    production.update_task(session["id"], plan["id"], "completed", {"approved": False}, [])

    with pytest.raises(ValueError, match="storyboard approval is required"):
        headless.generate_video(VideoGenerationRequest(
            prompt="subtle movement",
            references=[ArtifactRef(artifact_id=anchor["id"], role="start_frame")],
            width=270,
            height=480,
            frames=16,
            fps=8,
            production_session_id=session["id"],
            idempotency_key="video-plan-gate-0001",
        ))


def test_video_generation_rejects_unapproved_canonical_reference(headless, tmp_path: Path) -> None:
    image = tmp_path / "canonical-anchor.png"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=270x480:d=0.1", "-frames:v", "1", str(image)])
    anchor = headless.import_reference(str(image), "identity", "import-unapproved-anchor-0001")
    session = ProductionService(headless).start(ProductionStartRequest(
        goal="Generate from an accepted identity anchor",
        user_prompt="Use only approved references.",
        idempotency_key="production-unapproved-anchor-0001",
    ))

    with pytest.raises(ValueError, match="requires approved anchors"):
        headless.generate_video(VideoGenerationRequest(
            prompt="subtle movement",
            references=[ArtifactRef(artifact_id=anchor["id"], role="start_frame")],
            width=270,
            height=480,
            frames=16,
            fps=8,
            production_session_id=session["id"],
            idempotency_key="video-unapproved-anchor-0001",
        ))


def test_real_comfy_gpu_ownership_allows_no_qwen_gateway() -> None:
    class Generator:
        def __init__(self) -> None:
            self.free_calls = 0

        def free_memory(self) -> None:
            self.free_calls += 1

    generator = Generator()
    coordinator = GpuCoordinator(generator, None, simulation=False)
    with coordinator.comfy(reset_models=False):
        pass
    assert generator.free_calls == 0

    with pytest.raises(RuntimeError, match="without a model gateway"):
        with coordinator.qwen():
            pass


def test_capability_router_is_deterministic_and_rejects_control_loss(headless) -> None:
    assert headless.registry.route_image([]).id == "flux_klein.t2i.v1"
    reference = ArtifactRef(artifact_id="a", role=ReferenceRole.IDENTITY)
    assert headless.registry.route_image([reference]).id == "flux_klein.single_reference.v1"
    assert headless.registry.route_image([reference, reference]).id == "flux_klein.multi_reference.v1"
    assert headless.registry.route_image(
        [reference, reference, reference], "qwen_image21.multi_reference.v1"
    ).id == "qwen_image21.multi_reference.v1"
    with pytest.raises(RoutingError, match="conflicts"):
        headless.registry.route_image([reference], "flux_klein.t2i.v1")
    with pytest.raises(RoutingError, match="conflicts"):
        headless.registry.route_image(
            [reference, reference, reference, reference],
            "qwen_image21.multi_reference.v1",
        )


def test_qwen_image21_rejects_multiple_candidates(headless, tmp_path: Path) -> None:
    image = tmp_path / "qwen-primary.png"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=270x480:d=0.1", "-frames:v", "1", str(image)])
    reference = headless.import_reference(str(image), "identity", "qwen-candidate-reference-1")
    request = ImageGenerationRequest(
        prompt="Edit the primary image",
        references=[ArtifactRef(artifact_id=reference["id"], role=ReferenceRole.IDENTITY)],
        candidate_count=2,
        preferred_capability="qwen_image21.multi_reference.v1",
        idempotency_key="qwen-image-candidates-1",
    )
    with pytest.raises(RoutingError, match="at most 1 candidate"):
        headless.generate_image(request)


def test_real_registry_uses_persisted_per_capability_qualification(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    report = data / "qualification" / "latest.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({"qualified_capabilities": ["flux_klein.t2i.v1"]}))
    settings = Settings(
        data_dir=data, db_path=data / "studio.sqlite3", projects_dir=data / "projects",
        export_dir=data / "exports", static_dir=repo / "src" / "vertical_studio" / "static",
        workflows_dir=repo / "workflows", execution_mode="real",
    )
    service = HeadlessMediaService(settings)
    assert service.registry.get("flux_klein.t2i.v1").qualification_state == "qualified"
    assert service.registry.get("wan22.i2v.v1").qualification_state == "unqualified"
    assert service.registry.get("qwen.reference_analysis.v1").qualification_state == "qualified"
    assert service.registry.get("media.technical_qa.v1").qualification_state == "qualified"
    assert service.registry.get("media.visual_qa.v1").qualification_state == "unqualified"


def test_unqualified_override_allows_execution_without_qualifying_capability(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry(
        repo / "workflows", simulation=False, allow_unqualified=True
    )
    capability = registry.require_executable("qwen_image21.multi_reference.v1")
    assert capability.qualification_state == "unqualified"
    listed = next(
        item for item in registry.list()
        if item.id == "qwen_image21.multi_reference.v1"
    )
    assert any("remains unqualified" in item for item in listed.limitations)

    unavailable = CapabilityRegistry(
        tmp_path / "missing-workflows", simulation=False, allow_unqualified=True
    )
    with pytest.raises(RoutingError, match="unavailable"):
        unavailable.require_executable("qwen_image21.multi_reference.v1")


def test_image_operation_is_idempotent_and_records_lineage(headless) -> None:
    request = ImageGenerationRequest(
        prompt="portrait test", width=270, height=480, seed=7,
        idempotency_key="image-test-0001",
    )
    first = headless.generate_image(request)
    duplicate = headless.generate_image(request)
    assert duplicate["id"] == first["id"]
    complete = wait_for(headless, first["id"])
    assert complete["state"] == "succeeded"
    artifact = headless.store.artifact(complete["result_artifact_ids"][0])
    assert artifact["capability_id"] == "flux_klein.t2i.v1"
    assert artifact["prompt"] == "portrait test"
    assert artifact["seed"] == 7


def test_video_modes_require_explicit_boundary_roles(headless, tmp_path: Path) -> None:
    image = tmp_path / "start.png"
    run_ffmpeg(["-f", "lavfi", "-i", "color=c=blue:s=270x480:d=0.1", "-frames:v", "1", str(image)])
    start = headless.import_reference(str(image), "start_frame", "import-start-0001")
    i2v = headless.generate_video(VideoGenerationRequest(
        prompt="subtle motion", references=[ArtifactRef(artifact_id=start["id"], role="start_frame")],
        width=270, height=480, frames=16, fps=8, idempotency_key="video-i2v-0001",
    ))
    assert i2v["capability_id"] == "wan22.i2v.v1"
    with pytest.raises(RoutingError) as error:
        headless.generate_video(VideoGenerationRequest(
            prompt="invalid forced FLF", references=[ArtifactRef(artifact_id=start["id"], role="start_frame")],
            width=270, height=480, frames=16, fps=8,
            preferred_capability="wan22.flf2v.v1", idempotency_key="video-invalid-0001",
        ))
    assert "requires exactly" in error.value.conflicts[0]


def test_standalone_reference_analysis_emits_shot_evidence(headless, tmp_path: Path) -> None:
    source = tmp_path / "reference.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=10:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
    ])
    imported = headless.import_reference(str(source), "style", "analysis-video-0001")
    operation = headless.analyze([imported["id"]], "analysis-evidence-0001")

    completed = wait_for(headless, operation["id"])

    assert completed["state"] == "succeeded"
    report_artifact = headless.store.artifact(completed["result_artifact_ids"][-1])
    report = json.loads(Path(report_artifact["path"]).read_text())
    source_report = report["source_analysis"][0]
    assert source_report["shot_count"] == 2
    assert source_report["cut_timestamps_seconds"]
    assert len(source_report["evidence_artifact_ids"]) == 6


def test_production_graph_is_model_neutral_and_dependency_driven(headless) -> None:
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Make a short portrait sequence", user_prompt="Keep one character consistent",
        idempotency_key="production-test-0001",
    ))
    assert session["approval_policy"]["preset"] == "approve_first_heavy"
    assert [task["role"] for task in session["tasks"]] == ["controller"]
    assert session["tasks"][0]["status"] == "ready"
    updated = production.update_task(session["id"], session["tasks"][0]["id"], "completed",
                                     {"decision": "plan"}, [])
    assert updated["tasks"][0]["status"] == "completed"
    assert "OpenCode" not in updated["tasks"][0]["handoff"]


def test_first_heavy_operation_requires_exact_approval(headless) -> None:
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Make one frame", user_prompt="A calm portrait", idempotency_key="approval-session-0001",
    ))
    request = ImageGenerationRequest(
        prompt="A calm portrait", width=270, height=480, seed=3,
        production_session_id=session["id"], idempotency_key="approval-image-0001",
    )
    with pytest.raises(ValueError, match="requires approval"):
        headless.generate_image(request)
    spec = {"capability_id": "flux_klein.t2i.v1", "request": request.model_dump(mode="json")}
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    approval_id = "approval-record-0001"
    with headless.store.connect() as connection:
        connection.execute(
            "INSERT INTO production_approvals VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, session["id"], hashlib.sha256(canonical.encode()).hexdigest(),
             json.dumps(spec), "approved", "test approval", "now", "now"),
        )
    queued = headless.generate_image(request)
    assert queued["state"] == "queued"
    assert production.inspect(session["id"])["usage"]["image_operations"] == 1
    with headless.store.connect() as connection:
        assert connection.execute("SELECT status FROM production_approvals WHERE id=?", (approval_id,)).fetchone()[0] == "consumed"


def test_recovery_requeues_only_interrupted_operations(headless) -> None:
    operation = headless.generate_image(ImageGenerationRequest(
        prompt="recover me", idempotency_key="recovery-test-0001",
    ))
    with headless.store.connect() as connection:
        connection.execute("UPDATE media_operations SET state='running' WHERE id=?", (operation["id"],))
    headless.recover()
    assert headless.store.operation(operation["id"])["state"] == "queued"


def test_mcp_exposes_atomic_and_production_control_contract(headless) -> None:
    server, service = build_mcp(headless.settings)
    try:
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert {
            "mcp_get_recent_calls",
            "mcp_get_status",
            "media_list_capabilities", "media_generate_image", "media_generate_video",
            "media_evaluate", "media_render_sequence", "media_cancel_operation",
            "production_start", "production_update_task", "production_request_approval",
            "production_resolve_approval",
            "production_cancel", "production_emergency_stop", "production_steer",
            "production_get_situation", "production_create_task", "production_invalidate_task",
        } <= names
    finally:
        service.stop()


def test_media_evaluate_uses_request_and_qwen_visual_critique(
    headless, tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "qa-target.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=8:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ])
    imported = headless.import_reference(str(video), "start_frame", "qa-target-video-0001")
    observed: dict = {}

    def inspect_video(path: Path, sample_dir: Path, **kwargs) -> QAReport:
        observed.update(path=path, sample_dir=sample_dir, kwargs=kwargs)
        return QAReport(
            technical_pass=True, advisory_pass=True, evaluator="test-qwen",
            goal_mode=kwargs["goal_mode"],
        )

    monkeypatch.setattr(headless.qa, "inspect_video", inspect_video)
    queued = headless.evaluate(EvaluationRequest(
        artifact_ids=[imported["id"]],
        goal_mode="adaptation",
        use_visual_critique=True,
        requirements={"subject": "blue sphere"},
        idempotency_key="qa-visual-request-0001",
    ))

    assert headless.run_once()
    completed = headless.store.operation(queued["id"])
    assert completed["state"] == "succeeded"
    assert observed["kwargs"]["use_visual_review"] is True
    assert observed["kwargs"]["context"]["requirements"] == {"subject": "blue sphere"}


def test_media_evaluate_defaults_to_technical_qa_without_qwen(headless, tmp_path: Path) -> None:
    image = tmp_path / "qa-image.png"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=0.1",
        "-frames:v", "1", str(image),
    ])
    imported = headless.import_reference(str(image), "identity", "qa-image-technical-0001")
    queued = headless.evaluate(EvaluationRequest(
        artifact_ids=[imported["id"]],
        idempotency_key="qa-technical-default-0001",
    ))

    assert queued["capability_id"] == "media.technical_qa.v1"
    assert headless.run_once()
    completed = headless.store.operation(queued["id"])
    assert completed["state"] == "succeeded"
    report_artifact = headless.store.artifact(completed["result_artifact_ids"][0])
    report = json.loads(Path(report_artifact["path"]).read_text())["reports"][0]
    assert report["technical_pass"] is True
    assert report["evaluator"] == "technical_only"


def test_media_evaluate_returns_manual_fallback_when_qwen_is_unavailable(
    headless, tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "qa-fallback.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=green:s=160x90:r=8:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ])
    imported = headless.import_reference(str(video), "start_frame", "qa-fallback-video-0001")

    def inspect_video(path: Path, sample_dir: Path, **kwargs) -> QAReport:
        if kwargs["use_visual_review"]:
            raise ModelUnavailable("Qwen is offline")
        return QAReport(
            technical_pass=True, advisory_pass=True, evaluator="technical_only",
            goal_mode=kwargs["goal_mode"],
        )

    monkeypatch.setattr(headless.qa, "inspect_video", inspect_video)
    queued = headless.evaluate(EvaluationRequest(
        artifact_ids=[imported["id"]],
        use_visual_critique=True,
        idempotency_key="qa-visual-fallback-0001",
    ))

    assert headless.run_once()
    completed = headless.store.operation(queued["id"])
    assert completed["state"] == "succeeded"
    report_artifact = headless.store.artifact(completed["result_artifact_ids"][0])
    report = json.loads(Path(report_artifact["path"]).read_text())["reports"][0]
    assert report["evaluator"] == "manual_review_required"
    assert report["advisory_pass"] is False
    assert "Qwen is offline" in report["reasons"][-1]


def test_mcp_marks_simulated_generation_in_status_operation_and_artifact(headless) -> None:
    server, service = build_mcp(headless.settings)
    try:
        status_result = asyncio.run(server.call_tool("mcp_get_status", {}))
        status = status_result.model_dump(mode="json", by_alias=True)["structuredContent"]
        assert status["execution_mode"] == "simulation"
        assert status["backend"] == "mock_simulator"
        assert status["qwen"]["checked"] is False
        assert status["allow_unqualified_capabilities"] is False
        assert status["keyframe_image_backend"] == "klein"
        assert status["unqualified_override_capabilities"] == []
        assert status["comfyui"]["checked"] is False
        assert "not ComfyUI results" in status["simulation_warning"]

        queued_result = asyncio.run(server.call_tool("media_generate_image", {
            "request": {
                "prompt": "test image", "idempotency_key": "simulation-provenance-001",
                "width": 256, "height": 256,
            },
        }))
        queued = queued_result.model_dump(mode="json", by_alias=True)["structuredContent"]
        assert queued["state"] == "queued"
        assert queued["execution_mode"] == "simulation"
        assert queued["backend"] == "mock_simulator"

        assert service.run_once()
        operation = service.store.operation(queued["id"])
        assert operation["state"] == "succeeded"
        artifact = service.store.artifact(operation["result_artifact_ids"][0])
        assert artifact["metadata"]["backend"] == "mock_simulator"
        assert artifact["metadata"]["execution_mode"] == "simulation"
        assert artifact["metadata"]["simulated"] is True
        assert "prompt_id" not in artifact["metadata"]
    finally:
        service.stop()


def test_mcp_status_exposes_enabled_unqualified_override(headless, monkeypatch) -> None:
    settings = replace(
        headless.settings,
        execution_mode="real",
        allow_unqualified_capabilities=True,
        keyframe_image_backend="qwen_image21",
    )
    server, service = build_mcp(settings)
    monkeypatch.setattr(service.generator, "health", lambda: {"available": True})
    try:
        status_result = asyncio.run(server.call_tool("mcp_get_status", {}))
        status = status_result.model_dump(mode="json", by_alias=True)["structuredContent"]
        capability_states = {item["id"]: item["qualification_state"] for item in status["capabilities"]}
        assert status["allow_unqualified_capabilities"] is True
        assert status["keyframe_image_backend"] == "qwen_image21"
        assert capability_states["qwen_image21.multi_reference.v1"] == "unqualified"
        assert "qwen_image21.multi_reference.v1" in status["unqualified_override_capabilities"]
        assert "media.visual_qa.v1" in status["unqualified_override_capabilities"]
        assert "qwen_image21.multi_reference.v1" not in status["real_generation_ready_capabilities"]
    finally:
        service.stop()


def test_approval_decision_can_be_recovered_by_id(headless) -> None:
    server, service = build_mcp(headless.settings)
    try:
        session = ProductionService(service).start(ProductionStartRequest(
            goal="Approve one image", user_prompt="Create the requested image",
            idempotency_key="approval-recovery-session-001",
        ))
        image_request = ImageGenerationRequest(
            prompt="Approved test image", width=256, height=256, seed=7,
            production_session_id=session["id"], idempotency_key="approval-recovery-image-001",
        )
        operation_spec = {
            "capability_id": "flux_klein.t2i.v1",
            "request": image_request.model_dump(mode="json"),
        }
        requested = asyncio.run(server.call_tool("production_request_approval", {
            "production_session_id": session["id"],
            "operation_spec": operation_spec,
            "reason": "Render the approved sequence",
            "dependencies": [],
            "affected_shots": ["shot-1"],
            "attempt": 1,
            "remaining_budget": {},
        })).model_dump(mode="json", by_alias=True)["structuredContent"]
        assert requested["created_approval_status"] == "pending"
        approval_id = requested["approvals"][-1]["id"]

        with pytest.raises(Exception, match="does not belong to production session"):
            asyncio.run(server.call_tool("production_approve", {
                "production_session_id": "wrong-session-id",
                "approval_id": approval_id,
                "reason": "user approved",
            }))

        resolved = asyncio.run(server.call_tool("production_resolve_approval", {
            "approval_id": approval_id,
            "decision": "approved",
            "reason": "user approved",
        })).model_dump(mode="json", by_alias=True)["structuredContent"]
        assert next(item for item in resolved["approvals"] if item["id"] == approval_id)["status"] == "approved"

        repeated = asyncio.run(server.call_tool("production_resolve_approval", {
            "approval_id": approval_id,
            "decision": "approved",
            "reason": "repeat delivery",
        })).model_dump(mode="json", by_alias=True)["structuredContent"]
        assert next(item for item in repeated["approvals"] if item["id"] == approval_id)["status"] == "approved"

        queued = service.generate_image(image_request)
        assert queued["state"] == "queued"
        with service.store.connect() as connection:
            status = connection.execute(
                "SELECT status FROM production_approvals WHERE id=?", (approval_id,)
            ).fetchone()[0]
        assert status == "consumed"
    finally:
        service.stop()


def test_cli_manually_approves_existing_request(headless, monkeypatch, capsys) -> None:
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Manual approval test", user_prompt="Approve the operation",
        idempotency_key="manual-approval-session-001",
    ))
    operation_spec = {"capability_id": "flux_klein.t2i.v1", "request": {"prompt": "test"}}
    canonical = json.dumps(operation_spec, sort_keys=True, separators=(",", ":"))
    approval_id = "manual-approval-record-001"
    with headless.store.connect() as connection:
        connection.execute(
            "INSERT INTO production_approvals VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, session["id"], hashlib.sha256(canonical.encode()).hexdigest(),
             json.dumps(operation_spec), "pending", "", "now", None),
        )

    monkeypatch.setenv("VVS_DATA_DIR", str(headless.settings.data_dir))
    monkeypatch.setenv("VVS_EXECUTION_MODE", "simulation")
    monkeypatch.setattr("sys.argv", [
        "vertical-studio", "approval", approval_id, "approved", "--reason", "manual fallback",
    ])
    assert cli_main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "production_session_id": session["id"],
        "approval_id": approval_id,
        "status": "approved",
        "reason": "manual fallback",
    }


def test_mcp_input_schemas_are_described_and_calls_are_traced(headless) -> None:
    server, service = build_mcp(headless.settings)
    try:
        tools = asyncio.run(server.list_tools())
        tool_schemas = {tool.name: tool.input_schema for tool in tools}
        for tool in tools:
            schema = tool.input_schema
            for name, field in schema.get("properties", {}).items():
                assert field.get("description"), f"{tool.name}.{name} has no description"
            for definition_name, definition in schema.get("$defs", {}).items():
                for name, field in definition.get("properties", {}).items():
                    assert field.get("description"), f"{tool.name}.{definition_name}.{name} has no description"
        assert tool_schemas["media_import_reference"]["properties"]["role"]["enum"] == [
            "identity", "environment", "prop", "style", "composition", "pose",
            "start_frame", "end_frame", "motion_continuity",
        ]
        trace_limit = tool_schemas["mcp_get_recent_calls"]["properties"]["limit"]
        assert (trace_limit["minimum"], trace_limit["maximum"]) == (1, 100)

        asyncio.run(server.call_tool("media_list_capabilities", {}))
        with pytest.raises(Exception, match=r"trace_id=[0-9a-f]{32}") as tool_error:
            asyncio.run(server.call_tool("media_inspect_capability", {"capability_id": "missing-capability"}))
        assert "unknown capability" in str(tool_error.value)
        with pytest.raises(Exception, match=r"trace_id=[0-9a-f]{32}") as validation_error:
            asyncio.run(server.call_tool("media_inspect_capability", {}))
        assert "capability_id" in str(validation_error.value)

        trace_path = headless.settings.data_dir / "logs" / "mcp-calls.jsonl"
        records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        calls = {}
        for record in records:
            calls.setdefault(record["trace_id"], []).append(record)
        assert any(
            [record["phase"] for record in entries] == ["started", "completed"]
            and entries[0]["tool"] == "media_list_capabilities"
            for entries in calls.values()
        )
        assert any(
            [record["phase"] for record in entries] == ["started", "failed"]
            and entries[0]["tool"] == "media_inspect_capability"
            and entries[-1]["error"]
            for entries in calls.values()
        )
        assert sum(record["phase"] == "failed" for record in records) == 2
    finally:
        service.stop()


def test_agent_selects_stages_and_invalidation_is_transitive(headless) -> None:
    production = ProductionService(headless)
    session = production.start(ProductionStartRequest(
        goal="Make one image", user_prompt="A calm portrait",
        idempotency_key="dynamic-session-0001",
    ))
    assert [task["role"] for task in session["tasks"]] == ["controller"]
    controller_id = session["tasks"][0]["id"]
    production.update_task(session["id"], controller_id, "completed", {"decision": "skip planning"}, [])
    image = production.create_task(session["id"], ProductionTaskRequest(
        stage="image", objective="Generate the requested portrait",
        dependency_task_ids=[controller_id], acceptance_criteria=["portrait exists"],
        idempotency_key="dynamic-image-0001",
    ))
    image_task = next(task for task in image["active_tasks"] if task["role"] == "image")
    assert image_task["status"] == "ready"
    qa = production.create_task(session["id"], ProductionTaskRequest(
        stage="qa", objective="Review the portrait", dependency_task_ids=[image_task["id"]],
        idempotency_key="dynamic-qa-0001",
    ))
    qa_task = next(task for task in qa["active_tasks"] if task["role"] == "qa")
    assert qa_task["status"] == "pending"
    invalidated = production.invalidate_task(session["id"], image_task["id"], "change requested")
    statuses = {task["role"]: task["status"] for task in production.inspect(session["id"])["tasks"]}
    assert statuses["image"] == "invalidated"
    assert statuses["qa"] == "invalidated"
    assert "create_task" in invalidated["allowed_actions"]
