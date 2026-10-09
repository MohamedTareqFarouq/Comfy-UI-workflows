from __future__ import annotations
import hashlib, json, math, shutil
from pathlib import Path
from typing import Any
from .config import Settings
from .db import Database
from .schemas import (ApprovalRequest, ArtifactSelection, ExecutionMode, Job, JobKind, JobState,
 PlanRequest, Project, ProjectCreate, ProjectState, ProjectUpdate, RenderRequest, RetryRequest,
 Scene, SceneSpec, SceneState, Storyboard)
from .services.gpu import GpuCoordinator
from .services.model_gateway import ModelGateway, ModelUnavailable
from .services.research import BraveResearchProvider

def stable_key(*values: Any) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()

class Orchestrator:
    def __init__(self, db: Database, settings: Settings, model: ModelGateway,
                 research: BraveResearchProvider, gpu: GpuCoordinator):
        self.db, self.settings, self.model, self.research = db, settings, model, research
        self.gpu = gpu

    def create_project(self, request: ProjectCreate) -> Project:
        mode = ExecutionMode.SIMULATION if self.settings.is_simulation else ExecutionMode.REAL
        return self.db.create_project(request, mode)

    def plan_project(self, project_id: str, request: PlanRequest | None = None) -> Storyboard:
        project = self.db.get_project(project_id)
        research_context = ""
        if project.state == ProjectState.DRAFT and project.research_required:
            project = self.db.transition_project(project_id, ProjectState.RESEARCH_NEEDED)
        if project.state == ProjectState.RESEARCH_NEEDED:
            if self.settings.is_simulation:
                research_context = "Simulation research; no external factual claims introduced."
            elif self.research.configured:
                sources = self.research.search(project.idea)
                self.db.save_research_sources(project_id, sources)
                research_context = json.dumps(sources)
            else:
                sources = self.db.list_research_sources(project_id)
                if not sources:
                    raise ValueError(
                        "research is required but Brave Search is not configured; "
                        "create the project with source_urls"
                    )
                research_context = json.dumps(sources)
            project = self.db.transition_project(project_id, ProjectState.PLANNING)
        elif project.state == ProjectState.DRAFT:
            project = self.db.transition_project(project_id, ProjectState.PLANNING)
        elif project.state == ProjectState.STORYBOARD_REVIEW:
            project = self.db.transition_project(project_id, ProjectState.PLANNING, "revision")
        if project.state != ProjectState.PLANNING:
            raise ValueError(f"cannot plan from {project.state}")
        storyboard = self._storyboard(
            project, research_context, (request or PlanRequest()).feedback
        )
        self.db.replace_storyboard(project_id, storyboard.scenes, storyboard.character_bible)
        self.db.transition_project(project_id, ProjectState.STORYBOARD_REVIEW)
        return storyboard

    def _storyboard(
        self, project: Project, research_context: str, feedback: str = ""
    ) -> Storyboard:
        if not self.settings.is_simulation:
            try:
                with self.gpu.qwen():
                    return self.model.generate_structured(
                        Storyboard,
                        system=(
                            "Create a coherent vertical 9:16 storyboard of 3-10 distinct "
                            "scenes. Each scene must advance a different story beat. Scene "
                            "durations must sum exactly to the requested project duration and each "
                            "must be between 1 and 6 seconds. Preserve identity, continuity and "
                            "safe framing. The top-level keys are summary, character_bible, and "
                            "scenes; never use a storyboard wrapper. Every scene uses title, "
                            "narrative_purpose, duration_seconds, start_composition, "
                            "end_composition, and motion; never substitute scene, duration, "
                            "description, or beat. For every scene, describe both its opening keyframe in "
                            "start_composition and its closing keyframe in end_composition, plus "
                            "the motion connecting them. Apply revision feedback. Return JSON only."
                        ),
                        context=json.dumps(
                            {
                                "project": project.model_dump(),
                                "research": research_context,
                                "revision_feedback": feedback,
                            },
                            default=str,
                        ),
                    )
            except ModelUnavailable:
                if self.model.managed:
                    raise
        count = max(3, min(10, math.ceil(project.duration_seconds / 3)))
        duration = project.duration_seconds / count
        scenes = [SceneSpec(
            title=f"Scene {n}",
            narrative_purpose=f"Advance part {n} of the story: {project.idea}",
            duration_seconds=duration,
            start_composition=f"Vertical 9:16 {project.visual_style}; establish scene {n}, centered safe framing",
            end_composition=f"Vertical 9:16 {project.visual_style}; complete scene {n} while preserving identity",
            motion="One clear continuous action with stable identity and natural movement",
            camera="Stable cinematic camera with restrained movement",
            continuity=["same identity", "same wardrobe", "consistent palette"],
            negative_constraints=["identity change", "wardrobe change", "extra limbs", "text", "watermark", "jitter"],
        ) for n in range(1, count + 1)]
        return Storyboard(
            summary=f"A {project.duration_seconds}-second vertical interpretation of {project.idea}",
            character_bible={"style": project.visual_style, "identity_rule": "Preserve face, silhouette, wardrobe, palette and proportions."},
            scenes=scenes,
        )

    def update_project(self, project_id: str, request: ProjectUpdate) -> Project:
        project = self.db.get_project(project_id)
        if project.state not in {
            ProjectState.DRAFT, ProjectState.PLANNING, ProjectState.STORYBOARD_REVIEW
        }:
            raise ValueError("project details can only be edited before generation")
        return self.db.update_project(project_id, request)

    def update_scene(self, scene_id: str, spec: SceneSpec) -> Scene:
        scene = self.db.get_scene(scene_id)
        project = self.db.get_project(scene.project_id)
        if project.state != ProjectState.STORYBOARD_REVIEW or scene.state != SceneState.PLANNED:
            raise ValueError("scenes can only be edited during storyboard review")
        others = [item.spec for item in self.db.list_scenes(project.id) if item.id != scene_id]
        Storyboard(summary="edited", character_bible={}, scenes=[*others, spec])
        return self.db.update_scene_spec(scene_id, spec)

    def delete_scene(self, scene_id: str) -> None:
        scene = self.db.get_scene(scene_id)
        project = self.db.get_project(scene.project_id)
        if project.state != ProjectState.STORYBOARD_REVIEW or scene.state != SceneState.PLANNED:
            raise ValueError("scenes can only be deleted during storyboard review")
        self.db.delete_scene(scene_id)

    def retry_project(self, project_id: str) -> Project:
        self.db.reset_project(project_id)
        project_dir = self.settings.projects_dir / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)
        export = self.settings.export_dir / f"{project_id}.mp4"
        if export.exists():
            export.unlink()
        return self.db.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        self.db.delete_project(project_id)
        project_dir = self.settings.projects_dir / project_id
        if project_dir.exists():
            shutil.rmtree(project_dir)
        export = self.settings.export_dir / f"{project_id}.mp4"
        if export.exists():
            export.unlink()

    def approve_storyboard(self, project_id: str, request: ApprovalRequest) -> list[Job]:
        if self.db.get_project(project_id).state != ProjectState.STORYBOARD_REVIEW:
            raise ValueError("project is not awaiting storyboard approval")
        self.db.record_approval(project_id, "storyboard", request.approved, request.notes)
        if not request.approved:
            self.db.transition_project(project_id, ProjectState.PLANNING, request.notes)
            return []
        self.db.transition_project(project_id, ProjectState.REFERENCES)
        return [self.queue_keyframes(scene.id) for scene in self.db.list_scenes(project_id)]

    def queue_keyframes(self, scene_id: str) -> Job:
        scene = self.db.get_scene(scene_id)
        active = self._active(scene_id, JobKind.KEYFRAME)
        if active: return active
        if scene.state != SceneState.KEYFRAMES_QUEUED:
            scene = self.db.transition_scene(scene_id, SceneState.KEYFRAMES_QUEUED)
        prior = [j for j in self.db.list_jobs(scene.project_id) if j.scene_id == scene.id and j.kind == JobKind.KEYFRAME]
        payload = {"scene_id": scene.id, "scene_revision": scene.revision, "spec": scene.spec.model_dump(),
                   "candidates": 2, "width": 384, "height": 672, "template": "flux_t2i_api",
                   "generation_round": len(prior) + 1}
        return self.db.create_job(scene.project_id, JobKind.KEYFRAME, stable_key("keyframe", payload), payload, scene.id)

    def approve_keyframes(self, scene_id: str, selection: ArtifactSelection, request: ApprovalRequest) -> Scene:
        scene = self.db.get_scene(scene_id)
        if scene.state != SceneState.KEYFRAMES_READY: raise ValueError("keyframes are not ready")
        self.db.record_approval(scene.project_id, "keyframes", request.approved, request.notes, scene.id)
        if not request.approved:
            self.queue_keyframes(scene.id)
            return self.db.get_scene(scene.id)
        if not selection.start_artifact_id or not selection.end_artifact_id:
            raise ValueError("select start and end keyframes")
        self.db.select_artifacts(scene.id, start_id=selection.start_artifact_id, end_id=selection.end_artifact_id)
        scene = self.db.transition_scene(scene.id, SceneState.KEYFRAMES_APPROVED)
        scenes = self.db.list_scenes(scene.project_id)
        if all(s.state == SceneState.KEYFRAMES_APPROVED for s in scenes):
            # This is the stage boundary: keep Flux resident through all image
            # generation/review, then free it once before any Wan job is queued.
            self.gpu.release_comfy()
            if self.db.get_project(scene.project_id).state == ProjectState.CONTACT_SHEET_REVIEW:
                self.db.transition_project(scene.project_id, ProjectState.SCENES)
            for item in scenes:
                self.queue_video(item.id)
        return scene

    def queue_video(self, scene_id: str, retry: RetryRequest | None = None) -> Job:
        scene = self.db.get_scene(scene_id)
        active = self._active(scene_id, JobKind.VIDEO)
        if active: return active
        if not scene.selected_start_artifact_id or not scene.selected_end_artifact_id:
            raise ValueError("approved keyframes required")
        if scene.state != SceneState.VIDEO_QUEUED:
            scene = self.db.transition_scene(scene.id, SceneState.VIDEO_QUEUED)
        prior = [j for j in self.db.list_jobs(scene.project_id) if j.scene_id == scene.id and j.kind == JobKind.VIDEO]
        ordinal = len(prior) + 1
        payload = {"scene_id": scene.id, "scene_revision": scene.revision, "spec": scene.spec.model_dump(),
          "start_artifact_id": scene.selected_start_artifact_id, "end_artifact_id": scene.selected_end_artifact_id,
          "attempt": ordinal, "duration_seconds": scene.spec.duration_seconds, "width": 384, "height": 672,
          "fps": 16, "frames": 49, "template": "wan14b_lightx2v_flf2v_api",
          "workflow_profile": "wan14b-lightx2v-4step-unqualified",
          "prompt_override": retry.changed_prompt if retry else None, "new_seed": retry.new_seed if retry else True}
        return self.db.create_job(scene.project_id, JobKind.VIDEO, stable_key("video", scene.id, ordinal, payload), payload, scene.id)

    def approve_scene(self, scene_id: str, request: ApprovalRequest) -> Scene:
        scene = self.db.get_scene(scene_id)
        if scene.state not in {SceneState.VIDEO_READY, SceneState.QA_FAILED}: raise ValueError("video not ready")
        self.db.record_approval(scene.project_id, "video", request.approved, request.notes, scene.id)
        if request.approved:
            scene = self.db.transition_scene(scene.id, SceneState.APPROVED, "user approval")
            if all(s.state == SceneState.APPROVED for s in self.db.list_scenes(scene.project_id)):
                if self.db.get_project(scene.project_id).state == ProjectState.SCENES:
                    self.db.transition_project(scene.project_id, ProjectState.FINAL_REVIEW)
        else:
            self.queue_video(scene.id, RetryRequest())
        return scene

    def queue_render(self, project_id: str, request: RenderRequest) -> Job:
        project = self.db.get_project(project_id)
        scenes = self.db.list_scenes(project_id)
        if project.state not in {ProjectState.FINAL_REVIEW, ProjectState.COMPLETE} or any(s.state != SceneState.APPROVED for s in scenes):
            raise ValueError("all scenes must be approved")
        ids = [s.selected_video_artifact_id for s in scenes]
        payload = {"artifact_ids": ids, "export_path": request.export_path}
        job = self.db.create_job(
            project_id, JobKind.RENDER,
            stable_key("render", project_id, ids, request.export_path), payload,
        )
        # An identical completed render is already the idempotent result. A new
        # path or changed clip selection creates a fresh job and render manifest.
        if job.state == JobState.QUEUED:
            self.db.transition_project(project_id, ProjectState.RENDERING, "render requested")
        return job

    def cancel_project(self, project_id: str) -> list[Job]:
        project = self.db.get_project(project_id)
        cancelled = self.db.cancel_project_jobs(project_id)
        if project.state != ProjectState.PAUSED and project.state != ProjectState.COMPLETE:
            self.db.transition_project(project_id, ProjectState.PAUSED, "jobs cancelled")
        return cancelled

    def pause_project(self, project_id: str) -> Project:
        return self.db.transition_project(project_id, ProjectState.PAUSED)

    def resume_project(self, project_id: str) -> Project:
        return self.db.resume_project(project_id)

    def _active(self, scene_id: str, kind: JobKind) -> Job | None:
        scene = self.db.get_scene(scene_id)
        for job in reversed(self.db.list_jobs(scene.project_id)):
            if job.scene_id == scene_id and job.kind == kind and job.state in {JobState.QUEUED, JobState.RUNNING}:
                return job
        return None
