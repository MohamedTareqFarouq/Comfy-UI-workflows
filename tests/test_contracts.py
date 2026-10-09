from __future__ import annotations

import json
from pathlib import Path

import pytest

from vertical_studio.schemas import ProjectState, SceneState, Storyboard
from vertical_studio.services.comfy import ComfyClient, GenerationError
from vertical_studio.services.model_gateway import ModelGateway
from vertical_studio.services.mock_comfy import MockComfyClient
from vertical_studio.state import InvalidTransition, validate_project_transition, validate_scene_transition


def test_invalid_state_transitions_are_rejected() -> None:
    with pytest.raises(InvalidTransition):
        validate_project_transition(ProjectState.DRAFT, ProjectState.COMPLETE)
    with pytest.raises(InvalidTransition):
        validate_scene_transition(SceneState.PLANNED, SceneState.VIDEO_READY)


def test_real_adapter_rejects_ui_workflow(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(json.dumps({"nodes": []}))
    client = ComfyClient("http://127.0.0.1:1", tmp_path)
    with pytest.raises(GenerationError) as error:
        client.generate_keyframes(tmp_path / "out", {"template": "bad"})
    assert error.value.code == "workflow_not_api_format"


@pytest.mark.parametrize("failure,transient", [("timeout", True), ("oom", True), ("node_failure", False)])
def test_mock_failure_contract(tmp_path: Path, failure: str, transient: bool) -> None:
    client = MockComfyClient()
    with pytest.raises(GenerationError) as error:
        client.generate_video(tmp_path, {"simulate_failure": failure})
    assert error.value.code == failure
    assert error.value.transient is transient


def test_qualification_is_diagnostics_only_by_default(components) -> None:
    from vertical_studio.qualification import QualificationError, QualificationService

    service = QualificationService(components.settings)
    report = service.run("diagnostics")
    assert report["qualified"] is False
    assert Path(report["report_path"]).exists()
    with pytest.raises(QualificationError, match="confirm"):
        service.run("smoke")


def test_structured_retry_shows_qwen_the_exact_schema(monkeypatch, tmp_path: Path) -> None:
    invalid = {
        "storyboard": [
            {"scene": 1, "duration": 10, "description": "Wrong shape", "beat": "Opening"}
        ]
    }
    valid = {
        "summary": "A complete short story",
        "character_bible": {"worker": "same uniform and brass lantern"},
        "scenes": [
            {
                "title": "Arrival",
                "narrative_purpose": "Establish the worker and approaching train",
                "duration_seconds": 3.0,
                "start_composition": "Worker alone on a foggy platform",
                "end_composition": "A headlight appears in the distance",
                "motion": "Fog drifts while the distant light approaches",
            },
            {
                "title": "Signal",
                "narrative_purpose": "The worker signals the train",
                "duration_seconds": 3.0,
                "start_composition": "Worker lowers the old lantern",
                "end_composition": "Worker raises it above his head",
                "motion": "The arm and lantern rise steadily",
            },
            {
                "title": "Stop",
                "narrative_purpose": "Resolve the mysterious arrival",
                "duration_seconds": 4.0,
                "start_composition": "Train enters beside the worker",
                "end_composition": "Train stops under warm station lights",
                "motion": "The train brakes and station lights turn on",
            },
        ],
    }
    responses = [invalid, valid]
    requests: list[dict] = []

    class Response:
        def __init__(self, body: dict):
            self.body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": json.dumps(self.body)}}]}

    def fake_post(*_args, **kwargs):
        requests.append(json.loads(json.dumps(kwargs["json"])))
        return Response(responses.pop(0))

    gateway = ModelGateway(
        "http://127.0.0.1:8080", "qwen", log_path=tmp_path / "qwen.log"
    )
    monkeypatch.setattr(gateway, "ensure_available", lambda: None)
    monkeypatch.setattr("vertical_studio.services.model_gateway.httpx.post", fake_post)

    storyboard = gateway.generate_structured(
        Storyboard, system="Create a storyboard.", context="A railway worker."
    )

    assert storyboard.summary == valid["summary"]
    assert len(requests) == 2
    assert '"summary"' in requests[0]["messages"][0]["content"]
    assert '"scenes"' in requests[0]["messages"][0]["content"]
    correction = requests[1]["messages"][-1]["content"]
    assert "do not add a wrapper object" in correction
    assert '"start_composition"' in correction
