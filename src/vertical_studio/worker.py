from __future__ import annotations
import logging, random, socket, threading
from pathlib import Path
from typing import Any
from .config import Settings
from .db import Database
from .platform import PlatformAdapter
from .schemas import ArtifactKind, Job, JobKind, JobState, ProjectState, SceneState
from .services.comfy import GenerationError, Generator
from .services.gpu import GpuCoordinator
from .services.model_gateway import ModelUnavailable
from .services.media import extract_frames, probe_media, sha256_file
from .services.qa import QualityService
from .services.reference import ReferenceAnalyzer
from .services.renderer import Renderer

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, db: Database, settings: Settings, generator: Generator,
                 renderer: Renderer | None = None, qa: QualityService | None = None,
                 gpu: GpuCoordinator | None = None):
        self.db, self.settings, self.generator = db, settings, generator
        self.renderer, self.qa = renderer or Renderer(), qa or QualityService()
        self.gpu = gpu or GpuCoordinator(generator, self.qa.model, simulation=True)
        self.owner, self._stop = f"{socket.gethostname()}:{id(self)}", threading.Event()

    def run_once(self) -> Job | None:
        job = self.db.claim_next_job(self.owner)
        if not job: return None
        try:
            {
                JobKind.REFERENCE_ANALYSIS: self._reference_analysis,
                JobKind.CANONICAL_ASSET: self._canonical_asset,
                JobKind.CHARACTER_SHEET: self._character_sheet,
                JobKind.KEYFRAME: self._keyframes,
                JobKind.VIDEO: self._video,
                JobKind.RENDER: self._render,
            }[job.kind](job)
            return self.db.finish_job(job.id)
        except GenerationError as exc:
            if self.db.get_job(job.id).state == JobState.CANCELLED:
                return self.db.get_job(job.id)
            if exc.code == "oom": self.generator.free_memory()
            if exc.transient and job.attempts < 2: return self.db.requeue_job(job.id, str(exc))
            self._mark_failed(job, exc.code)
            return self.db.finish_job(job.id, error_code=exc.code, error_message=str(exc))
        except Exception as exc:
            if self.db.get_job(job.id).state == JobState.CANCELLED:
                return self.db.get_job(job.id)
            self._mark_failed(job, "internal_error")
            return self.db.finish_job(job.id, error_code="internal_error", error_message=str(exc))

    def run_until_idle(self, max_jobs: int = 100) -> list[Job]:
        completed = []
        for _ in range(max_jobs):
            result = self.run_once()
            if result is None: break
            completed.append(result)
        return completed

    def prepare_to_serve(self) -> None:
        self._stop.clear()

    def serve(self, poll_seconds: float = .5) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once() is None:
                    self._stop.wait(poll_seconds)
            except Exception:
                # Claim/reconciliation failures happen outside run_once's per-job
                # exception boundary. Keep the API worker alive and make the cause
                # visible instead of silently stranding all queued jobs.
                logger.exception("background worker loop failed")
                self._stop.wait(max(1.0, poll_seconds))

    def stop(self) -> None:
        self._stop.set(); self.generator.interrupt()
        if self.qa.model is not None:
            self.qa.model.unload()

    def _dir(self, job: Job, attempt_id: str) -> Path:
        path = self.settings.projects_dir / job.project_id / "scenes" / (job.scene_id or "project") / "attempts" / attempt_id
        path.mkdir(parents=True, exist_ok=True); return path

    @staticmethod
    def _latest_candidates(artifacts: list, role: str) -> dict[int, object]:
        result: dict[int, object] = {}
        for artifact in reversed(artifacts):
            candidate = artifact.metadata.get("candidate")
            if artifact.metadata.get("role") == role and isinstance(candidate, int):
                result.setdefault(candidate, artifact)
        return result

    def _reference_analysis(self, job: Job) -> None:
        source = self.db.get_reference_source(job.project_id)
        if source is None or source["id"] != job.payload.get("source_id"):
            raise GenerationError(
                "reference_source_missing",
                "the queued reference source is no longer available",
            )
        project = self.db.get_project(job.project_id)
        round_number = int(job.payload.get("analysis_round", 1))
        evidence_dir = (
            self.settings.projects_dir / job.project_id / "reference" / "evidence"
            / f"analysis-{round_number}"
        )
        frames = extract_frames(
            Path(source["path"]),
            evidence_dir,
            count=int(job.payload.get("frame_count", 8)),
        )
        if self.qa.model is None:
            raise GenerationError(
                "model_unavailable", "reference analysis requires a configured model"
            )
        analyzer = ReferenceAnalyzer(
            self.qa.model, simulation=self.settings.is_simulation
        )
        try:
            if self.settings.is_simulation:
                blueprint, trace = analyzer.analyze(
                    project, frames, source_name=source["original_name"]
                )
            else:
                with self.gpu.qwen():
                    blueprint, trace = analyzer.analyze(
                        project, frames, source_name=source["original_name"]
                    )
        except ModelUnavailable as exc:
            raise GenerationError(
                "model_unavailable", str(exc), transient=True
            ) from exc
        if self.db.get_job(job.id).state == JobState.CANCELLED:
            return
        # Persistence boundary: never save a blueprint that production cannot use.
        blueprint.as_storyboard()
        evidence_ids = []
        for index, frame in enumerate(frames, 1):
            artifact = self.db.add_artifact(
                job.project_id,
                ArtifactKind.IMAGE,
                frame,
                sha256_file(frame),
                {
                    "role": "reference_evidence",
                    "source_id": source["id"],
                    "analysis_round": round_number,
                    "order": index,
                    "sample_count": len(frames),
                },
            )
            evidence_ids.append(artifact.id)
        trace["evidence_artifact_ids"] = evidence_ids
        trace["job_id"] = job.id
        self.db.save_reference_analysis(
            job.project_id,
            source["id"],
            blueprint.model_dump(mode="json"),
            trace,
        )
        self.db.transition_project(
            job.project_id, ProjectState.REFERENCE_REVIEW
        )

    def _canonical_asset(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        payload = {**job.payload, "project_id": job.project_id, "seed": seed}
        attempt = self.db.create_attempt(
            job, "flux-t2i-canonical-reference-v1", seed, payload
        )
        try:
            with self.gpu.comfy(reset_models=False):
                result = self.generator.generate_reference_asset(
                    self._dir(job, attempt), payload
                )
            path = result.files[0]
            artifact = self.db.add_artifact(
                job.project_id, ArtifactKind.IMAGE, path, sha256_file(path),
                {
                    **result.metadata,
                    "role": "canonical_reference",
                    "asset_key": payload["asset_key"],
                    "asset_role": payload["asset_role"],
                    "asset_label": payload["asset_label"],
                    "prompt": payload["prompt"],
                },
                None, attempt,
            )
            self.db.attach_canonical_artifact(
                payload["canonical_asset_id"], artifact.id
            )
            self.db.finish_attempt(attempt, "succeeded")
        except Exception as exc:
            self.db.finish_attempt(attempt, "failed", {"message": str(exc)})
            raise

    def _character_sheet(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        payload = {**job.payload, "project_id": job.project_id, "seed": seed}
        attempt = self.db.create_attempt(job, "flux-t2i-character-sheet-v1", seed, payload)
        try:
            with self.gpu.comfy(reset_models=False):
                result = self.generator.generate_character_sheet(
                    self._dir(job, attempt), payload
                )
            path = result.files[0]
            self.db.add_artifact(
                job.project_id, ArtifactKind.IMAGE, path, sha256_file(path),
                {**result.metadata, "role": "character_sheet"}, None, attempt,
            )
            self.db.finish_attempt(attempt, "succeeded")
        except Exception as exc:
            self.db.finish_attempt(attempt, "failed", {"message": str(exc)})
            raise

    def _keyframes(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        scene = self.db.get_scene(job.scene_id)
        generation_payload = {**job.payload, "seed": seed}
        registered = self.db.list_canonical_assets(job.project_id)
        available = {
            asset.key: asset for asset in registered
            if asset.approved and asset.artifact_id
        }
        opening_environment = (
            scene.spec.opening_environment_key or scene.spec.environment_key
        )
        closing_environment = (
            scene.spec.closing_environment_key or opening_environment
        )
        character_keys = list(scene.spec.characters_present)
        prop_keys = list(scene.spec.important_props)
        explicit_keys = [
            *character_keys,
            *prop_keys,
            *[key for key in (opening_environment, closing_environment) if key],
        ]
        other_keys = [
            key for key in scene.spec.reference_keys if key not in explicit_keys
        ]

        def unique(keys: list[str]) -> list[str]:
            return list(dict.fromkeys(key for key in keys if key))

        if explicit_keys:
            opening_keys = unique([
                *character_keys, opening_environment, *prop_keys, *other_keys,
            ])
            closing_keys = unique(
                ([closing_environment, *character_keys] if closing_environment != opening_environment
                 else [*character_keys, closing_environment])
                + [*prop_keys, *other_keys]
            )
        else:
            fallback = list(scene.spec.reference_keys) or list(available)
            opening_keys = unique(fallback)
            closing_keys = unique(fallback)
        required_keys = set(opening_keys) | set(closing_keys)
        missing = required_keys - set(available)
        if registered and (not required_keys or missing):
            detail = ", ".join(sorted(missing)) or "no references selected"
            raise GenerationError(
                "canonical_references_missing",
                f"scene is missing approved canonical references: {detail}",
            )

        def references(keys: list[str], limit: int) -> list[dict[str, Any]]:
            result = []
            for key in keys[:limit]:
                asset = available[key]
                result.append({
                    "asset_id": asset.id,
                    "artifact_id": asset.artifact_id,
                    "key": asset.key,
                    "role": asset.role,
                    "label": asset.label,
                    "description": asset.description,
                    "path": self.db.get_artifact(asset.artifact_id).path,
                })
            return result

        # User-selected inputs may mix canonical assets with extracted source frames.
        # Opening anchors accept two images. Closing generation reserves Image 1 for
        # the exact opening/continuity frame, leaving one selectable reference slot.
        if "opening_reference_frames" in generation_payload:
            opening_references = list(
                generation_payload.get("opening_reference_frames", [])
            )[:2]
            closing_references = list(
                generation_payload.get("closing_reference_frames", [])
            )[:1]
        else:
            opening_references = references(opening_keys, 1)
            closing_references = references(closing_keys, 1)
        generation_payload["opening_reference_frames"] = opening_references
        generation_payload["closing_reference_frames"] = closing_references
        generation_payload["opening_canonical_references"] = [
            item for item in opening_references
            if item.get("source_type", "canonical_asset") == "canonical_asset"
        ]
        generation_payload["closing_canonical_references"] = [
            item for item in closing_references
            if item.get("source_type", "canonical_asset") == "canonical_asset"
        ]
        generation_payload["canonical_references"] = list({
            reference["artifact_id"]: reference
            for reference in [*opening_references, *closing_references]
            if reference.get("artifact_id")
        }.values())
        if not scene.spec.starts_new_continuity:
            scenes = self.db.list_scenes(job.project_id)
            index = next(i for i, item in enumerate(scenes) if item.id == scene.id)
            if index == 0:
                raise GenerationError("continuity_dependency_missing", "continuation has no previous scene")
            previous = scenes[index - 1]
            if previous.spec.continuity_group != scene.spec.continuity_group:
                raise GenerationError("continuity_dependency_missing", "previous scene belongs to another continuity group")
            candidates = int(job.payload.get("candidates", 1))
            if previous.state == SceneState.KEYFRAMES_APPROVED and previous.selected_end_artifact_id:
                approved_end = self.db.get_artifact(previous.selected_end_artifact_id)
                generation_payload["source_frames"] = [
                    {"candidate": candidate, "artifact_id": approved_end.id,
                     "path": approved_end.path, "sha256": approved_end.sha256}
                    for candidate in range(1, candidates + 1)
                ]
            else:
                previous_ends = self._latest_candidates(
                    self.db.list_artifacts(job.project_id, previous.id), "end"
                )
                if len(previous_ends) < candidates:
                    raise GenerationError(
                        "continuity_dependency_missing",
                        "previous end frames are unavailable",
                        transient=True,
                    )
                generation_payload["source_frames"] = [
                    {"candidate": candidate, "artifact_id": previous_ends[candidate].id,
                     "path": previous_ends[candidate].path, "sha256": previous_ends[candidate].sha256}
                    for candidate in range(1, candidates + 1)
                ]
        attempt = self.db.create_attempt(
            job, "flux-klein-edit-two-reference-v1", seed, generation_payload
        )
        try:
            with self.gpu.comfy(reset_models=False):
                result = self.generator.generate_keyframes(
                    self._dir(job, attempt), generation_payload
                )
            if self.db.get_job(job.id).state == JobState.CANCELLED:
                self.db.finish_attempt(attempt, "cancelled")
                return
            created_starts: dict[int, Any] = {}
            for reference in generation_payload.get("source_frames", []):
                created = self.db.add_artifact(
                    job.project_id, ArtifactKind.IMAGE, Path(reference["path"]),
                    reference["sha256"],
                    {"candidate": reference["candidate"], "role": "start",
                     "generation_method": "reuse_previous_end",
                     "shared_from_artifact_id": reference["artifact_id"],
                     "input_references": [{
                         "artifact_id": reference["artifact_id"],
                         "key": "previous_scene_end",
                         "role": "continuity",
                         "label": "Exact previous scene end frame",
                         "source_type": "continuity_frame",
                     }],
                     "continuity_group": scene.spec.continuity_group},
                    job.scene_id, attempt,
                )
                created_starts[reference["candidate"]] = created
            for path in result.files:
                tokens = path.stem.split("-")
                candidate = int(tokens[1]) if len(tokens) > 2 and tokens[1].isdigit() else 1
                role = tokens[-1] if tokens[-1] in {"start", "end"} else "candidate"
                selected_inputs = (
                    generation_payload.get("opening_reference_frames", [])
                    if role == "start"
                    else generation_payload.get("closing_reference_frames", [])
                )
                if selected_inputs:
                    method = (
                        "flux_klein_edit_reference_anchor"
                        if role == "start"
                        else "flux_klein_edit_reference_transition"
                    )
                else:
                    method = "t2i_anchor" if role == "start" else "flux_edit"
                input_references = [
                    {key: ref.get(key) for key in (
                        "asset_id", "artifact_id", "key", "role", "label", "source_type"
                    )}
                    for ref in selected_inputs
                ]
                if role == "end" and candidate in created_starts:
                    input_references.insert(0, {
                        "artifact_id": created_starts[candidate].id,
                        "key": "opening_frame",
                        "role": "continuity",
                        "label": "Generated opening keyframe",
                        "source_type": "continuity_frame",
                    })
                created = self.db.add_artifact(
                    job.project_id, ArtifactKind.IMAGE, path, sha256_file(path),
                    {**result.metadata, "candidate": candidate, "role": role,
                     "generation_method": method,
                     "continuity_group": scene.spec.continuity_group,
                     "input_references": input_references,
                     "canonical_references": [
                         ref for ref in input_references
                         if ref.get("source_type") == "canonical_asset"
                     ],
                     "opening_environment_key": opening_environment,
                     "closing_environment_key": closing_environment},
                    job.scene_id, attempt,
                )
                if role == "start":
                    created_starts[candidate] = created
            self.db.finish_attempt(attempt, "succeeded")
            self.db.transition_scene(job.scene_id, SceneState.KEYFRAMES_READY)
            scenes = self.db.list_scenes(job.project_id)
            reviewable = {
                SceneState.KEYFRAMES_READY, SceneState.KEYFRAMES_APPROVED,
                SceneState.VIDEO_QUEUED, SceneState.VIDEO_READY,
                SceneState.QA_FAILED, SceneState.APPROVED,
            }
            if all(s.state in reviewable for s in scenes) and self.db.get_project(job.project_id).state == ProjectState.REFERENCES:
                self.db.transition_project(job.project_id, ProjectState.CONTACT_SHEET_REVIEW)
        except Exception as exc:
            self.db.finish_attempt(attempt, "failed", {"message": str(exc)})
            raise

    def _video(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        start = self.db.get_artifact(job.payload["start_artifact_id"])
        end = self.db.get_artifact(job.payload["end_artifact_id"])
        binding = job.payload.get("keyframe_binding", {})
        for role, artifact in (("start", start), ("end", end)):
            expected = binding.get(role, {})
            if artifact.kind != ArtifactKind.IMAGE or artifact.scene_id != job.scene_id:
                raise GenerationError("keyframe_binding_invalid", f"{role} keyframe is not an image from this scene")
            if artifact.metadata.get("role") != role:
                raise GenerationError("keyframe_binding_invalid", f"{role} keyframe has role {artifact.metadata.get('role')!r}")
            if expected and (
                expected.get("artifact_id") != artifact.id
                or expected.get("sha256") != artifact.sha256
                or expected.get("path") != artifact.path
            ):
                raise GenerationError("keyframe_binding_changed", f"{role} keyframe changed after video was queued")
            path = Path(artifact.path)
            if not path.is_file() or sha256_file(path) != artifact.sha256:
                raise GenerationError("keyframe_file_changed", f"{role} keyframe file is missing or no longer matches its hash")
        if start.id == end.id:
            raise GenerationError("keyframe_binding_invalid", "start and end keyframes are the same artifact")
        generation_payload = {
            **job.payload,
            "seed": seed,
            "start_image_path": start.path,
            "end_image_path": end.path,
        }
        attempt = self.db.create_attempt(
            job, "wan14b-lightx2v-4step-v1", seed, generation_payload
        )
        directory = self._dir(job, attempt)
        try:
            with self.gpu.comfy():
                result = self.generator.generate_video(directory, generation_payload)
            video = result.files[0]
            artifact = self.db.add_artifact(job.project_id, ArtifactKind.VIDEO, video, sha256_file(video),
                {**result.metadata, "workflow_profile": job.payload.get("workflow_profile")}, job.scene_id, attempt)
            scene = self.db.get_scene(job.scene_id)
            references = [
                Path(self.db.get_artifact(artifact_id).path)
                for artifact_id in (
                    scene.selected_start_artifact_id,
                    scene.selected_end_artifact_id,
                )
                if artifact_id
            ]
            review_args = {
                "context": {
                    "scene": scene.spec.model_dump(),
                    "character_bible": {
                        key: value
                        for key, value in self.db.get_character_bible(job.project_id).items()
                        if not key.startswith("_")
                    },
                    "criteria": [
                        "prompt adherence", "identity and wardrobe", "environment",
                        "anatomy and unwanted text", "temporal consistency",
                        "start/end keyframe continuity", "requested visual style fidelity",
                    ],
                },
                "reference_images": references,
            }
            try:
                if self.qa.enable_visual_review:
                    with self.gpu.qwen():
                        report = self.qa.inspect_video(
                            video, directory / "qa-frames", **review_args
                        )
                else:
                    report = self.qa.inspect_video(
                        video, directory / "qa-frames", **review_args
                    )
            except ModelUnavailable as exc:
                report = QualityService().inspect_video(video, directory / "qa-frames")
                report.advisory_pass = False
                report.reasons.append(f"visual review unavailable: {exc}")
            self.db.add_qa_report(job.project_id, job.scene_id, artifact.id, report.model_dump())
            self.db.select_artifacts(job.scene_id, video_id=artifact.id)
            self.db.clear_continuity_stale(job.scene_id)
            self.db.finish_attempt(attempt, "succeeded")
            self.db.transition_scene(job.scene_id, SceneState.VIDEO_READY if report.technical_pass else SceneState.QA_FAILED)
        except Exception as exc:
            self.db.finish_attempt(attempt, "failed", {"message": str(exc)}); raise

    def _render(self, job: Job) -> None:
        artifacts = [self.db.get_artifact(x) for x in job.payload["artifact_ids"] if x]
        if len(artifacts) != len(job.payload["artifact_ids"]): raise GenerationError("missing_artifact", "selected video missing")
        target = PlatformAdapter(self.settings.platform_mode).export_path(
            job.payload.get("export_path"),
            self.settings.export_dir / f"{job.project_id}.mp4",
        )
        manifest = self.renderer.render([Path(x.path) for x in artifacts], target)
        artifact = self.db.add_artifact(job.project_id, ArtifactKind.RENDER, target, sha256_file(target), manifest["probe"])
        self.db.save_render_manifest(job.project_id, manifest, artifact.id)
        self.db.transition_project(job.project_id, ProjectState.COMPLETE)

    def _mark_failed(self, job: Job, reason: str) -> None:
        if job.scene_id:
            try: self.db.transition_scene(job.scene_id, SceneState.FAILED, reason)
            except Exception: pass
