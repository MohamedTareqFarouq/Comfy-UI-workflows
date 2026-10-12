from __future__ import annotations

import json
from pathlib import Path

import pytest

from vertical_studio.orchestrator import Orchestrator
from vertical_studio.schemas import ProjectState, SceneState, Storyboard, VideoRetryPromptProposal
from vertical_studio.services.comfy import ComfyClient, GenerationError, GenerationResult
from vertical_studio.services.model_gateway import ModelGateway
from vertical_studio.services.mock_comfy import MockComfyClient
from vertical_studio.state import InvalidTransition, validate_project_transition, validate_scene_transition


def test_invalid_state_transitions_are_rejected() -> None:
    with pytest.raises(InvalidTransition):
        validate_project_transition(ProjectState.DRAFT, ProjectState.COMPLETE)
    with pytest.raises(InvalidTransition):
        validate_scene_transition(SceneState.PLANNED, SceneState.VIDEO_READY)


def test_multireference_workflows_and_maps_are_valid_json() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_dir = root / "workflows"
    mappings = json.loads((workflow_dir / "workflow_maps.json").read_text())
    for count in range(1, 5):
        name = f"flux_klein_multiref_{count}_api"
        graph = json.loads((workflow_dir / f"{name}.json").read_text())
        reference_keys = [key for key in mappings[name] if key.startswith("reference_")]
        assert len(reference_keys) == count
        for key in reference_keys:
            pointers = mappings[name][key]
            if isinstance(pointers, dict):
                pointers = [pointers]
            assert all(str(pointer["node_id"]) in graph for pointer in pointers)


def test_storyboard_enforces_endpoint_environment_continuity() -> None:
    assets = [
        {"key": "leo", "role": "character", "label": "Leo", "prompt": "Leo alone"},
        {"key": "kitchen", "role": "environment", "label": "Kitchen", "prompt": "Empty kitchen"},
        {"key": "dining_room", "role": "environment", "label": "Dining room", "prompt": "Empty dining room"},
    ]

    def scene(title: str, opening: str, closing: str, first: bool = False) -> dict:
        return {
            "title": title,
            "narrative_purpose": title,
            "duration_seconds": 3,
            "start_composition": f"Leo in {opening}",
            "end_composition": f"Leo in {closing}",
            "motion": "One clear action",
            "continuity_group": "restaurant",
            "starts_new_continuity": first,
            "transition_from_previous": "new_sequence" if first else "continuous",
            "reference_keys": ["leo", opening, closing],
            "characters_present": ["leo"],
            "environment_key": opening,
            "opening_environment_key": opening,
            "closing_environment_key": closing,
        }

    valid = Storyboard(
        summary="Kitchen to dining room",
        requires_character_sheet=False,
        character_sheet_prompt="",
        character_bible={},
        canonical_assets=assets,
        scenes=[
            scene("Prepare", "kitchen", "kitchen", True),
            scene("Cross doorway", "kitchen", "dining_room"),
            scene("Serve", "dining_room", "dining_room"),
        ],
    )
    assert valid.scenes[1].closing_environment_key == "dining_room"

    broken = [item.model_dump() for item in valid.scenes]
    broken[2]["opening_environment_key"] = "kitchen"
    broken[2]["environment_key"] = "kitchen"
    with pytest.raises(ValueError, match="opening environment"):
        Storyboard(
            summary=valid.summary,
            requires_character_sheet=False,
            character_sheet_prompt="",
            character_bible={},
            canonical_assets=assets,
            scenes=broken,
        )


