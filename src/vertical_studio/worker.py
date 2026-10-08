from __future__ import annotations
import random, socket, threading
from pathlib import Path
from .config import Settings
from .db import Database
from .platform import PlatformAdapter
from .schemas import ArtifactKind, Job, JobKind, ProjectState, SceneState
from .services.comfy import GenerationError, Generator
from .services.media import probe_media, sha256_file
from .services.qa import QualityService
from .services.renderer import Renderer

class Worker:
    def __init__(self, db: Database, settings: Settings, generator: Generator,
                 renderer: Renderer | None = None, qa: QualityService | None = None):
        self.db, self.settings, self.generator = db, settings, generator
        self.renderer, self.qa = renderer or Renderer(), qa or QualityService()
        self.owner, self._stop = f"{socket.gethostname()}:{id(self)}", threading.Event()

    def run_once(self) -> Job | None:
        job = self.db.claim_next_job(self.owner)
        if not job: return None
        try:
            {JobKind.KEYFRAME: self._keyframes, JobKind.VIDEO: self._video, JobKind.RENDER: self._render}[job.kind](job)
            return self.db.finish_job(job.id)
        except GenerationError as exc:
            if exc.code == "oom": self.generator.free_memory()
            if exc.transient and job.attempts < 2: return self.db.requeue_job(job.id, str(exc))
            self._mark_failed(job, exc.code)
            return self.db.finish_job(job.id, error_code=exc.code, error_message=str(exc))
        except Exception as exc:
            self._mark_failed(job, "internal_error")
            return self.db.finish_job(job.id, error_code="internal_error", error_message=str(exc))

    def run_until_idle(self, max_jobs: int = 100) -> list[Job]:
        completed = []
        for _ in range(max_jobs):
            result = self.run_once()
            if result is None: break
            completed.append(result)
        return completed

    def serve(self, poll_seconds: float = .5) -> None:
        while not self._stop.is_set():
            if self.run_once() is None: self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set(); self.generator.interrupt()

    def _dir(self, job: Job, attempt_id: str) -> Path:
        path = self.settings.projects_dir / job.project_id / "scenes" / (job.scene_id or "project") / "attempts" / attempt_id
        path.mkdir(parents=True, exist_ok=True); return path

    def _keyframes(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        generation_payload = {**job.payload, "seed": seed}
        attempt = self.db.create_attempt(
            job, "flux-klein-4b-v1", seed, generation_payload
        )
        try:
            result = self.generator.generate_keyframes(
                self._dir(job, attempt), generation_payload
            )
            for path in result.files:
                tokens = path.stem.split("-")
                candidate = int(tokens[1]) if len(tokens) > 2 and tokens[1].isdigit() else 1
                role = tokens[-1] if tokens[-1] in {"start", "end"} else "candidate"
                self.db.add_artifact(job.project_id, ArtifactKind.IMAGE, path, sha256_file(path),
                    {**result.metadata, "candidate": candidate, "role": role, "probe": probe_media(path)}, job.scene_id, attempt)
            self.db.finish_attempt(attempt, "succeeded")
            self.db.transition_scene(job.scene_id, SceneState.KEYFRAMES_READY)
            scenes = self.db.list_scenes(job.project_id)
            if all(s.state == SceneState.KEYFRAMES_READY for s in scenes) and self.db.get_project(job.project_id).state == ProjectState.REFERENCES:
                self.db.transition_project(job.project_id, ProjectState.CONTACT_SHEET_REVIEW)
        except Exception as exc:
            self.db.finish_attempt(attempt, "failed", {"message": str(exc)}); raise

    def _video(self, job: Job) -> None:
        seed = random.randrange(1, 2**31)
        start = self.db.get_artifact(job.payload["start_artifact_id"])
        end = self.db.get_artifact(job.payload["end_artifact_id"])
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
            report = self.qa.inspect_video(
                video,
                directory / "qa-frames",
                context={
                    "scene": scene.spec.model_dump(),
                    "character_bible": self.db.get_character_bible(job.project_id),
                    "criteria": [
                        "prompt adherence",
                        "identity and wardrobe",
                        "environment",
                        "anatomy and unwanted text",
                        "temporal consistency",
                        "start/end keyframe continuity",
                    ],
                },
                reference_images=references,
            )
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
