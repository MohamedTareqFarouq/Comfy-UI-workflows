from __future__ import annotations

import json
from pathlib import Path

import pytest

from vertical_studio.schemas import ProjectState, SceneState
from vertical_studio.services.comfy import ComfyClient, GenerationError
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
