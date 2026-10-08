from __future__ import annotations

from fastapi.testclient import TestClient

from vertical_studio.api import create_app


def test_pwa_and_project_api_contract(components) -> None:
    app = create_app(components.settings)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Vertical Video Studio" in page.text

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
