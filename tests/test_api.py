from __future__ import annotations

from fastapi.testclient import TestClient

from vertical_studio.api import create_app


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
