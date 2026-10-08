from __future__ import annotations
import asyncio, threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, load_settings
from .db import Database
from .orchestrator import Orchestrator
from .schemas import ApprovalRequest, ArtifactSelection, ProjectCreate, RenderRequest, RetryRequest
from .services.comfy import ComfyClient
from .services.mock_comfy import MockComfyClient
from .services.model_gateway import ModelGateway
from .services.qa import QualityService
from .services.research import BraveResearchProvider
from .worker import Worker

class KeyframeApproval(BaseModel):
    selection: ArtifactSelection
    approval: ApprovalRequest

class Components:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.model = ModelGateway(settings.llama_url, settings.qwen_model)
        self.research = BraveResearchProvider(settings.brave_api_key)
        self.generator = MockComfyClient() if settings.is_simulation else ComfyClient(settings.comfy_url, settings.workflows_dir)
        self.orchestrator = Orchestrator(self.db, settings, self.model, self.research)
        self.worker = Worker(
            self.db, settings, self.generator,
            qa=QualityService(
                self.model, enable_visual_review=not settings.is_simulation
            ),
        )
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.worker.serve, daemon=True, name="vertical-studio-worker")
            self.thread.start()

    def stop(self) -> None:
        self.worker.stop()
        if self.thread: self.thread.join(timeout=5)

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    components = Components(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        components.start()
        yield
        components.stop()

    app = FastAPI(title="Vertical Video Studio", version="0.1.0", lifespan=lifespan)
    app.state.components = components

    def fail(exc: Exception):
        if isinstance(exc, KeyError): raise HTTPException(404, str(exc))
        raise HTTPException(409, str(exc))

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "mode": settings.execution_mode, "platform": settings.platform_mode,
            "generator": components.generator.health(), "model": components.model.health(),
            "data_dir": str(settings.data_dir), "real_generation_enabled": not settings.is_simulation,
        }

    @app.get("/api/projects")
    def projects():
        return components.db.list_projects()

    @app.post("/api/projects", status_code=201)
    def create_project(request: ProjectCreate):
        return components.orchestrator.create_project(request)

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str):
        try:
            return {
                "project": components.db.get_project(project_id),
                "scenes": components.db.list_scenes(project_id),
                "jobs": components.db.list_jobs(project_id),
                "attempts": components.db.list_attempts(project_id),
                "artifacts": components.db.list_artifacts(project_id),
                "character_bible": components.db.get_character_bible(project_id),
                "research_sources": components.db.list_research_sources(project_id),
                "qa_reports": components.db.list_qa_reports(project_id),
                "approvals": components.db.list_approvals(project_id),
                "render_manifests": components.db.list_render_manifests(project_id),
                "events": components.db.events_since(project_id),
            }
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/plan")
    def plan(project_id: str):
        try: return components.orchestrator.plan_project(project_id)
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/storyboard-approval")
    def storyboard_approval(project_id: str, request: ApprovalRequest):
        try: return components.orchestrator.approve_storyboard(project_id, request)
        except Exception as exc: fail(exc)

    @app.post("/api/scenes/{scene_id}/keyframe-approval")
    def keyframe_approval(scene_id: str, request: KeyframeApproval):
        try: return components.orchestrator.approve_keyframes(scene_id, request.selection, request.approval)
        except Exception as exc: fail(exc)

    @app.post("/api/scenes/{scene_id}/retry-keyframes")
    def retry_keyframes(scene_id: str):
        try: return components.orchestrator.queue_keyframes(scene_id)
        except Exception as exc: fail(exc)

    @app.post("/api/scenes/{scene_id}/retry-video")
    def retry_video(scene_id: str, request: RetryRequest):
        try: return components.orchestrator.queue_video(scene_id, request)
        except Exception as exc: fail(exc)

    @app.post("/api/scenes/{scene_id}/approval")
    def scene_approval(scene_id: str, request: ApprovalRequest):
        try: return components.orchestrator.approve_scene(scene_id, request)
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/render")
    def render(project_id: str, request: RenderRequest):
        try: return components.orchestrator.queue_render(project_id, request)
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/pause")
    def pause(project_id: str):
        try: return components.orchestrator.pause_project(project_id)
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/resume")
    def resume(project_id: str):
        try: return components.orchestrator.resume_project(project_id)
        except Exception as exc: fail(exc)

    @app.post("/api/projects/{project_id}/cancel")
    def cancel(project_id: str):
        try:
            cancelled = components.orchestrator.cancel_project(project_id)
            components.generator.interrupt()
            return cancelled
        except Exception as exc: fail(exc)

    @app.post("/api/simulation/drain")
    def drain():
        if not settings.is_simulation: raise HTTPException(403, "available only in simulation mode")
        return components.worker.run_until_idle()

    @app.get("/api/artifacts/{artifact_id}/content")
    def artifact_content(artifact_id: str):
        try:
            artifact = components.db.get_artifact(artifact_id)
            path = Path(artifact.path).resolve()
            allowed = (settings.projects_dir.resolve(), settings.export_dir.resolve())
            if not any(path == root or root in path.parents for root in allowed):
                raise HTTPException(403, "artifact is outside managed storage")
            if not path.exists(): raise HTTPException(404, "artifact file is missing")
            return FileResponse(path)
        except HTTPException: raise
        except Exception as exc: fail(exc)

    @app.websocket("/api/projects/{project_id}/events")
    async def events(websocket: WebSocket, project_id: str):
        await websocket.accept(); cursor = 0
        try:
            while True:
                values = components.db.events_since(project_id, cursor)
                for value in values:
                    cursor = max(cursor, value["id"]); await websocket.send_json(value)
                await asyncio.sleep(.5)
        except WebSocketDisconnect: pass

    app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
    return app

app = create_app()
