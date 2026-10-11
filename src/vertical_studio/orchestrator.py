from __future__ import annotations
import hashlib, json, math, shutil
from pathlib import Path
from typing import Any
from .config import Settings
from .db import Database
from .schemas import (ApprovalRequest, ArtifactSelection, CanonicalAssetSpec, ExecutionMode, Job, JobKind, JobState,
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

    def reconcile_project_state(self, project_id: str) -> Project:
        """Advance persisted project milestones implied by completed scene states."""
        scenes = self.db.list_scenes(project_id)
        project = self.db.get_project(project_id)
        if not scenes:
            return project
        keyframes_reviewable = {
            SceneState.KEYFRAMES_READY, SceneState.KEYFRAMES_APPROVED,
            SceneState.VIDEO_QUEUED, SceneState.VIDEO_READY,
            SceneState.QA_FAILED, SceneState.APPROVED,
        }
        video_phase = {
            SceneState.KEYFRAMES_APPROVED, SceneState.VIDEO_QUEUED,
            SceneState.VIDEO_READY, SceneState.QA_FAILED, SceneState.APPROVED,
        }
        if project.state == ProjectState.REFERENCES and all(
            scene.state in keyframes_reviewable for scene in scenes
        ):
            project = self.db.transition_project(
                project_id, ProjectState.CONTACT_SHEET_REVIEW,
                "reconciled completed keyframe generation",
            )
        if project.state == ProjectState.CONTACT_SHEET_REVIEW and all(
            scene.state in video_phase for scene in scenes
        ):
            project = self.db.transition_project(
                project_id, ProjectState.SCENES,
                "reconciled completed keyframe approvals",
            )
        if project.state == ProjectState.SCENES and all(
            scene.state == SceneState.APPROVED for scene in scenes
        ):
            project = self.db.transition_project(
                project_id, ProjectState.FINAL_REVIEW,
                "reconciled completed video approvals",
            )
        return project

    def reconcile_all_project_states(self) -> None:
        for project in self.db.list_projects():
            self.reconcile_project_state(project.id)

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
        self.model.last_trace = None
        storyboard = self._storyboard(
            project, research_context, (request or PlanRequest()).feedback
        )
        bible = {
            **storyboard.character_bible,
            "_generation_plan": {
                "requires_character_sheet": storyboard.requires_character_sheet,
                "character_sheet_prompt": storyboard.character_sheet_prompt,
                "visual_style": project.visual_style,
            },
            "_prompt_audit": {
                "qwen_storyboard": self.model.last_trace or {
                    "model": "simulation-fallback",
                    "request": {
                        "project": project.model_dump(mode="json"),
                        "research": research_context,
                        "revision_feedback": (request or PlanRequest()).feedback,
                    },
                    "parsed_response": storyboard.model_dump(mode="json"),
                }
            },
        }
        self.db.replace_storyboard(
            project_id, storyboard.scenes, bible, storyboard.canonical_assets
        )
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
                            "safe framing. Treat project.visual_style as a mandatory design constraint "
                            "and repeat it concretely in every scene composition and character bible. "
                            "Decide explicitly whether a reusable character sheet "
                            "is required. Divide scenes into contiguous continuity groups. The first "
                            "scene of every group must set starts_new_continuity=true and will use a "
                            "fresh T2I anchor; later scenes in that group must set it false and reuse "
                            "the exact previous end frame. Start a new group for a new location, main "
                            "character, time jump, radical composition, or deliberate cut. The top-level "
                            "keys include summary, requires_character_sheet, character_sheet_prompt, "
                            "character_bible, canonical_assets, and scenes; never use a storyboard wrapper. Every scene uses title, "
                            "narrative_purpose, duration_seconds, start_composition, "
                            "end_composition, motion, continuity_group, starts_new_continuity, "
                            "continuity_reason, and transition_from_previous; never substitute scene, "
                            "duration, description, or beat. Define reusable canonical_assets for every recurring "
                            "character, environment and important prop. Each asset needs a stable lowercase key, "
                            "role, label, clean single-subject generation prompt and description. Every scene must "
                            "list only the canonical asset keys it needs in reference_keys, plus characters_present, "
                            "environment_key, important_props, character_count, opening_state, closing_state, "
                            "object_owner and handoff_to_next_scene. For every scene, describe both its opening keyframe in "
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
            continuity_group="main-sequence",
            starts_new_continuity=n == 1,
            continuity_reason="First visual anchor" if n == 1 else "Same subject, location and continuous action",
            transition_from_previous="new_sequence" if n == 1 else "continuous",
            reference_keys=["protagonist", "primary_environment"],
            characters_present=["protagonist"],
            environment_key="primary_environment",
            character_count=1,
            opening_state=f"The protagonist begins story beat {n}",
            closing_state=f"The protagonist completes story beat {n}",
        ) for n in range(1, count + 1)]
        return Storyboard(
            summary=f"A {project.duration_seconds}-second vertical interpretation of {project.idea}",
            requires_character_sheet=True,
            character_sheet_prompt=(
                f"Full-body character turnaround and identity reference for the recurring protagonist "
                f"in {project.visual_style}; front, three-quarter and profile views, consistent face, "
                "wardrobe, proportions and neutral lighting"
            ),
            character_bible={"style": project.visual_style, "identity_rule": "Preserve face, silhouette, wardrobe, palette and proportions."},
            canonical_assets=[
                CanonicalAssetSpec(
                    key="protagonist", role="character", label="Recurring protagonist",
                    prompt=(
                        f"{project.visual_style}. One clean three-quarter full-body identity reference of the "
                        "recurring protagonist, neutral pose, consistent face, hair, wardrobe and proportions, "
                        "plain neutral background, one person only, no text, no reference-sheet layout."
                    ),
                    description="The recurring protagonist identity and wardrobe anchor.",
                ),
                CanonicalAssetSpec(
                    key="primary_environment", role="environment", label="Primary environment",
                    prompt=(
                        f"{project.visual_style}. Clean establishing reference of the main environment for: "
                        f"{project.idea}. No people, no text, coherent architecture and lighting, vertical composition."
                    ),
                    description="The stable architecture, palette and lighting anchor.",
                ),
            ],
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
        specs = [
            spec if item.id == scene_id else item.spec
            for item in self.db.list_scenes(project.id)
        ]
        if specs and not specs[0].starts_new_continuity:
            specs[0] = specs[0].model_copy(update={
                "starts_new_continuity": True,
                "transition_from_previous": "new_sequence",
                "continuity_reason": "First visual anchor",
            })
        bible = self.db.get_character_bible(project.id)
        plan = bible.get("_generation_plan", {})
        Storyboard(
            summary="edited",
            requires_character_sheet=bool(plan.get("requires_character_sheet")),
            character_sheet_prompt=plan.get("character_sheet_prompt", ""),
            character_bible=bible,
            canonical_assets=[
                CanonicalAssetSpec(**asset.model_dump())
                for asset in self.db.list_canonical_assets(project.id)
            ],
            scenes=specs,
        )
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
        scenes = self.db.list_scenes(project_id)
        if scenes and not scenes[0].spec.starts_new_continuity:
            first_spec = scenes[0].spec.model_copy(update={
                "starts_new_continuity": True,
                "transition_from_previous": "new_sequence",
                "continuity_reason": "Migrated first visual anchor",
            })
            self.db.update_scene_spec(scenes[0].id, first_spec)
        assets = self.db.list_canonical_assets(project_id)
        if assets:
            return [self.queue_canonical_asset(asset.id) for asset in assets]
        return [self.queue_keyframes(scene.id) for scene in self.db.list_scenes(project_id)]

    def queue_canonical_asset(self, asset_id: str) -> Job:
        asset = self.db.get_canonical_asset(asset_id)
        project = self.db.get_project(asset.project_id)
        prior = [
            job for job in self.db.list_jobs(project.id)
            if job.kind == JobKind.CANONICAL_ASSET
            and job.payload.get("canonical_asset_id") == asset.id
        ]
        active = next(
            (job for job in reversed(prior) if job.state in {JobState.QUEUED, JobState.RUNNING}),
            None,
        )
        if active:
            return active
        payload = {
            "canonical_asset_id": asset.id,
            "asset_key": asset.key,
            "asset_role": asset.role,
            "asset_label": asset.label,
            "prompt": asset.prompt,
            "description": asset.description,
            "visual_style": project.visual_style,
            "width": 720,
            "height": 1280,
            "template": "flux_t2i_api",
            "generation_round": len(prior) + 1,
        }
        return self.db.create_job(
            project.id, JobKind.CANONICAL_ASSET,
            stable_key("canonical_asset", asset.id, payload["generation_round"], payload), payload,
        )

    def approve_canonical_assets(
        self, project_id: str, request: ApprovalRequest
    ) -> list[Job]:
        project = self.db.get_project(project_id)
        if project.state != ProjectState.REFERENCES:
            raise ValueError("project is not awaiting canonical reference approval")
        assets = self.db.list_canonical_assets(project_id)
        if not assets:
            raise ValueError("project has no canonical assets")
        if request.approved and any(not asset.artifact_id for asset in assets):
            raise ValueError("all canonical references must be generated before approval")
        self.db.record_approval(
            project_id, "canonical_assets", request.approved, request.notes
        )
        self.db.approve_canonical_assets(project_id, request.approved)
        if not request.approved:
            return [self.queue_canonical_asset(asset.id) for asset in assets]
        return [self.queue_keyframes(scene.id) for scene in self.db.list_scenes(project_id)]

    def queue_character_sheet(self, project_id: str) -> Job:
        bible = self.db.get_character_bible(project_id)
        plan = bible.get("_generation_plan", {})
        for job in reversed(self.db.list_jobs(project_id)):
            if job.kind == JobKind.CHARACTER_SHEET and job.state in {JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED}:
                return job
        project = self.db.get_project(project_id)
        payload = {
            "prompt": plan.get("character_sheet_prompt", "consistent recurring character reference sheet"),
            "visual_style": plan.get("visual_style", project.visual_style),
            "character_bible": bible,
            "width": 384,
            "height": 672,
            "template": "flux_t2i_api",
        }
        return self.db.create_job(
            project_id, JobKind.CHARACTER_SHEET,
            stable_key("character_sheet", project_id, payload), payload,
        )

    def queue_keyframes(self, scene_id: str) -> Job:
        scene = self.db.get_scene(scene_id)
        project = self.db.get_project(scene.project_id)
        active = self._active(scene_id, JobKind.KEYFRAME)
        if active: return active
        assets = self.db.list_canonical_assets(scene.project_id)
        if assets and not all(asset.approved and asset.artifact_id for asset in assets):
            raise ValueError("approve all canonical references before generating scene keyframes")
        if scene.state != SceneState.KEYFRAMES_QUEUED:
            scene = self.db.transition_scene(scene_id, SceneState.KEYFRAMES_QUEUED)
        prior = [j for j in self.db.list_jobs(scene.project_id) if j.scene_id == scene.id and j.kind == JobKind.KEYFRAME]
        payload = {"scene_id": scene.id, "scene_revision": scene.revision, "spec": scene.spec.model_dump(),
                   "visual_style": project.visual_style,
                   "candidates": 2, "width": 384, "height": 672,
                   "generation_mode": "anchor" if scene.spec.starts_new_continuity else "continuation",
                   "t2i_template": "flux_t2i_api",
                   "multiref_template_prefix": "flux_klein_multiref",
                   "generation_round": len(prior) + 1}
        return self.db.create_job(scene.project_id, JobKind.KEYFRAME, stable_key("keyframe", payload), payload, scene.id)

    def regenerate_keyframes(self, scene_id: str) -> Job:
        scene = self.db.get_scene(scene_id)
        if scene.state not in {
            SceneState.KEYFRAMES_READY, SceneState.KEYFRAMES_APPROVED,
            SceneState.VIDEO_READY, SceneState.QA_FAILED, SceneState.APPROVED,
        }:
            raise ValueError("scene keyframes are not ready to regenerate")
        _affected, interrupted = self.db.invalidate_keyframe_chain(scene_id)
        if interrupted:
            self.gpu.generator.interrupt()
        return self.queue_keyframes(scene_id)

    def approve_keyframes(self, scene_id: str, selection: ArtifactSelection, request: ApprovalRequest) -> Scene:
        scene = self.db.get_scene(scene_id)
        if scene.state != SceneState.KEYFRAMES_READY: raise ValueError("keyframes are not ready")
        if not request.approved:
            self.db.record_approval(scene.project_id, "keyframes", False, request.notes, scene.id)
            self.queue_keyframes(scene.id)
            return self.db.get_scene(scene.id)
        if not selection.start_artifact_id or not selection.end_artifact_id:
            raise ValueError("select start and end keyframes")
        if not scene.spec.starts_new_continuity:
            scenes = self.db.list_scenes(scene.project_id)
            index = next(i for i, item in enumerate(scenes) if item.id == scene.id)
            previous = scenes[index - 1]
            if previous.spec.continuity_group != scene.spec.continuity_group:
                raise ValueError("continuation scene does not follow its continuity group")
            if previous.state != SceneState.KEYFRAMES_APPROVED or not previous.selected_end_artifact_id:
                raise ValueError("approve the previous connected scene first")
            selected_start = self.db.get_artifact(selection.start_artifact_id)
            if selected_start.metadata.get("shared_from_artifact_id") != previous.selected_end_artifact_id:
                raise ValueError("select the start frame that exactly matches the previous approved end frame")
        self.db.record_approval(scene.project_id, "keyframes", True, request.notes, scene.id)
        self.db.select_artifacts(scene.id, start_id=selection.start_artifact_id, end_id=selection.end_artifact_id)
        scene = self.db.transition_scene(scene.id, SceneState.KEYFRAMES_APPROVED)
        scenes = self.db.list_scenes(scene.project_id)
        index = next(i for i, item in enumerate(scenes) if item.id == scene.id)
        if index + 1 < len(scenes):
            following = scenes[index + 1]
            if (
                not following.spec.starts_new_continuity
                and following.spec.continuity_group == scene.spec.continuity_group
                and following.state == SceneState.PLANNED
            ):
                self.queue_keyframes(following.id)
                scenes = self.db.list_scenes(scene.project_id)
        if all(s.state == SceneState.KEYFRAMES_APPROVED for s in scenes):
            # This is the stage boundary: keep Flux resident through all image
            # generation/review, then free it once before any Wan job is queued.
            self.gpu.release_comfy()
            self.reconcile_project_state(scene.project_id)
            for item in scenes:
                self.queue_video(item.id)
        return scene

    def queue_video(self, scene_id: str, retry: RetryRequest | None = None) -> Job:
        scene = self.db.get_scene(scene_id)
        project = self.db.get_project(scene.project_id)
        active = self._active(scene_id, JobKind.VIDEO)
        if active: return active
        if not scene.selected_start_artifact_id or not scene.selected_end_artifact_id:
            raise ValueError("approved keyframes required")
        if scene.state != SceneState.VIDEO_QUEUED:
            scene = self.db.transition_scene(scene.id, SceneState.VIDEO_QUEUED)
        prior = [j for j in self.db.list_jobs(scene.project_id) if j.scene_id == scene.id and j.kind == JobKind.VIDEO]
        ordinal = len(prior) + 1
        payload = {"scene_id": scene.id, "scene_revision": scene.revision, "spec": scene.spec.model_dump(),
          "visual_style": project.visual_style,
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
            self.reconcile_project_state(scene.project_id)
        else:
            self.queue_video(scene.id, RetryRequest())
        return scene

    def queue_render(self, project_id: str, request: RenderRequest) -> Job:
        project = self.reconcile_project_state(project_id)
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