def test_keyframes_use_different_environment_references_per_endpoint(
    monkeypatch, tmp_path: Path
) -> None:
    client = ComfyClient("http://127.0.0.1:1", tmp_path)
    calls: list[list[str]] = []

    def fake_multiref(
        output: Path, _payload: dict, *, prompt: str, seed: int,
        references: list[dict], output_prefix: str,
    ) -> GenerationResult:
        calls.append([reference["key"] for reference in references])
        output.mkdir(parents=True, exist_ok=True)
        path = output / f"{output_prefix}.png"
        path.write_bytes(f"{prompt}:{seed}".encode())
        return GenerationResult(files=[path], metadata={})

    monkeypatch.setattr(client, "_generate_multiref_image", fake_multiref)
    client.generate_keyframes(
        tmp_path / "output",
        {
            "scene_id": "doorway",
            "spec": {
                "narrative_purpose": "Leo crosses from kitchen to dining room",
                "start_composition": "Leo in the kitchen doorway",
                "end_composition": "Leo enters the dining room",
                "motion": "Leo takes one step through the doorway",
                "negative_constraints": [],
                "safe_area": "center subject",
                "characters_present": ["leo"],
                "opening_environment_key": "kitchen",
                "closing_environment_key": "dining_room",
            },
            "generation_mode": "anchor",
            "candidates": 1,
            "seed": 42,
            "width": 384,
            "height": 672,
            "visual_style": "stylized 3D animation",
            "opening_canonical_references": [
                {"key": "leo", "role": "character", "label": "Leo", "path": "leo.png"},
                {"key": "kitchen", "role": "environment", "label": "Kitchen", "path": "kitchen.png"},
            ],
            "closing_canonical_references": [
                {"key": "leo", "role": "character", "label": "Leo", "path": "leo.png"},
                {"key": "dining_room", "role": "environment", "label": "Dining room", "path": "dining.png"},
            ],
        },
    )
    assert calls[0] == ["leo", "kitchen"]
    assert calls[1] == ["opening_frame", "leo", "dining_room"]


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
        "requires_character_sheet": True,
        "character_sheet_prompt": "Railway worker identity turnaround",
        "character_bible": {"worker": "same uniform and brass lantern"},
        "scenes": [
            {
                "title": "Arrival",
                "narrative_purpose": "Establish the worker and approaching train",
                "duration_seconds": 3.0,
                "start_composition": "Worker alone on a foggy platform",
                "end_composition": "A headlight appears in the distance",
                "motion": "Fog drifts while the distant light approaches",
                "continuity_group": "station-night",
                "starts_new_continuity": True,
                "transition_from_previous": "new_sequence",
            },
            {
                "title": "Signal",
                "narrative_purpose": "The worker signals the train",
                "duration_seconds": 3.0,
                "start_composition": "Worker lowers the old lantern",
                "end_composition": "Worker raises it above his head",
                "motion": "The arm and lantern rise steadily",
                "continuity_group": "station-night",
            },
            {
                "title": "Stop",
                "narrative_purpose": "Resolve the mysterious arrival",
                "duration_seconds": 4.0,
                "start_composition": "Train enters beside the worker",
                "end_composition": "Train stops under warm station lights",
                "motion": "The train brakes and station lights turn on",
                "continuity_group": "station-night",
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
    assert "Regenerate the complete object from scratch" in correction
    assert "JSON Schema is already present in the system message" in correction
    assert "Validation error" in correction


def test_structured_generation_recovers_from_two_malformed_json_responses(
    monkeypatch, tmp_path: Path
) -> None:
    valid = {
        "summary": "Compact recovery storyboard",
        "requires_character_sheet": False,
        "character_sheet_prompt": "",
        "character_bible": {},
        "scenes": [
            {
                "title": f"Scene {index}",
                "narrative_purpose": "Advance the story",
                "duration_seconds": 3,
                "start_composition": "Opening frame",
                "end_composition": "Closing frame",
                "motion": "One action",
                "continuity_group": "main",
                "starts_new_continuity": index == 1,
                "transition_from_previous": "new_sequence" if index == 1 else "continuous",
            }
            for index in range(1, 4)
        ],
    }
    responses = ['{"summary": "broken" "scenes": []}', '{"summary":', json.dumps(valid)]
    requests: list[dict] = []

    class Response:
        def __init__(self, content: str):
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": self.content}}]}

    def fake_post(*_args, **kwargs):
        requests.append(json.loads(json.dumps(kwargs["json"])))
        return Response(responses.pop(0))

    gateway = ModelGateway(
        "http://127.0.0.1:8080", "qwen", log_path=tmp_path / "qwen.log"
    )
    monkeypatch.setattr(gateway, "ensure_available", lambda: None)
    monkeypatch.setattr("vertical_studio.services.model_gateway.httpx.post", fake_post)

    storyboard = gateway.generate_structured(
        Storyboard, system="Create a storyboard.", context="A compact story."
    )

    assert storyboard.summary == valid["summary"]
    assert len(requests) == 3
    assert requests[2]["temperature"] == 0
    final_correction = requests[2]["messages"][-1]["content"]
    assert "do not repeat list items or phrases" in final_correction
    assert "finish the complete JSON object" in final_correction
    assert all(message["role"] != "assistant" for message in requests[2]["messages"])


def test_video_retry_schema_excludes_irrelevant_keyframe_fields() -> None:
    properties = VideoRetryPromptProposal.model_json_schema()["properties"]
    assert set(properties) == {
        "stage", "video_prompt", "negative_prompt", "rationale"
    }
    assert properties["negative_prompt"]["maxLength"] == 1800


def test_retry_negative_prompt_is_deduplicated_bounded_and_style_safe() -> None:
    repeated = ", ".join(
        ["3D animation", "motion blur", "MOTION BLUR"]
        + [f"artifact {index}" for index in range(80)]
    )
    cleaned = Orchestrator._sanitize_negative_prompt(
        repeated,
        "Premium stylized 3D animation with warm lighting",
    )
    terms = cleaned.split(", ")

    assert "3D animation" not in terms
    assert terms.count("motion blur") == 1
    assert len(terms) == 60
    assert terms[-1] == "artifact 58"


def test_flux_edit_receives_previous_frame_character_sheet_and_style(monkeypatch, tmp_path: Path) -> None:
    client = ComfyClient("http://127.0.0.1:1", tmp_path)
    source = tmp_path / "previous.png"
    sheet = tmp_path / "sheet.png"
    for path in (source, sheet):
        path.write_bytes(b"reference")
    captured: dict = {}

    def fake_mapping(template: str, required: set[str]) -> dict:
        captured["template"] = template
        captured["required"] = required
        return {
            name: {"node_id": name, "input": name}
            for name in required
        }

    def fake_run(template: str, payload: dict, output: Path) -> GenerationResult:
        captured["patches"] = payload["patches"]
        output.mkdir(parents=True, exist_ok=True)
        result = output / "generated.png"
        result.write_bytes(b"generated")
        return GenerationResult(files=[result], metadata={"prompt_id": "test"})

    monkeypatch.setattr(client, "_load_mapping", fake_mapping)
    monkeypatch.setattr(client, "_upload_image", lambda path: Path(path).name)
    monkeypatch.setattr(client, "_run", fake_run)
    result = client.generate_keyframes(
        tmp_path / "output",
        {
            "scene_id": "scene-2",
            "spec": {
                "narrative_purpose": "Continue walking",
                "start_composition": "Worker beside the lamp",
                "end_composition": "Worker reaches the train",
                "motion": "Walk forward",
                "negative_constraints": [],
                "safe_area": "center subject",
            },
            "generation_mode": "continuation",
            "candidates": 1,
            "seed": 42,
            "width": 384,
            "height": 672,
            "edit_template": "flux_edit_api",
            "visual_style": "hand-drawn cel cartoon",
            "source_frames": [{"candidate": 1, "path": str(source)}],
            "character_sheet_path": str(sheet),
        },
    )
    assert result.files[0].name == "candidate-1-end.png"
    assert {"source_image", "character_sheet"} <= captured["required"]
    assert "first_frame" not in captured["required"]
    values = {patch["input"]: patch["value"] for patch in captured["patches"]}
    assert values["source_image"] == source.name
    assert values["character_sheet"] == sheet.name
    assert "MANDATORY VISUAL STYLE: hand-drawn cel cartoon" in values["prompt"]
    assert "photorealistic" in values["prompt"]


def test_comfy_upload_names_do_not_collide_for_same_basename(tmp_path: Path) -> None:
    first_dir = tmp_path / "previous"
    second_dir = tmp_path / "current"
    first_dir.mkdir()
    second_dir.mkdir()
    start = first_dir / "candidate-1-end.png"
    end = second_dir / "candidate-1-end.png"
    start.write_bytes(b"start-frame-content")
    end.write_bytes(b"end-frame-content")

    start_name = ComfyClient._upload_filename(start)
    end_name = ComfyClient._upload_filename(end)

    assert start_name != end_name
    assert start_name.startswith("candidate-1-end-")
    assert end_name.startswith("candidate-1-end-")


def test_wan_retry_uses_accepted_full_prompts_without_wrapping(
    monkeypatch, tmp_path: Path
) -> None:
    client = ComfyClient("http://127.0.0.1:1", tmp_path)
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    captured: dict = {}
    monkeypatch.setattr(client, "_load_template", lambda _template: {})
    monkeypatch.setattr(
        client, "_load_mapping",
        lambda _template, required: {
            name: {"node_id": name, "input": name} for name in required
        },
    )
    monkeypatch.setattr(client, "_upload_image", lambda path: Path(path).name)

    def fake_run(_template: str, payload: dict, output: Path) -> GenerationResult:
        captured["values"] = {
            patch["input"]: patch["value"] for patch in payload["patches"]
        }
        output.mkdir(parents=True, exist_ok=True)
        result = output / "clip.mp4"
        result.write_bytes(b"video")
        return GenerationResult(files=[result], metadata={})

    monkeypatch.setattr(client, "_run", fake_run)
    client.generate_video(
        tmp_path / "video",
        {
            "scene_id": "scene-1",
            "spec": {
                "narrative_purpose": "Serve dinner",
                "motion": "Place the bowl",
                "negative_constraints": [],
            },
            "visual_style": "stylized 3D animation",
            "prompt_override": "ACCEPTED FULL POSITIVE PROMPT",
            "negative_prompt_override": "ACCEPTED NEGATIVE PROMPT",
            "start_image_path": str(start),
            "end_image_path": str(end),
            "seed": 7,
            "width": 384,
            "height": 672,
            "frames": 49,
            "attempt": 2,
        },
    )
    assert captured["values"]["prompt"] == "ACCEPTED FULL POSITIVE PROMPT"
    assert captured["values"]["negative_prompt"] == "ACCEPTED NEGATIVE PROMPT"


def test_real_wan_workflow_binds_start_and_end_nodes_in_order(
    monkeypatch, tmp_path: Path
) -> None:
    workflow_dir = Path(__file__).parents[1] / "workflows"
    client = ComfyClient("http://127.0.0.1:1", workflow_dir)
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    captured: dict = {}

    monkeypatch.setattr(
        client, "_upload_image", lambda path: f"uploaded-{Path(path).name}"
    )

    def fake_run(_template: str, payload: dict, output: Path) -> GenerationResult:
        captured["patches"] = payload["patches"]
        output.mkdir(parents=True, exist_ok=True)
        result = output / "clip.mp4"
        result.write_bytes(b"video")
        return GenerationResult(files=[result], metadata={})

    monkeypatch.setattr(client, "_run", fake_run)
    result = client.generate_video(
        tmp_path / "video",
        {
            "scene_id": "scene-1",
            "spec": {
                "narrative_purpose": "Cross the room",
                "motion": "Walk from the opening pose to the closing pose",
                "negative_constraints": [],
            },
            "visual_style": "stylized animation",
            "start_artifact_id": "start-artifact",
            "end_artifact_id": "end-artifact",
            "keyframe_binding": {
                "start": {"artifact_id": "start-artifact", "sha256": "start-hash"},
                "end": {"artifact_id": "end-artifact", "sha256": "end-hash"},
            },
            "start_image_path": str(start),
            "end_image_path": str(end),
            "seed": 8,
            "width": 384,
            "height": 672,
            "frames": 49,
        },
    )
    patches = {
        (str(patch["node_id"]), patch["input"]): patch["value"]
        for patch in captured["patches"]
    }
    assert patches[("68", "image")] == "uploaded-start.png"
    assert patches[("62", "image")] == "uploaded-end.png"
    audit = result.metadata["prompt_audit"]
    assert audit["start_artifact_id"] == "start-artifact"
    assert audit["end_artifact_id"] == "end-artifact"
    assert audit["comfy_inputs"]["start"]["node_id"] == "68"
    assert audit["comfy_inputs"]["end"]["node_id"] == "62"


def test_wan_prompt_enforces_project_style(monkeypatch, tmp_path: Path) -> None:
    client = ComfyClient("http://127.0.0.1:1", tmp_path)
    start = tmp_path / "start.png"
    end = tmp_path / "end.png"
    start.write_bytes(b"start")
    end.write_bytes(b"end")
    captured: dict = {}

    monkeypatch.setattr(client, "_load_template", lambda _template: {})
    monkeypatch.setattr(
        client,
        "_load_mapping",
        lambda _template, required: {
            name: {"node_id": name, "input": name} for name in required
        },
    )
    monkeypatch.setattr(client, "_upload_image", lambda path: Path(path).name)

    def fake_run(_template: str, payload: dict, output: Path) -> GenerationResult:
        captured["patches"] = payload["patches"]
        output.mkdir(parents=True, exist_ok=True)
        result = output / "clip.mp4"
        result.write_bytes(b"video")
        return GenerationResult(files=[result], metadata={})

    monkeypatch.setattr(client, "_run", fake_run)
    client.generate_video(
        tmp_path / "video",
        {
            "scene_id": "scene-1",
            "spec": {
                "narrative_purpose": "The worker raises a lantern",
                "motion": "Raise the lantern",
                "camera": "locked camera",
                "negative_constraints": [],
            },
            "visual_style": "cinematic 3D cartoon",
            "seed": 7,
            "width": 384,
            "height": 672,
            "frames": 49,
            "start_image_path": str(start),
            "end_image_path": str(end),
        },
    )
    values = {patch["input"]: patch["value"] for patch in captured["patches"]}
    assert "MANDATORY VISUAL STYLE: cinematic 3D cartoon" in values["prompt"]
    assert "Do not convert it to photorealistic" in values["prompt"]
    assert "photorealistic" in values["negative_prompt"]
