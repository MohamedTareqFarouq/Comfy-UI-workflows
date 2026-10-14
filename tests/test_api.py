from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from vertical_studio.api import create_app
from vertical_studio.services.media import run_ffmpeg


def test_pwa_and_project_api_contract(components) -> None:
    app = create_app(components.settings)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Vertical Video Studio" in page.text
        script = client.get("/app.js")
        assert script.status_code == 200
        assert "projectActivity" in script.text
        assert "Planning storyboard with Qwen" in script.text

        observability = client.get("/observability.html")
        assert "Uploading reference video" in script.text
        assert "uploadReference" in script.text
        assert 'idea.required=!reference' in script.text
        assert "projectId!==state.current" in script.text
        assert "handleDetailRefreshError" in script.text
        assert "error.status=response.status" in script.text
        assert observability.status_code == 200
        assert "Prompt and decision audit" in observability.text
        audit_script = client.get("/observability.js")
        assert audit_script.status_code == 200

        created = client.post(
            "/api/projects",
            json={
                "title": "API test",
                "idea": "A portrait-format miniature story",
                "duration_seconds": 10,
                "visual_style": "cinematic 3D cartoon",
                "research_required": False,
            },
        )
        assert created.status_code == 201
        project_id = created.json()["id"]

        planned = client.post(f"/api/projects/{project_id}/plan", json={})
        assert planned.status_code == 200
        detail = client.get(f"/api/projects/{project_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["project"]["state"] == "storyboard_review"
        assert 3 <= len(body["scenes"]) <= 10
        assert "research_sources" in body
        assert "attempts" in body
        assert "approvals" in body
        assert "render_manifests" in body
        trace = body["character_bible"]["_prompt_audit"]["qwen_storyboard"]
        assert trace["model"] == "simulation-fallback"
        assert trace["parsed_response"]["scenes"]


def test_storyboard_edit_regenerate_retry_and_delete(components) -> None:
    app = create_app(components.settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={"idea": "Three distinct moments", "duration_seconds": 10},
        ).json()
        project_id = created["id"]
        assert client.post(f"/api/projects/{project_id}/plan", json={}).status_code == 200
        detail = client.get(f"/api/projects/{project_id}").json()
        scene = detail["scenes"][0]
        changed = {**scene["spec"], "title": "Edited opening"}
        response = client.patch(f"/api/scenes/{scene['id']}", json=changed)
        assert response.status_code == 200
        assert response.json()["spec"]["title"] == "Edited opening"

        regenerated = client.post(
            f"/api/projects/{project_id}/plan",
            json={"feedback": "Make every scene visually distinct"},
        )
        assert regenerated.status_code == 200

        restarted = client.post(f"/api/projects/{project_id}/retry", json={})
        assert restarted.status_code == 200
        assert restarted.json()["state"] == "draft"

        deleted = client.delete(f"/api/projects/{project_id}")
        assert deleted.status_code == 204
        assert all(item["id"] != project_id for item in client.get("/api/projects").json())


def test_reference_video_becomes_reviewable_storyboard(
    components, tmp_path: Path
) -> None:
    source = tmp_path / "reference.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "color=c=0x36596b:s=270x480:r=8:d=10",
        "-vf", "format=yuv420p",
        "-c:v", "libx264", "-preset", "ultrafast", "-an", str(source),
    ])
    app = create_app(components.settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={
                "title": "Reference remake",
                "idea": "Faithfully reconstruct the uploaded reference",
                "duration_seconds": 10,
                "source_mode": "reference",
            },
        )
        assert created.status_code == 201
        project_id = created.json()["id"]
        assert created.json()["source_mode"] == "reference"

        blocked = client.post(f"/api/projects/{project_id}/plan", json={})
        assert blocked.status_code == 409
        assert "analyze and approve" in blocked.json()["detail"]

        uploaded = client.post(
            f"/api/projects/{project_id}/reference/upload",
            content=source.read_bytes(),
            headers={
                "content-type": "application/octet-stream",
                "x-filename": source.name,
            },
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["original_name"] == source.name
        assert uploaded.json()["metadata"]["streams"][0]["codec_type"] == "video"

        analyzed = client.post(
            f"/api/projects/{project_id}/reference/analyze"
        )
        assert analyzed.status_code == 202
        job = analyzed.json()
        assert job["id"]
        assert job["kind"] == "reference_analysis"
        assert job["state"] == "queued"

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = client.get(f"/api/projects/{project_id}").json()
            if detail["project"]["state"] == "reference_review":
                break
            time.sleep(0.05)
        assert detail["project"]["state"] == "reference_review"
        blueprint = detail["reference_analysis"]["blueprint"]
        assert blueprint["style"]["medium"]
        assert blueprint["motion"]["animation_style"]
        assert len(blueprint["canonical_assets"]) == 2
        assert len(blueprint["scenes"]) >= 3
        assert detail["reference_source"]["artifact_id"]
        evidence = [
            artifact for artifact in detail["artifacts"]
            if artifact["metadata"].get("role") == "reference_evidence"
        ]
        assert len(evidence) == 8

        approved = client.post(
            f"/api/projects/{project_id}/reference/approval",
            json={"approved": True, "notes": ""},
        )
        assert approved.status_code == 200
        detail = client.get(f"/api/projects/{project_id}").json()
        assert detail["project"]["state"] == "storyboard_review"
        assert detail["reference_analysis"]["approved"] is True
        assert detail["canonical_assets"]
        assert detail["scenes"]
