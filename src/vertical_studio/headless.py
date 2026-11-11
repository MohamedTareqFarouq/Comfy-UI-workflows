from __future__ import annotations

import json
import mimetypes
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .capabilities import CapabilityRegistry, RoutingError
from .config import Settings
from .headless_schemas import (
    ApprovalPolicy,
    CancelRequest,
    EvaluationRequest,
    ImageGenerationRequest,
    MediaType,
    OperationState,
    ProductionStartRequest,
    ProductionTaskRequest,
    ReferenceRole,
    RenderRequest,
    VideoGenerationRequest,
)
from .schemas import utc_now
from .services.comfy import ComfyClient, GenerationError
from .services.gpu import GpuCoordinator
from .services.media import analyze_reference_video, probe_media, sha256_file
from .services.mock_comfy import MockComfyClient
from .services.qa import QualityService
from .services.renderer import Renderer


HEADLESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_artifacts (
  id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
  width INTEGER, height INTEGER, metadata_json TEXT NOT NULL, source_ids_json TEXT NOT NULL,
  capability_id TEXT, workflow_version TEXT, prompt TEXT, seed INTEGER,
  settings_json TEXT NOT NULL, production_session_id TEXT, disposition TEXT NOT NULL,
  qa_json TEXT, user_notes TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_artifacts_session ON media_artifacts(production_session_id, created_at);
CREATE TABLE IF NOT EXISTS media_operations (
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, production_session_id TEXT,
  capability_id TEXT NOT NULL, capability_version TEXT NOT NULL, media_type TEXT NOT NULL,
  state TEXT NOT NULL, request_json TEXT NOT NULL, input_artifact_ids_json TEXT NOT NULL,
  reference_roles_json TEXT NOT NULL, resource_class TEXT NOT NULL, queue_position INTEGER NOT NULL,
  progress REAL NOT NULL, result_artifact_ids_json TEXT NOT NULL, failure_json TEXT,
  cancellation_json TEXT, parent_operation_id TEXT, retry_of TEXT, attempt INTEGER NOT NULL,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_operations_queue ON media_operations(state, queue_position);
CREATE INDEX IF NOT EXISTS idx_media_operations_session ON media_operations(production_session_id, created_at);
CREATE TABLE IF NOT EXISTS production_sessions (
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
  goal TEXT NOT NULL, request_json TEXT NOT NULL, interpretation_json TEXT NOT NULL,
  plan_json TEXT NOT NULL, continuity_json TEXT NOT NULL, budgets_json TEXT NOT NULL,
  usage_json TEXT NOT NULL, approval_policy_json TEXT NOT NULL, steering_json TEXT NOT NULL,
  generation_paused INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_tasks (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
  requirements_json TEXT NOT NULL, dependency_ids_json TEXT NOT NULL, artifact_ids_json TEXT NOT NULL,
  handoff_json TEXT NOT NULL, invalidated_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_approvals (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation_spec_hash TEXT NOT NULL,
  operation_spec_json TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS production_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL,
  data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _loads(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


class HeadlessStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(HEADLESS_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def public_operation(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key, default in (
            ("request_json", {}), ("input_artifact_ids_json", []),
            ("reference_roles_json", []), ("result_artifact_ids_json", []),
            ("failure_json", None), ("cancellation_json", None),
        ):
            result[key.removesuffix("_json")] = _loads(result.pop(key), default)
        return result

    @staticmethod
    def public_artifact(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key, default in (
            ("metadata_json", {}), ("source_ids_json", []), ("settings_json", {}), ("qa_json", None),
        ):
            result[key.removesuffix("_json")] = _loads(result.pop(key), default)
        return result

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM media_artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"artifact not found: {artifact_id}")
        return self.public_artifact(row)

    def operation(self, operation_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM media_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError(f"operation not found: {operation_id}")
        return self.public_operation(row)

    def list_operations(self, session_id: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses, values = [], []
        if session_id:
            clauses.append("production_session_id=?"); values.append(session_id)
        if state:
            clauses.append("state=?"); values.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM media_operations{where} ORDER BY created_at DESC", values
            ).fetchall()
        return [self.public_operation(row) for row in rows]


class HeadlessMediaService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = HeadlessStore(settings.db_path)
        self.registry = CapabilityRegistry(
            settings.workflows_dir,
            simulation=settings.is_simulation,
            qualification_report=settings.data_dir / "qualification" / "latest.json",
        )
        self.generator = MockComfyClient() if settings.is_simulation else ComfyClient(settings.comfy_url, settings.workflows_dir)
        self.qa = QualityService(enable_visual_review=False)
        self.gpu = GpuCoordinator(self.generator, None, simulation=settings.is_simulation)
        self.renderer = Renderer()
        self.root = settings.data_dir / "headless"
        self.artifacts_dir = self.root / "artifacts"
        self.operations_dir = self.root / "operations"
        self.exports_dir = self.root / "exports"
        for path in (self.artifacts_dir, self.operations_dir, self.exports_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_operation_id: str | None = None
        self._lock = threading.RLock()
        self.recover()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="headless-media-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set(); self._wake.set()
        if self._active_operation_id:
            self.generator.interrupt()
        if self._thread:
            self._thread.join(timeout=5)

    def recover(self) -> None:
        now = utc_now()
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE media_operations SET state='queued', started_at=NULL, progress=0, updated_at=? WHERE state='running'",
                (now,),
            )

    def _serve(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(0.5); self._wake.clear()

    def _claim(self) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM media_operations WHERE state='queued' ORDER BY queue_position, created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit(); return None
            now = utc_now()
            changed = connection.execute(
                "UPDATE media_operations SET state='running', progress=.01, started_at=?, updated_at=? WHERE id=? AND state='queued'",
                (now, now, row["id"]),
            ).rowcount
            connection.commit()
        return self.store.operation(row["id"]) if changed else None

    def run_once(self) -> bool:
        operation = self._claim()
        if operation is None:
            return False
        with self._lock:
            self._active_operation_id = operation["id"]
        try:
            artifact_ids = self._execute(operation)
            current = self.store.operation(operation["id"])
            if current["state"] == OperationState.CANCELLED:
                return True
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE media_operations SET state='succeeded', progress=1, result_artifact_ids_json=?, finished_at=?, updated_at=? WHERE id=?",
                    (json.dumps(artifact_ids), utc_now(), utc_now(), operation["id"]),
                )
        except Exception as exc:
            current = self.store.operation(operation["id"])
            if current["state"] != OperationState.CANCELLED:
                failure = {
                    "code": getattr(exc, "code", "internal_error"),
                    "message": str(exc),
                    "retryable": bool(getattr(exc, "transient", False)),
                }
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE media_operations SET state='failed', failure_json=?, finished_at=?, updated_at=? WHERE id=?",
                        (json.dumps(failure), utc_now(), utc_now(), operation["id"]),
                    )
        finally:
            with self._lock:
                self._active_operation_id = None
        return True

    def _execute(self, operation: dict[str, Any]) -> list[str]:
        capability = self.registry.require_executable(operation["capability_id"])
        request = operation["request"]
        target = self.operations_dir / operation["id"]
        target.mkdir(parents=True, exist_ok=True)
        if capability.media_type == MediaType.IMAGE:
            return self._execute_image(operation, target)
        if capability.media_type == MediaType.VIDEO:
            return self._execute_video(operation, target)
        if capability.media_type == MediaType.EVALUATION:
            return self._execute_evaluation(operation, target)
        if capability.media_type == MediaType.RENDER:
            return self._execute_render(operation, target)
        if capability.media_type == MediaType.ANALYSIS:
            return self._execute_analysis(operation, target)
        raise GenerationError("unsupported_capability", capability.id)

    def _execute_image(self, operation: dict[str, Any], target: Path) -> list[str]:
        request = operation["request"]
        resolved = []
        for reference in request["references"]:
            artifact = self.store.artifact(reference["artifact_id"])
            resolved.append({**reference, "path": artifact["path"], "sha256": artifact["sha256"]})
        created = []
        for candidate in range(1, int(request["candidate_count"]) + 1):
            payload = {**request, "asset_key": f"candidate-{candidate}", "candidates": 1,
                       "operation_id": operation["id"], "capability_id": operation["capability_id"],
                       "resolved_references": resolved}
            with self.gpu.comfy(reset_models=False):
                result = self.generator.generate_capability_image(target / str(candidate), payload)
            for path in result.files:
                created.append(self._record_generated(
                    operation, path,
                    {**result.metadata, "execution_mode": self.settings.execution_mode,
                     "backend": "mock_simulator" if self.settings.is_simulation else "comfyui",
                     "simulated": self.settings.is_simulation},
                    "generated",
                ))
        return created

    def _execute_video(self, operation: dict[str, Any], target: Path) -> list[str]:
        request = operation["request"]
        references = {item["role"]: self.store.artifact(item["artifact_id"]) for item in request["references"]}
        created = []
        for candidate in range(1, int(request["candidate_count"]) + 1):
            payload = {
                **request, "scene_id": operation["id"], "attempt": candidate,
                "operation_id": operation["id"], "capability_id": operation["capability_id"],
                "duration_seconds": request["frames"] / request["fps"],
                "start_path": references[ReferenceRole.START_FRAME]["path"],
                "end_path": references.get(ReferenceRole.END_FRAME, {}).get("path"),
                "resolved_references": [
                    {**item, "path": references[item["role"]]["path"],
                     "sha256": references[item["role"]]["sha256"]}
                    for item in request["references"]
                ],
            }
            with self.gpu.comfy(reset_models=False):
                result = self.generator.generate_capability_video(target / str(candidate), payload)
            for path in result.files:
                created.append(self._record_generated(
                    operation, path,
                    {**result.metadata, "execution_mode": self.settings.execution_mode,
                     "backend": "mock_simulator" if self.settings.is_simulation else "comfyui",
                     "simulated": self.settings.is_simulation},
                    "generated",
                ))
        return created

    def _execute_analysis(self, operation: dict[str, Any], target: Path) -> list[str]:
        evidence = []
        source_analysis = []
        output_artifact_ids = []
        for artifact_id in operation["input_artifact_ids"]:
            artifact = self.store.artifact(artifact_id)
            evidence.append({"artifact_id": artifact_id, "media_type": artifact["media_type"], "metadata": artifact["metadata"]})
            if artifact["media_type"] == "video":
                analysis = analyze_reference_video(
                    Path(artifact["path"]), target / f"evidence-{artifact_id}", sample_count=6
                )
                frame_artifact_ids = []
                for sample in analysis["sample_frames"]:
                    frame_artifact_id = self._record_generated(
                        operation,
                        Path(sample["path"]),
                        {
                            "role": "reference_evidence",
                            "source_artifact_id": artifact_id,
                            "timestamp_seconds": sample["timestamp_seconds"],
                        },
                        "generated",
                    )
                    frame_artifact_ids.append(frame_artifact_id)
                    output_artifact_ids.append(frame_artifact_id)
                source_analysis.append({
                    "source_artifact_id": artifact_id,
                    **analysis,
                    "evidence_artifact_ids": frame_artifact_ids,
                })
        report = target / "analysis.json"
        report.write_text(json.dumps({
            "entities": [], "style": {}, "environment": {}, "motion": {}, "camera": {},
            "continuity": {}, "canonical_asset_suggestions": [], "evidence": evidence,
            "source_analysis": source_analysis,
            "note": "This report contains measurable media evidence. Use a vision-capable reviewer for semantic identity, action, style, and continuity interpretation; do not treat measurements as fidelity scores.",
        }, indent=2))
        report_id = self._record_generated(operation, report, {"analysis": True}, "generated")
        return [*output_artifact_ids, report_id]

    def _execute_evaluation(self, operation: dict[str, Any], target: Path) -> list[str]:
        reports = []
        for artifact_id in operation["input_artifact_ids"]:
            artifact = self.store.artifact(artifact_id)
            if artifact["media_type"] != "video":
                report = {"technical_pass": Path(artifact["path"]).exists(), "advisory_pass": True,
                          "scores": {"accessible": 1.0}, "reasons": [], "retry_recommendation": None}
            else:
                qa = self.qa.inspect_video(
                    Path(artifact["path"]), target / artifact_id,
                    goal_mode=request.get("goal_mode", "adaptation"),
                )
                report = {**qa.model_dump(), "retry_recommendation": None if qa.technical_pass else "retry_with_changed_settings"}
            reports.append({"artifact_id": artifact_id, **report})
            with self.store.connect() as connection:
                connection.execute("UPDATE media_artifacts SET qa_json=? WHERE id=?", (json.dumps(report), artifact_id))
        path = target / "qa.json"
        path.write_text(json.dumps({"reports": reports}, indent=2))
        return [self._record_generated(operation, path, {"qa": True}, "generated")]

    def _execute_render(self, operation: dict[str, Any], target: Path) -> list[str]:
        artifacts = [self.store.artifact(item) for item in operation["input_artifact_ids"]]
        invalid = [item["id"] for item in artifacts if item["media_type"] != "video" or item["disposition"] != "approved"]
        if invalid:
            raise GenerationError("render_input_rejected", f"render requires approved video artifacts: {invalid}")
        output = self.exports_dir / f"{operation['id']}.mp4"
        metadata = self.renderer.render([Path(item["path"]) for item in artifacts], output)
        return [self._record_generated(operation, output, metadata, "generated")]

    def _record_generated(self, operation: dict[str, Any], path: Path, metadata: dict[str, Any], disposition: str) -> str:
        artifact_id = uuid.uuid4().hex
        media_type = "video" if path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"} else "image"
        if path.suffix.lower() == ".json": media_type = "analysis"
        probe = {}
        if media_type in {"image", "video"}:
            try: probe = probe_media(path)
            except Exception: probe = {}
        stream = next((item for item in probe.get("streams", []) if item.get("codec_type") in {"video"}), {})
        request = operation["request"]
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO media_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, str(path.resolve()), sha256_file(path), media_type,
                 stream.get("width"), stream.get("height"), json.dumps({**metadata, "probe": probe}),
                 json.dumps(operation["input_artifact_ids"]), operation["capability_id"], operation["capability_version"],
                 request.get("prompt"), request.get("seed"), json.dumps(request), operation["production_session_id"],
                 disposition, None, "", utc_now()),
            )
        return artifact_id

    def import_reference(self, source_path: str, role: str, idempotency_key: str,
                         production_session_id: str | None = None) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("reference must be a file")
        digest = sha256_file(source)
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM media_artifacts WHERE json_extract(metadata_json, '$.import_idempotency_key')=?",
                (idempotency_key,),
            ).fetchone()
            if existing: return self.store.public_artifact(existing)
        artifact_id = uuid.uuid4().hex
        suffix = source.suffix.lower()
        media_type = "video" if suffix in {".mp4", ".mov", ".webm", ".mkv", ".m4v"} else "image"
        if media_type == "image" and not (mimetypes.guess_type(source.name)[0] or "").startswith("image/"):
            raise ValueError("unsupported reference file type")
        destination = self.artifacts_dir / f"{artifact_id}{suffix}"
        shutil.copy2(source, destination)
        metadata = probe_media(destination)
        stream = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "video"), {})
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO media_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, str(destination), digest, media_type, stream.get("width"), stream.get("height"),
                 json.dumps({"import_idempotency_key": idempotency_key, "original_name": source.name, "probe": metadata, "role": role}),
                 "[]", None, None, None, None, "{}", production_session_id, "canonical" if role in {"identity", "environment", "prop"} else "imported", None, "", utc_now()),
            )
        return self.store.artifact(artifact_id)

    def _submit(self, capability_id: str, request: dict[str, Any], input_ids: list[str], roles: list[str],
                idempotency_key: str, session_id: str | None, parent: str | None = None, retry_of: str | None = None,
                attempt: int = 1) -> dict[str, Any]:
        capability = self.registry.require_executable(capability_id)
        for artifact_id in input_ids: self.store.artifact(artifact_id)
        with self.store.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM media_operations WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
        if existing:
            previous = _loads(existing["request_json"], {})
            if previous != request:
                raise ValueError("idempotency key was already used with a different request")
            return self.store.public_operation(existing)
        if session_id:
            self._guard_production_submission(session_id, capability, request)
        with self.store.connect() as connection:
            queue = connection.execute("SELECT COALESCE(MAX(queue_position),0)+1 FROM media_operations").fetchone()[0]
            operation_id, now = uuid.uuid4().hex, utc_now()
            connection.execute(
                "INSERT INTO media_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, idempotency_key, session_id, capability.id, capability.version, capability.media_type,
                 OperationState.QUEUED, json.dumps(request), json.dumps(input_ids), json.dumps(roles),
                 capability.resource_class, queue, 0, "[]", None, None, parent, retry_of, attempt, now, None, None, now),
            )
            if session_id and capability.media_type in {MediaType.IMAGE, MediaType.VIDEO}:
                session = connection.execute(
                    "SELECT usage_json FROM production_sessions WHERE id=?", (session_id,)
                ).fetchone()
                usage = _loads(session["usage_json"], {})
                key = "image_operations" if capability.media_type == MediaType.IMAGE else "video_operations"
                usage[key] = int(usage.get(key, 0)) + 1
                if capability.media_type == MediaType.VIDEO:
                    usage["generated_video_seconds"] = float(usage.get("generated_video_seconds", 0)) + (
                        float(request["frames"]) / float(request["fps"]) * int(request["candidate_count"])
                    )
                connection.execute(
                    "UPDATE production_sessions SET usage_json=?, updated_at=? WHERE id=?",
                    (json.dumps(usage), now, session_id),
                )
        self._wake.set()
        return self.store.operation(operation_id)

    def _guard_production_submission(self, session_id: str, capability: Any,
                                     request: dict[str, Any]) -> None:
        with self.store.connect() as connection:
            session = connection.execute(
                "SELECT * FROM production_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"production session not found: {session_id}")
            if session["state"] != "active":
                raise ValueError("production session is paused or stopped")
            if session["generation_paused"] and capability.resource_class.startswith("gpu_"):
                raise ValueError("generation is paused for this production session")
            session_request = _loads(session["request_json"], {})
            if capability.media_type == MediaType.VIDEO:
                reference_ids = set(session_request.get("reference_artifact_ids", []))
                if reference_ids and bool(session_request.get("reference_analysis_required", False)):
                    analyses = connection.execute(
                        "SELECT input_artifact_ids_json FROM media_operations WHERE production_session_id=? AND capability_id=? AND state='succeeded'",
                        (session_id, "qwen.reference_analysis.v1"),
                    ).fetchall()
                    if not any(reference_ids <= set(_loads(row["input_artifact_ids_json"], [])) for row in analyses):
                        raise ValueError("reference analysis is required before video generation; complete the evidence pass first")
                plan_tasks = connection.execute(
                    "SELECT status,handoff_json FROM production_tasks WHERE session_id=? AND role='plan'",
                    (session_id,),
                ).fetchall()
                if plan_tasks and not any(
                    row["status"] == "completed" and _loads(row["handoff_json"], {}).get("approved") is True
                    for row in plan_tasks
                ):
                    raise ValueError("storyboard approval is required before video generation")
                for item in request.get("references", []):
                    artifact_id = item.get("artifact_id")
                    if not artifact_id:
                        continue
                    artifact = self.store.artifact(artifact_id)
                    if artifact["disposition"] != "approved":
                        raise ValueError(
                            f"video generation requires approved anchors; artifact {artifact_id} is not approved"
                        )
            budgets = _loads(session["budgets_json"], {})
            usage = _loads(session["usage_json"], {})
            if capability.media_type == MediaType.IMAGE:
                if int(usage.get("image_operations", 0)) >= int(budgets["max_image_operations"]):
                    raise ValueError("maximum image-operation budget reached; explicit budget expansion is required")
            if capability.media_type == MediaType.VIDEO:
                if int(usage.get("video_operations", 0)) >= int(budgets["max_video_operations"]):
                    raise ValueError("maximum video-operation budget reached; explicit budget expansion is required")
                seconds = float(request["frames"]) / float(request["fps"]) * int(request["candidate_count"])
                if float(usage.get("generated_video_seconds", 0)) + seconds > float(budgets["max_generated_video_seconds"]):
                    raise ValueError("generated-video-seconds budget reached; explicit budget expansion is required")
            if int(request.get("candidate_count", 1)) > int(budgets["max_candidates_per_operation"]):
                raise ValueError("candidate count exceeds the production-session budget")

            policy = _loads(session["approval_policy_json"], {})
            preset = policy.get("preset", "approve_first_heavy")
            heavy = capability.resource_class in {"gpu_image", "gpu_video", "cpu_render"}
            needs_approval = preset == "approve_every_heavy" and heavy
            if preset == "approve_first_heavy" and heavy:
                earlier = connection.execute(
                    "SELECT COUNT(*) FROM media_operations WHERE production_session_id=? AND resource_class=?",
                    (session_id, capability.resource_class),
                ).fetchone()[0]
                needs_approval = earlier == 0
            if preset == "custom" and heavy:
                needs_approval = any(
                    bool(rule.get("require_approval", True))
                    for rule in policy.get("rules", [])
                    if (not rule.get("capability_id") or rule["capability_id"] == capability.id)
                    and (not rule.get("media_type") or rule["media_type"] == capability.media_type)
                )
            if not needs_approval:
                return
            spec = {"capability_id": capability.id, "request": request}
            digest = __import__("hashlib").sha256(
                json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            approval = connection.execute(
                "SELECT id FROM production_approvals WHERE session_id=? AND operation_spec_hash=? AND status='approved' ORDER BY decided_at DESC LIMIT 1",
                (session_id, digest),
            ).fetchone()
            if approval is None:
                raise ValueError(
                    "this operation requires approval bound to the exact capability and request; "
                    "call production_request_approval first"
                )
            connection.execute(
                "UPDATE production_approvals SET status='consumed' WHERE id=?", (approval["id"],)
            )

    def generate_image(self, request: ImageGenerationRequest) -> dict[str, Any]:
        capability = self.registry.route_image(request.references, request.preferred_capability)
        payload = request.model_dump(mode="json")
        return self._submit(capability.id, payload, [x.artifact_id for x in request.references],
                            [x.role for x in request.references], request.idempotency_key, request.production_session_id)

    def generate_video(self, request: VideoGenerationRequest) -> dict[str, Any]:
        capability = self.registry.route_video(request.references, request.preferred_capability)
        payload = request.model_dump(mode="json")
        return self._submit(capability.id, payload, [x.artifact_id for x in request.references],
                            [x.role for x in request.references], request.idempotency_key, request.production_session_id)

    def analyze(self, artifact_ids: list[str], idempotency_key: str, session_id: str | None = None) -> dict[str, Any]:
        return self._submit("qwen.reference_analysis.v1", {"artifact_ids": artifact_ids}, artifact_ids, [], idempotency_key, session_id)

    def evaluate(self, request: EvaluationRequest) -> dict[str, Any]:
        return self._submit("media.visual_qa.v1", request.model_dump(mode="json"), request.artifact_ids, [],
                            request.idempotency_key, request.production_session_id)

    def render(self, request: RenderRequest) -> dict[str, Any]:
        return self._submit("ffmpeg.sequence_render.v1", request.model_dump(mode="json"), request.artifact_ids, [],
                            request.idempotency_key, request.production_session_id)

    def retry(self, operation_id: str, idempotency_key: str) -> dict[str, Any]:
        original = self.store.operation(operation_id)
        if original["state"] not in {OperationState.FAILED, OperationState.CANCELLED}:
            raise ValueError("only failed or cancelled operations can be retried")
        return self._submit(original["capability_id"], original["request"], original["input_artifact_ids"],
                            original["reference_roles"], idempotency_key, original["production_session_id"],
                            parent=operation_id, retry_of=original.get("retry_of") or operation_id,
                            attempt=int(original["attempt"]) + 1)

    def cancel_operation(self, operation_id: str, reason: str, actor: str = "user") -> dict[str, Any]:
        operation = self.store.operation(operation_id)
        if operation["state"] in {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.CANCELLED}:
            return operation
        cancellation = {"reason": reason, "actor": actor, "at": utc_now()}
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE media_operations SET state='cancelled', cancellation_json=?, finished_at=?, updated_at=? WHERE id=?",
                (json.dumps(cancellation), utc_now(), utc_now(), operation_id),
            )
        if self._active_operation_id == operation_id:
            self.generator.interrupt()
        return self.store.operation(operation_id)


class ProductionService:
    STAGES = ("analyze", "plan", "asset", "image", "video", "qa", "repair", "render", "finish")

    def __init__(self, media: HeadlessMediaService | None = None, *, store: HeadlessStore | None = None):
        if media is None and store is None:
            raise ValueError("media or store is required")
        self.media = media
        self.store = store or media.store

    def start(self, request: ProductionStartRequest) -> dict[str, Any]:
        for artifact_id in request.reference_artifact_ids: self.store.artifact(artifact_id)
        with self.store.connect() as connection:
            existing = connection.execute("SELECT id FROM production_sessions WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
            if existing: return self.inspect(existing["id"])
            session_id, now = uuid.uuid4().hex, utc_now()
            connection.execute(
                "INSERT INTO production_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, request.idempotency_key, "active", request.goal, request.model_dump_json(), "{}", "{}", "{}",
                 request.budgets.model_dump_json(), json.dumps({"image_operations": 0, "video_operations": 0, "generated_video_seconds": 0}),
                 request.approval_policy.model_dump_json(), "[]", 0, now, now),
            )
            task_id = uuid.uuid4().hex
            requirements = {
                "goal": request.goal, "user_prompt": request.user_prompt,
                "objective": "Inspect the request and choose the smallest sufficient next stage.",
                "acceptance_criteria": ["A concrete next stage is selected or the request is complete."],
                "instructions": self._instructions("controller", request.control_mode.value),
                "idempotency_key": f"controller-{request.idempotency_key}",
            }
            connection.execute(
                "INSERT INTO production_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, session_id, "controller", "ready", json.dumps(requirements),
                 "[]", "[]", "{}", None, now, now),
            )
            self._event(connection, session_id, "session_started", {"request": request.model_dump(mode="json")})
        return self.inspect(session_id)

    @staticmethod
    def _instructions(role: str, control_mode: str = "HITL") -> list[str]:
        common = [
            "Use only registered atomic media tools; never submit workflow JSON or node patches.",
            "Generated outputs do not become canonical without explicit user approval.",
            "Do not repeat an unchanged request after the same failure.",
        ]
        mode_rules = (
            [
                "Control mode is HITL: pause at storyboard, canonical-asset, and keyframe gates and wait for explicit user approval before dependent costly work.",
                "Never represent an agent recommendation or task completion as user approval.",
            ]
            if control_mode == "HITL"
            else [
                "Control mode is Autopilot: advance without routine pauses only when every stated acceptance criterion is verified by evidence.",
                "If acceptance is subjective, evidence is missing, QA fails, or a hard gate is uncertain, pause and request user direction instead of self-approving.",
                "Autopilot does not bypass reference analysis, accepted storyboard/anchors, QA rejection, capability readiness, or budgets.",
            ]
        )
        specific = {
            "controller": [
                "Inspect the situation, skip unnecessary stages, and create only the smallest sufficient next task.",
                "When a reference exists, require an evidence-based analysis pass before storyboard or video review can be approved.",
            ],
            "analyze": [
                "Extract typed evidence and recommend, but do not promote, canonical assets.",
                "Ground every continuity, style, and identity claim in reference evidence before it is used for a plan or approval gate.",
            ],
            "plan": ["Define beats, shots, continuity facts, requirements, and acceptance criteria.", "Compare each scene to the reference evidence and flag identity, direction, composition, prop, and style drift before approval."],
            "asset": ["Propose canonical identity, environment, and prop assets and stop dependents if rejected.", "Use the reference evidence pack as the acceptance baseline for any canonical anchor."],
            "image": ["Use T2I for no reference, single-reference for one dominant input, and multi-reference to combine inputs.", "Do not queue expensive video generation until the required storyboard and anchors are accepted."],
            "video": ["Prefer I2V from an approved start; use FLF2V only when the destination state matters.", "Require accepted start/end anchors and continuity checks before a new video is generated."],
            "qa": ["Evaluate declared requirements and recommend a materially changed retry or stop.", "For exact replica, require evidence-backed threshold checks and route uncalibrated or low-scoring outputs to human review."],
            "repair": ["Diagnose the failed requirement, preserve unaffected artifacts, and repair the smallest dependency.", "Use the failed dimensions and evidence references to repair only the cause of the drift."],
            "render": ["Select approved clips explicitly and render in the declared order."],
            "finish": ["Confirm the requested deliverable exists and summarize its artifact ID."],
        }
        return common + mode_rules + specific[role]

    @staticmethod
    def _event(connection: sqlite3.Connection, session_id: str, event_type: str, data: dict[str, Any]) -> None:
        connection.execute("INSERT INTO production_events(session_id,event_type,data_json,created_at) VALUES(?,?,?,?)",
                           (session_id, event_type, json.dumps(data), utc_now()))

    def inspect(self, session_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            session = connection.execute("SELECT * FROM production_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None: raise KeyError(f"production session not found: {session_id}")
            tasks = [dict(row) for row in connection.execute("SELECT * FROM production_tasks WHERE session_id=? ORDER BY created_at", (session_id,))]
            approvals = [dict(row) for row in connection.execute("SELECT * FROM production_approvals WHERE session_id=? ORDER BY created_at", (session_id,))]
            events = [dict(row) for row in connection.execute("SELECT * FROM production_events WHERE session_id=? ORDER BY id", (session_id,))]
        result = dict(session)
        for key, default in (("request_json", {}), ("interpretation_json", {}), ("plan_json", {}), ("continuity_json", {}),
                             ("budgets_json", {}), ("usage_json", {}), ("approval_policy_json", {}), ("steering_json", [])):
            result[key.removesuffix("_json")] = _loads(result.pop(key), default)
        for task in tasks:
            for key, default in (("requirements_json", {}), ("dependency_ids_json", []), ("artifact_ids_json", []), ("handoff_json", {})):
                task[key.removesuffix("_json")] = _loads(task.pop(key), default)
        for approval in approvals:
            approval["operation_spec"] = _loads(approval.pop("operation_spec_json"), {})
        for event in events: event["data"] = _loads(event.pop("data_json"), {})
        return {**result, "tasks": tasks, "approvals": approvals, "events": events,
                "operations": self.store.list_operations(session_id=session_id)}

    def situation(self, session_id: str) -> dict[str, Any]:
        """Return compact authoritative context intended for a small controller model."""
        full = self.inspect(session_id)
        active_tasks = [task for task in full["tasks"] if task["status"] in {"ready", "running", "pending"}]
        operations = full["operations"]
        pending_approvals = [item for item in full["approvals"] if item["status"] == "pending"]
        allowed = ["inspect_capabilities", "create_task", "update_task", "steer", "pause", "cancel"]
        if full["state"] == "active" and not full["generation_paused"]:
            allowed += ["import_reference", "analyze", "generate_image", "generate_video", "evaluate", "render"]
        if pending_approvals:
            allowed += ["approve", "reject"]
        return {
            "session_id": session_id, "state": full["state"], "goal": full["goal"],
            "request": full["request"], "usage": full["usage"], "budgets": full["budgets"],
            "approval_policy": full["approval_policy"], "active_tasks": active_tasks,
            "running_operations": [item for item in operations if item["state"] in {"queued", "running"}],
            "recent_completed_operations": [item for item in operations if item["state"] == "succeeded"][-8:],
            "recent_failed_operations": [item for item in operations if item["state"] in {"failed", "cancelled"}][-5:],
            "pending_approvals": pending_approvals, "recent_steering": full["steering"][-5:],
            "allowed_actions": allowed,
            "controller_rule": "Choose one smallest sufficient next action; do not replay an unchanged failed request.",
        }

    def create_task(self, session_id: str, request: ProductionTaskRequest) -> dict[str, Any]:
        for artifact_id in request.input_artifact_ids:
            self.store.artifact(artifact_id)
        with self.store.connect() as connection:
            session = connection.execute(
                "SELECT request_json FROM production_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError("production session not found")
            control_mode = _loads(session["request_json"], {}).get("control_mode", "HITL")
            existing = connection.execute(
                "SELECT id FROM production_tasks WHERE session_id=? AND json_extract(requirements_json, '$.idempotency_key')=?",
                (session_id, request.idempotency_key),
            ).fetchone()
            if existing:
                return self.situation(session_id)
            known = {row[0] for row in connection.execute("SELECT id FROM production_tasks WHERE session_id=?", (session_id,))}
            if not set(request.dependency_task_ids) <= known:
                raise ValueError("every dependency must be a task in this production session")
            completed = {row[0] for row in connection.execute(
                "SELECT id FROM production_tasks WHERE session_id=? AND status='completed'", (session_id,)
            )}
            status = "ready" if set(request.dependency_task_ids) <= completed else "pending"
            task_id, now = uuid.uuid4().hex, utc_now()
            requirements = {
                "objective": request.objective, "acceptance_criteria": request.acceptance_criteria,
                "input_artifact_ids": request.input_artifact_ids,
                "instructions": self._instructions(request.stage, control_mode), "idempotency_key": request.idempotency_key,
            }
            connection.execute(
                "INSERT INTO production_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, session_id, request.stage, status, json.dumps(requirements),
                 json.dumps(request.dependency_task_ids), json.dumps(request.input_artifact_ids), "{}", None, now, now),
            )
            self._event(connection, session_id, "task_created", {"task_id": task_id, "stage": request.stage})
        return self.situation(session_id)

    def invalidate_task(self, session_id: str, task_id: str, reason: str) -> dict[str, Any]:
        """Invalidate one task and every transitive dependent while preserving unrelated work."""
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM production_tasks WHERE session_id=?", (session_id,)).fetchall()
            tasks = {row["id"]: row for row in rows}
            if task_id not in tasks:
                raise KeyError("production task not found")
            invalid = {task_id}
            changed = True
            while changed:
                changed = False
                for candidate in rows:
                    if candidate["id"] not in invalid and set(_loads(candidate["dependency_ids_json"], [])) & invalid:
                        invalid.add(candidate["id"])
                        changed = True
            artifact_ids: set[str] = set()
            for invalid_id in invalid:
                artifact_ids.update(_loads(tasks[invalid_id]["artifact_ids_json"], []))
                connection.execute(
                    "UPDATE production_tasks SET status='invalidated', invalidated_reason=?, updated_at=? WHERE id=?",
                    (reason, utc_now(), invalid_id),
                )
            for artifact_id in artifact_ids:
                connection.execute(
                    "UPDATE media_artifacts SET disposition='superseded', user_notes=? WHERE id=?", (reason, artifact_id)
                )
            self._event(connection, session_id, "tasks_invalidated", {
                "root_task_id": task_id, "task_ids": sorted(invalid),
                "artifact_ids": sorted(artifact_ids), "reason": reason,
            })
        return self.situation(session_id)


    def update_task(self, session_id: str, task_id: str, status: str, handoff: dict[str, Any], artifact_ids: list[str],
                    invalidated_reason: str | None = None) -> dict[str, Any]:
        if status not in {"pending", "ready", "running", "completed", "rejected", "invalidated", "skipped"}:
            raise ValueError("invalid task status")
        for artifact_id in artifact_ids: self.store.artifact(artifact_id)
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM production_tasks WHERE id=? AND session_id=?", (task_id, session_id)).fetchone()
            if row is None: raise KeyError("production task not found")
            connection.execute(
                "UPDATE production_tasks SET status=?, handoff_json=?, artifact_ids_json=?, invalidated_reason=?, updated_at=? WHERE id=?",
                (status, json.dumps(handoff), json.dumps(artifact_ids), invalidated_reason, utc_now(), task_id),
            )
            if status == "completed":
                candidates = connection.execute("SELECT * FROM production_tasks WHERE session_id=? AND status='pending'", (session_id,)).fetchall()
                completed = {r[0] for r in connection.execute("SELECT id FROM production_tasks WHERE session_id=? AND status='completed'", (session_id,))}
                for candidate in candidates:
                    if set(_loads(candidate["dependency_ids_json"], [])) <= completed:
                        connection.execute("UPDATE production_tasks SET status='ready', updated_at=? WHERE id=?", (utc_now(), candidate["id"]))
            self._event(connection, session_id, "task_updated", {"task_id": task_id, "status": status, "handoff": handoff})
        return self.inspect(session_id)

    def set_policy(self, session_id: str, policy: ApprovalPolicy) -> dict[str, Any]:
        with self.store.connect() as connection:
            changed = connection.execute("UPDATE production_sessions SET approval_policy_json=?, updated_at=? WHERE id=?",
                                         (policy.model_dump_json(), utc_now(), session_id)).rowcount
            if not changed: raise KeyError("production session not found")
            self._event(connection, session_id, "approval_policy_changed", policy.model_dump(mode="json"))
        return self.inspect(session_id)

    def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return self.inspect(session_id)["approvals"]

    def decide_approval(self, session_id: str, approval_id: str, approved: bool, reason: str) -> dict[str, Any]:
        target_status = "approved" if approved else "rejected"
        with self.store.connect() as connection:
            approval = connection.execute(
                "SELECT session_id, status FROM production_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if approval is None:
                raise KeyError(f"approval not found: {approval_id}")
            if approval["session_id"] != session_id:
                raise ValueError("approval does not belong to production session")
            if approval["status"] == target_status:
                return self.inspect(session_id)
            if approval["status"] != "pending":
                raise ValueError(
                    f"approval is already {approval['status']}; it cannot be changed to {target_status}"
                )
            changed = connection.execute(
                "UPDATE production_approvals SET status=?, reason=?, decided_at=? WHERE id=? AND session_id=? AND status='pending'",
                (target_status, reason, utc_now(), approval_id, session_id),
            ).rowcount
            if not changed:
                current = connection.execute(
                    "SELECT status FROM production_approvals WHERE id=?", (approval_id,)
                ).fetchone()
                if current is None:
                    raise KeyError(f"approval not found: {approval_id}")
                if current["status"] != target_status:
                    raise ValueError(f"approval decision raced with another update; current status is {current['status']}")
            self._event(connection, session_id, "approval_decided", {"approval_id": approval_id, "approved": approved, "reason": reason})
        return self.inspect(session_id)

    def decide_approval_by_id(self, approval_id: str, approved: bool, reason: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            approval = connection.execute(
                "SELECT session_id FROM production_approvals WHERE id=?", (approval_id,)
            ).fetchone()
        if approval is None:
            raise KeyError(f"approval not found: {approval_id}")
        return self.decide_approval(approval["session_id"], approval_id, approved, reason)

    def pause(self, session_id: str, generation_only: bool = False) -> dict[str, Any]:
        with self.store.connect() as connection:
            if generation_only:
                changed = connection.execute("UPDATE production_sessions SET generation_paused=1, updated_at=? WHERE id=?", (utc_now(), session_id)).rowcount
            else:
                changed = connection.execute("UPDATE production_sessions SET state='paused', generation_paused=1, updated_at=? WHERE id=?", (utc_now(), session_id)).rowcount
            if not changed: raise KeyError("production session not found")
            self._event(connection, session_id, "paused", {"generation_only": generation_only})
        return self.inspect(session_id)

    def resume(self, session_id: str) -> dict[str, Any]:
        with self.store.connect() as connection:
            changed = connection.execute("UPDATE production_sessions SET state='active', generation_paused=0, updated_at=? WHERE id=?", (utc_now(), session_id)).rowcount
            if not changed: raise KeyError("production session not found")
            self._event(connection, session_id, "resumed", {})
        return self.inspect(session_id)

    def cancel(self, session_id: str, request: CancelRequest) -> dict[str, Any]:
        if self.media is None:
            raise RuntimeError("cancellation requires a running media service")
        operations = self.store.list_operations(session_id=session_id)
        targets = []
        for operation in operations:
            media_type, state = operation["media_type"], operation["state"]
            if request.operation_id == operation["id"]:
                targets.append(operation); continue
            if state == "running" and request.active_scope in {"all", "current", media_type}:
                targets.append(operation)
            if state == "queued" and (request.queued_scope == "all" or request.queued_scope == media_type or
                                      (request.queued_scope == "selected" and operation["id"] in request.queued_operation_ids)):
                targets.append(operation)
        for operation in targets:
            self.media.cancel_operation(operation["id"], request.reason, request.actor)
        if request.controller_disposition == "pause_session": self.pause(session_id)
        elif request.controller_disposition == "pause_generation": self.pause(session_id, generation_only=True)
        return self.inspect(session_id)

    def emergency_stop(self, session_id: str, reason: str) -> dict[str, Any]:
        return self.cancel(session_id, CancelRequest(active_scope="all", queued_scope="all",
                           controller_disposition="pause_session", reason=reason,
                           idempotency_key=f"emergency-{uuid.uuid4().hex}"))
