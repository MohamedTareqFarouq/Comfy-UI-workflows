from __future__ import annotations
import hashlib, json, math, shutil
from pathlib import Path
from typing import Any
from .config import Settings
from .db import Database
from .schemas import (ApprovalRequest, ArtifactKind, ArtifactSelection, CanonicalAssetPromptProposal,
 CanonicalAssetPromptRevisionRequest, CanonicalAssetRetryRequest, CanonicalAssetSpec, ExecutionMode,
 Job, JobKind, JobState, KeyframeRetryPromptProposal, PlanRequest, Project, ProjectCreate,
 ProjectState, ProjectUpdate, ReferenceBlueprint, RenderRequest, RetryPromptBundle,
 RetryPromptRevisionRequest, RetryRequest, Scene, SceneSpec, SceneState, SourceMode,
 Storyboard, VideoRetryPromptProposal)
from .services.gpu import GpuCoordinator
from .services.media import probe_media, sha256_file
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
    def import_reference_file(
        self, project_id: str, source_path: Path, original_name: str
    ) -> dict[str, Any]:
        project = self.db.get_project(project_id)
        if project.source_mode != SourceMode.REFERENCE:
            raise ValueError("project was not created in reference mode")
        if project.state != ProjectState.DRAFT:
            raise ValueError("reference media can only be replaced before analysis")
        if not source_path.is_file():
            raise ValueError("reference video is missing")
        size = source_path.stat().st_size
        if size <= 0 or size > 250 * 1024 * 1024:
            raise ValueError("reference video must be between 1 byte and 250 MB")
        suffix = Path(original_name).suffix.lower()
        if suffix not in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
            raise ValueError("unsupported reference video type")
        metadata = probe_media(source_path)
        video_stream = next(
            (
                stream for stream in metadata.get("streams", [])
                if stream.get("codec_type") == "video"
            ),
            None,
        )
        if video_stream is None:
            raise ValueError("reference file contains no video stream")
        duration = float(metadata.get("format", {}).get("duration") or 0)
        if not 9.0 <= duration <= 30.5:
            raise ValueError("reference video must be approximately 10-30 seconds")

        directory = self.settings.projects_dir / project_id / "reference"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"source{suffix}"
        shutil.copy2(source_path, target)
        digest = sha256_file(target)
        artifact = self.db.add_artifact(
            project_id,
            ArtifactKind.VIDEO,
            target,
            digest,
            {
                "role": "reference_source",
                "original_name": Path(original_name).name,
                "probe": metadata,
            },
        )
        rounded_duration = max(10, min(30, round(duration)))
        if rounded_duration != project.duration_seconds:
            self.db.update_project(
                project_id, ProjectUpdate(duration_seconds=rounded_duration)
            )
        return self.db.add_reference_source(
            project_id,
            origin="upload",
            original_name=Path(original_name).name,
            path=target,
            sha256=digest,
            metadata=metadata,
            artifact_id=artifact.id,
        )

    def analyze_reference(self, project_id: str) -> Job:
        project = self.db.get_project(project_id)
        if project.source_mode != SourceMode.REFERENCE:
            raise ValueError("project was not created in reference mode")
        if project.state == ProjectState.DRAFT:
            project = self.db.transition_project(
                project_id, ProjectState.REFERENCE_ANALYSIS
            )
        elif project.state != ProjectState.REFERENCE_ANALYSIS:
            raise ValueError("reference analysis can only run before blueprint review")
        source = self.db.get_reference_source(project_id)
        if source is None:
            raise ValueError("upload a reference video before analysis")

        prior = [
            job for job in self.db.list_jobs(project_id)
            if job.kind == JobKind.REFERENCE_ANALYSIS
        ]
        active = next(
            (
                job for job in reversed(prior)
                if job.state in {JobState.QUEUED, JobState.RUNNING}
            ),
            None,
        )
        if active:
            return active
        payload = {
            "source_id": source["id"],
            "source_name": source["original_name"],
            "analysis_round": len(prior) + 1,
            "frame_count": 8,
        }
        return self.db.create_job(
            project_id,
            JobKind.REFERENCE_ANALYSIS,
            stable_key("reference_analysis", project_id, payload),
            payload,
        )

    def approve_reference_analysis(
        self, project_id: str, request: ApprovalRequest
    ) -> Storyboard | Project:
        project = self.db.get_project(project_id)
        if project.state != ProjectState.REFERENCE_REVIEW:
            raise ValueError("project is not awaiting reference blueprint approval")
        analysis = self.db.get_reference_analysis(project_id)
        if analysis is None:
            raise ValueError("reference blueprint is missing")
        self.db.record_approval(
            project_id, "reference_blueprint", request.approved, request.notes
        )
        if not request.approved:
            return self.db.transition_project(
                project_id, ProjectState.REFERENCE_ANALYSIS, request.notes
            )
        blueprint = ReferenceBlueprint.model_validate(analysis["blueprint"])
        storyboard = blueprint.as_storyboard()
        style = blueprint.style
        visual_style = "; ".join(filter(None, [
            style.medium, style.visual_language,
            f"palette: {', '.join(style.palette)}" if style.palette else "",
            f"lighting: {style.lighting}" if style.lighting else "",
            f"materials: {style.materials}" if style.materials else "",
        ]))
        self.db.update_project(
            project_id, ProjectUpdate(visual_style=visual_style[:500])
        )
        bible = {
            **storyboard.character_bible,
            "_generation_plan": {
                "requires_character_sheet": storyboard.requires_character_sheet,
                "character_sheet_prompt": storyboard.character_sheet_prompt,
                "visual_style": visual_style[:500],
                "reference_analysis_id": analysis["id"],
            },
            "_prompt_audit": {"reference_analysis": analysis["trace"]},
        }
        self.db.replace_storyboard(
            project_id, storyboard.scenes, bible, storyboard.canonical_assets
        )
        self.db.approve_reference_analysis(analysis["id"])
        self.db.transition_project(project_id, ProjectState.STORYBOARD_REVIEW)
        return storyboard


    def plan_project(self, project_id: str, request: PlanRequest | None = None) -> Storyboard:
        project = self.db.get_project(project_id)
        if project.source_mode == SourceMode.REFERENCE:
            raise ValueError("analyze and approve the reference blueprint instead of text planning")
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
                            "character, environment and important prop. Create exactly one character asset per physical "
                            "character and one environment asset per visually distinct location or room, such as a kitchen, "
                            "dining room, doorway or corridor; never combine a cast or multiple rooms into one asset. Each "
                            "asset needs a stable lowercase key, role, label, clean single-subject generation prompt and "
                            "description. Every scene must list only canonical asset keys in reference_keys, "
                            "characters_present and important_props. Set environment_key and opening_environment_key to the "
                            "location visible in the opening frame, and closing_environment_key to the location visible in "
                            "the closing frame. Use the same lock when the background does not change. For a room transition, "
                            "use different opening and closing locks and make the compositions explicitly match those rooms. "
                            "A connected scene's opening environment must equal the previous scene's closing environment. "
                            "Never allow a kitchen reference in a dining-room-only endpoint. FLUX Edit accepts at most "
                            "the exact opening/continuity frame plus one selected canonical character or environment image. "
                            "For a stable location prefer the primary character reference; for a location transition prefer "
                            "the destination environment reference. Other asset keys remain descriptive metadata. Also provide "
                            "character_count, opening_state, closing_state, object_owner and handoff_to_next_scene. For every "
                            "scene, describe both its opening keyframe in "
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
            opening_environment_key="primary_environment",
            closing_environment_key="primary_environment",
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
        project = self.db.get_project(project_id)
        if project.source_mode == SourceMode.REFERENCE:
            raise ValueError("reference projects cannot discard their source with legacy restart")
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
        project = self.db.get_project(project_id)
        if project.state != ProjectState.STORYBOARD_REVIEW:
            raise ValueError("project is not awaiting storyboard approval")
        self.db.record_approval(project_id, "storyboard", request.approved, request.notes)
        if not request.approved:
            target = (
                ProjectState.REFERENCE_REVIEW
                if project.source_mode == SourceMode.REFERENCE else ProjectState.PLANNING
            )
            self.db.transition_project(project_id, target, request.notes)
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
        reference_frames: list[dict[str, Any]] = []
        if project.source_mode == SourceMode.REFERENCE:
            analysis = self.db.get_reference_analysis(project.id)
            if analysis and analysis["approved"]:
                evidence_ids = analysis["trace"].get("evidence_artifact_ids", [])
                asset_blueprint = next(
                    (
                        item for item in analysis["blueprint"].get("canonical_assets", [])
                        if item.get("key") == asset.key
                    ),
                    {},
                )
                requested = list(dict.fromkeys(
                    asset_blueprint.get("evidence_frame_indices", [])
                ))[:2]
                selected = [
                    index for index in requested
                    if isinstance(index, int) and 1 <= index <= len(evidence_ids)
                ]
                if not selected and evidence_ids:
                    count = min(2, len(evidence_ids))
                    selected = (
                        [1] if count == 1 else [
                            round(offset * (len(evidence_ids) - 1) / (count - 1)) + 1
                            for offset in range(count)
                        ]
                    )
                for index in selected:
                    artifact = self.db.get_artifact(evidence_ids[index - 1])
                    reference_frames.append({
                        "artifact_id": artifact.id,
                        "key": f"evidence_frame_{index}",
                        "role": "source_evidence",
                        "label": f"source evidence frame {index}",
                        "path": artifact.path,
                    })

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
            "single_edit_template": "image_flux2_klein_image_edit_4b_distilled_api",
            "edit_template": "flux_edit_api",
            "reference_frames": reference_frames,
            "generation_round": len(prior) + 1,
        }
        return self.db.create_job(
            project.id, JobKind.CANONICAL_ASSET,
            stable_key("canonical_asset", asset.id, payload["generation_round"], payload), payload,
        )

    def get_canonical_asset_retry_prompt(self, asset_id: str) -> dict[str, str]:
        asset = self.db.get_canonical_asset(asset_id)
        project = self.db.get_project(asset.project_id)
        if project.state != ProjectState.REFERENCES or asset.approved:
            raise ValueError("canonical references can only be edited during reference review")
        return {
            "asset_id": asset.id,
            "role": asset.role,
            "label": asset.label,
            "prompt": asset.prompt,
            "description": asset.description,
        }

    def revise_canonical_asset_prompt(
        self, asset_id: str, request: CanonicalAssetPromptRevisionRequest
    ) -> CanonicalAssetPromptProposal:
        asset = self.db.get_canonical_asset(asset_id)
        project = self.db.get_project(asset.project_id)
        if project.state != ProjectState.REFERENCES or asset.approved:
            raise ValueError("canonical references can only be edited during reference review")
        if self.settings.is_simulation:
            return CanonicalAssetPromptProposal(
                prompt=(
                    f"{request.prompt.rstrip()} Address this user feedback explicitly: "
                    f"{request.feedback.strip()}"
                ),
                rationale="Simulation revision appended the requested correction.",
            )
        system = (
            "Revise only the supplied canonical Flux reference prompt using the user's feedback. "
            "Keep this as one isolated reference asset, not a collage or scene. Preserve its role, "
            "identity description, visual style, and intended environment or prop details. Do not "
            "introduce unrelated characters, rooms, props, text, panels, or alternate views. "
            "For a character, show only that character. For an environment, show the empty location "
            "unless its description explicitly requires a person. Keep prompt under 220 words and "
            "rationale under 60 words. Return prompt and rationale as JSON only."
        )
        context = {
            "required_visual_style": project.visual_style,
            "asset": {
                "role": asset.role,
                "label": asset.label,
                "description": asset.description,
            },
            "current_prompt": request.prompt,
            "user_feedback": request.feedback,
        }
        with self.gpu.qwen():
            return self.model.generate_structured(
                CanonicalAssetPromptProposal,
                system=system,
                context=json.dumps(context),
                max_tokens=800,
            )

    def retry_canonical_asset(
        self, asset_id: str, request: CanonicalAssetRetryRequest
    ) -> Job:
        asset = self.db.get_canonical_asset(asset_id)
        project = self.db.get_project(asset.project_id)
        if project.state != ProjectState.REFERENCES or asset.approved:
            raise ValueError("canonical references can only be edited during reference review")
        interrupted = self.db.reset_canonical_asset(
            asset_id,
            request.prompt,
            request.feedback,
            request.revision_rationale,
        )
        if interrupted:
            self.gpu.generator.interrupt()
        return self.queue_canonical_asset(asset_id)

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

    def queue_keyframes(self, scene_id: str, retry: RetryRequest | None = None) -> Job:
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
                   "candidates": 1, "width": 384, "height": 672,
                   "generation_mode": "anchor" if scene.spec.starts_new_continuity else "continuation",
                   "t2i_template": "flux_t2i_api",
                   "single_edit_template": "image_flux2_klein_image_edit_4b_distilled_api",
                   "edit_template": "flux_edit_api",
                   "generation_round": len(prior) + 1,
                   "start_prompt_override": retry.changed_start_prompt if retry else None,
                   "end_prompt_override": retry.changed_end_prompt if retry else None,
                   "negative_prompt_override": retry.changed_negative_prompt if retry else None,
                   "retry_feedback": retry.feedback if retry else "",
                   "revision_rationale": retry.revision_rationale if retry else ""}
        return self.db.create_job(scene.project_id, JobKind.KEYFRAME, stable_key("keyframe", payload), payload, scene.id)

    def get_retry_prompts(self, scene_id: str, stage: str) -> RetryPromptBundle:
        if stage not in {"keyframe", "video"}:
            raise ValueError("retry stage must be keyframe or video")
        scene = self.db.get_scene(scene_id)
        artifacts = self.db.list_artifacts(scene.project_id, scene.id)
        if stage == "keyframe":
            for artifact in reversed(artifacts):
                if artifact.kind != ArtifactKind.IMAGE:
                    continue
                runs = artifact.metadata.get("runs", [])
                if not runs:
                    continue
                candidate = artifact.metadata.get("candidate", 1)
                matching = [run for run in runs if run.get("candidate") == candidate]
                start = next((run for run in matching if run.get("role") == "start"), None)
                end = next((run for run in matching if run.get("role") == "end"), None)
                if end:
                    return RetryPromptBundle(
                        stage="keyframe",
                        start_prompt=start.get("prompt") if start else None,
                        end_prompt=end.get("prompt"),
                        negative_prompt=end.get("negative_prompt", ""),
                    )
            return RetryPromptBundle(
                stage="keyframe",
                start_prompt=(
                    scene.spec.start_composition if scene.spec.starts_new_continuity else None
                ),
                end_prompt=scene.spec.end_composition,
                negative_prompt=", ".join(scene.spec.negative_constraints),
            )
        for artifact in reversed(artifacts):
            if artifact.kind != ArtifactKind.VIDEO:
                continue
            audit = artifact.metadata.get("prompt_audit")
            if audit:
                return RetryPromptBundle(
                    stage="video",
                    video_prompt=audit.get("prompt", ""),
                    negative_prompt=audit.get("negative_prompt", ""),
                )
        return RetryPromptBundle(
            stage="video",
            video_prompt=(
                f"{scene.spec.narrative_purpose}. Motion: {scene.spec.motion}. "
                f"Camera: {scene.spec.camera}."
            ),
            negative_prompt=", ".join(scene.spec.negative_constraints),
        )

    def revise_retry_prompts(
        self, scene_id: str, stage: str, request: RetryPromptRevisionRequest
    ) -> RetryPromptBundle:
        if stage != request.stage:
            raise ValueError("retry stage does not match the prompt revision request")
        scene = self.db.get_scene(scene_id)
        current = RetryPromptBundle(
            stage=request.stage,
            start_prompt=request.start_prompt,
            end_prompt=request.end_prompt,
            video_prompt=request.video_prompt,
            negative_prompt=request.negative_prompt,
        )
        if self.settings.is_simulation:
            correction = f" Address this user feedback explicitly: {request.feedback.strip()}"
            return current.model_copy(update={
                "start_prompt": (
                    f"{current.start_prompt}{correction}" if current.start_prompt else None
                ),
                "end_prompt": (
                    f"{current.end_prompt}{correction}" if current.end_prompt else None
                ),
                "video_prompt": (
                    f"{current.video_prompt}{correction}" if current.video_prompt else None
                ),
                "rationale": "Simulation revision appended the requested correction.",
            })
        reports = [
            report["report"] for report in self.db.list_qa_reports(scene.project_id)
            if report["scene_id"] == scene.id
        ]
        latest_reasons = (reports[-1].get("reasons", [])[:8] if reports else [])
        spec = scene.spec
        scene_context = {
            "narrative_purpose": spec.narrative_purpose,
            "start_composition": spec.start_composition,
            "end_composition": spec.end_composition,
            "motion": spec.motion,
            "camera": spec.camera,
            "characters_present": spec.characters_present,
            "environment_keys": [
                key for key in (
                    spec.opening_environment_key,
                    spec.closing_environment_key,
                    spec.environment_key,
                ) if key
            ],
            "important_props": spec.important_props,
            "opening_state": spec.opening_state,
            "closing_state": spec.closing_state,
            "object_owner": spec.object_owner,
        }
        project = self.db.get_project(scene.project_id)
        if stage == "video":
            output_type = VideoRetryPromptProposal
            system = (
                "Revise only the supplied Wan video prompt and negative prompt using the user's "
                "feedback. The stage is already selected; do not discuss or choose another stage. "
                "Preserve the story beat, named characters, identity, wardrobe, object ownership, "
                "environment locks, required visual style, start composition and end composition. "
                "Do not introduce new characters, actions, props, environments or facts. Prefer the "
                "smallest concrete edit that fixes the reported problem. Keep video_prompt under "
                "220 words. Keep negative_prompt under 60 unique comma-separated terms. Never repeat "
                "a term. Never put a required style, character, prop, environment or requested action "
                "in negative_prompt. Return stage, video_prompt, negative_prompt and a rationale under "
                "60 words. Return JSON only."
            )
            prompt_context = {
                "required_visual_style": project.visual_style,
                "scene": scene_context,
                "current_video_prompt": current.video_prompt,
                "current_negative_prompt": self._sanitize_negative_prompt(
                    current.negative_prompt, current.video_prompt or project.visual_style
                ),
                "user_feedback": request.feedback,
                "relevant_review_reasons": latest_reasons,
            }
        else:
            output_type = KeyframeRetryPromptProposal
            system = (
                "Revise only the supplied Flux keyframe prompts and negative prompt using the user's "
                "feedback. The stage is already selected; do not discuss or choose another stage. "
                "Preserve the story beat, named characters, identity, wardrobe, object ownership, "
                "environment locks and required visual style. Do not introduce new characters, props, "
                "environments or facts. Keep each prompt under 220 words. Keep negative_prompt under "
                "60 unique comma-separated terms. Never repeat a term or negate required content. "
                "If current_start_prompt is null, return start_prompt as null. Otherwise change it only "
                "when the feedback requires it. Return stage, start_prompt, end_prompt, negative_prompt "
                "and a rationale under 60 words. Return JSON only."
            )
            positive = " ".join(filter(None, (current.start_prompt, current.end_prompt)))
            prompt_context = {
                "required_visual_style": project.visual_style,
                "scene": scene_context,
                "current_start_prompt": current.start_prompt,
                "current_end_prompt": current.end_prompt,
                "current_negative_prompt": self._sanitize_negative_prompt(
                    current.negative_prompt, positive or project.visual_style
                ),
                "user_feedback": request.feedback,
                "relevant_review_reasons": latest_reasons,
            }
        with self.gpu.qwen():
            proposal = self.model.generate_structured(
                output_type,
                system=system,
                context=json.dumps(prompt_context),
                max_tokens=1200,
            )
        if proposal.stage != stage:
            raise ValueError("Qwen returned a prompt revision for the wrong stage")
        data = proposal.model_dump()
        positive_text = " ".join(filter(None, (
            data.get("start_prompt"),
            data.get("end_prompt"),
            data.get("video_prompt"),
            project.visual_style,
        )))
        data["negative_prompt"] = self._sanitize_negative_prompt(
            data.get("negative_prompt", ""), positive_text
        )
        if stage == "keyframe" and request.start_prompt is None:
            data["start_prompt"] = None
        return RetryPromptBundle(**data)

    @staticmethod
    def _sanitize_negative_prompt(
        negative_prompt: str, required_positive_text: str, limit: int = 60
    ) -> str:
        required = required_positive_text.casefold()
        unique: list[str] = []
        seen: set[str] = set()
        for raw in negative_prompt.split(","):
            term = " ".join(raw.split()).strip(" .")
            key = term.casefold()
            if not key or key in seen:
                continue
            if len(key) >= 3 and key in required:
                continue
            seen.add(key)
            unique.append(term[:120])
            if len(unique) >= limit:
                break
        return ", ".join(unique)

    def _reopen_scene_work(self, project_id: str) -> None:
        project = self.db.get_project(project_id)
        if project.state in {ProjectState.RENDERING, ProjectState.COMPLETE}:
            raise ValueError("generated stages cannot be retried while or after the final render")
        if project.state == ProjectState.FINAL_REVIEW:
            self.db.transition_project(
                project_id, ProjectState.SCENES, "an approved scene was reopened"
            )

    def regenerate_keyframes(
        self, scene_id: str, retry: RetryRequest | None = None
    ) -> Job:
        scene = self.db.get_scene(scene_id)
        if scene.state not in {
            SceneState.KEYFRAMES_READY, SceneState.KEYFRAMES_APPROVED,
            SceneState.VIDEO_QUEUED, SceneState.VIDEO_READY,
            SceneState.QA_FAILED, SceneState.APPROVED,
        }:
            raise ValueError("scene keyframes are not ready to regenerate")
        self._reopen_scene_work(scene.project_id)
        _affected, interrupted = self.db.invalidate_keyframe_chain(scene_id)
        if interrupted:
            self.gpu.generator.interrupt()
        return self.queue_keyframes(scene_id, retry)

    def approve_keyframes(self, scene_id: str, selection: ArtifactSelection, request: ApprovalRequest) -> Scene:
        scene = self.db.get_scene(scene_id)
        if scene.state != SceneState.KEYFRAMES_READY: raise ValueError("keyframes are not ready")
        if not request.approved:
            self.db.record_approval(scene.project_id, "keyframes", False, request.notes, scene.id)
            self.queue_keyframes(scene.id)
            return self.db.get_scene(scene.id)
        if not selection.start_artifact_id or not selection.end_artifact_id:
            raise ValueError("select start and end keyframes")
        selected_start = self.db.get_artifact(selection.start_artifact_id)
        selected_end = self.db.get_artifact(selection.end_artifact_id)
        if selected_start.id == selected_end.id:
            raise ValueError("start and end keyframes must be different artifacts")
        if selected_start.metadata.get("role") != "start":
            raise ValueError("selected start artifact is not a start keyframe")
        if selected_end.metadata.get("role") != "end":
            raise ValueError("selected end artifact is not an end keyframe")
        start_candidate = selected_start.metadata.get("candidate")
        end_candidate = selected_end.metadata.get("candidate")
        if start_candidate != end_candidate:
            raise ValueError("select start and end keyframes from the same candidate")
        if not scene.spec.starts_new_continuity:
            scenes = self.db.list_scenes(scene.project_id)
            index = next(i for i, item in enumerate(scenes) if item.id == scene.id)
            previous = scenes[index - 1]
            if previous.spec.continuity_group != scene.spec.continuity_group:
                raise ValueError("continuation scene does not follow its continuity group")
            if previous.state != SceneState.KEYFRAMES_APPROVED or not previous.selected_end_artifact_id:
                raise ValueError("approve the previous connected scene first")
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
        self._reopen_scene_work(scene.project_id)
        project = self.db.get_project(scene.project_id)
        active = self._active(scene_id, JobKind.VIDEO)
        if active: return active
        if not scene.selected_start_artifact_id or not scene.selected_end_artifact_id:
            raise ValueError("approved keyframes required")
        start = self.db.get_artifact(scene.selected_start_artifact_id)
        end = self.db.get_artifact(scene.selected_end_artifact_id)
        if start.metadata.get("role") != "start" or end.metadata.get("role") != "end":
            raise ValueError("approved keyframe roles are invalid; select start and end again")
        if start.id == end.id:
            raise ValueError("approved start and end keyframes are the same artifact")
        if scene.state != SceneState.VIDEO_QUEUED:
            scene = self.db.transition_scene(scene.id, SceneState.VIDEO_QUEUED)
        prior = [j for j in self.db.list_jobs(scene.project_id) if j.scene_id == scene.id and j.kind == JobKind.VIDEO]
        ordinal = len(prior) + 1
        payload = {"scene_id": scene.id, "scene_revision": scene.revision, "spec": scene.spec.model_dump(),
          "visual_style": project.visual_style,
          "start_artifact_id": scene.selected_start_artifact_id, "end_artifact_id": scene.selected_end_artifact_id,
          "keyframe_binding": {
              "start": {"artifact_id": start.id, "sha256": start.sha256, "role": "start", "path": start.path},
              "end": {"artifact_id": end.id, "sha256": end.sha256, "role": "end", "path": end.path},
              "identical_content": start.sha256 == end.sha256,
          },
          "attempt": ordinal, "duration_seconds": scene.spec.duration_seconds, "width": 384, "height": 672,
          "fps": 16, "frames": 49, "template": "wan14b_lightx2v_flf2v_api",
          "workflow_profile": "wan14b-lightx2v-4step-unqualified",
          "prompt_override": retry.changed_prompt if retry else None,
          "negative_prompt_override": retry.changed_negative_prompt if retry else None,
          "retry_feedback": retry.feedback if retry else "",
          "revision_rationale": retry.revision_rationale if retry else "",
          "new_seed": retry.new_seed if retry else True}
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
