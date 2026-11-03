from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from vertical_studio.capabilities import RoutingError
from vertical_studio.config import Settings
from vertical_studio.headless import HeadlessMediaService, ProductionService
from vertical_studio.headless_schemas import (
    ProductionTaskRequest,
    ArtifactRef,
    ImageGenerationRequest,
    ProductionStartRequest,
    ReferenceRole,
    VideoGenerationRequest,
)
from vertical_studio.services.media import run_ffmpeg
from vertical_studio.mcp_server import build_mcp


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


def test_capability_router_is_deterministic_and_rejects_control_loss(headless) -> None:
    assert headless.registry.route_image([]).id == "flux_klein.t2i.v1"
    reference = ArtifactRef(artifact_id="a", role=ReferenceRole.IDENTITY)
    assert headless.registry.route_image([reference]).id == "flux_klein.single_reference.v1"
    assert headless.registry.route_image([reference, reference]).id == "flux_klein.multi_reference.v1"
    with pytest.raises(RoutingError, match="conflicts"):
        headless.registry.route_image([reference], "flux_klein.t2i.v1")


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
            "media_list_capabilities", "media_generate_image", "media_generate_video",
            "media_evaluate", "media_render_sequence", "media_cancel_operation",
            "production_start", "production_update_task", "production_request_approval",
            "production_cancel", "production_emergency_stop", "production_steer",
            "production_get_situation", "production_create_task", "production_invalidate_task",
        } <= names
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
