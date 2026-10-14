from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .schemas import (
    Artifact,
    ArtifactKind,
    CanonicalAsset,
    CanonicalAssetSpec,
    ExecutionMode,
    Job,
    JobKind,
    JobState,
    Project,
    ProjectCreate,
    ProjectState,
    ProjectUpdate,
    Scene,
    SceneSpec,
    SceneState,
    utc_now,
)
from .state import validate_project_transition, validate_scene_transition


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  idea TEXT NOT NULL,
  duration_seconds INTEGER NOT NULL,
  visual_style TEXT NOT NULL,
  research_required INTEGER NOT NULL,
  source_mode TEXT NOT NULL DEFAULT 'description',
  state TEXT NOT NULL,
  resume_state TEXT,
  execution_mode TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS character_bibles (
  project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  data_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  extract TEXT NOT NULL,
  claims_json TEXT NOT NULL,
  retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  origin TEXT NOT NULL,
  original_name TEXT NOT NULL,
  source_url TEXT,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reference_analyses (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES reference_sources(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  blueprint_json TEXT NOT NULL,
  trace_json TEXT NOT NULL,
  approved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, version)
);
CREATE TABLE IF NOT EXISTS scenes (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  continuity_stale INTEGER NOT NULL DEFAULT 0,
  selected_start_artifact_id TEXT,
  selected_end_artifact_id TEXT,
  selected_video_artifact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, position)
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id TEXT REFERENCES scenes(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  error_code TEXT,
  error_message TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  workflow_version TEXT NOT NULL,
  seed INTEGER NOT NULL,
  parameters_json TEXT NOT NULL,
  input_hashes_json TEXT NOT NULL,
  status TEXT NOT NULL,
  error_json TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(job_id, ordinal)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id TEXT REFERENCES scenes(id) ON DELETE CASCADE,
  attempt_id TEXT REFERENCES attempts(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_assets (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  role TEXT NOT NULL,
  label TEXT NOT NULL,
  prompt TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  approved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_canonical_assets_project ON canonical_assets(project_id, role, key);
CREATE TABLE IF NOT EXISTS qa_reports (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id TEXT REFERENCES scenes(id) ON DELETE CASCADE,
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE CASCADE,
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id TEXT REFERENCES scenes(id) ON DELETE CASCADE,
  gate TEXT NOT NULL,
  approved INTEGER NOT NULL,
  notes TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_manifests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  manifest_json TEXT NOT NULL,
  artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  scene_id TEXT REFERENCES scenes(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  data_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenes_project ON scenes(project_id, position);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(projects)")
        }
        if "source_mode" not in columns:
            connection.execute(
                "ALTER TABLE projects ADD COLUMN source_mode TEXT NOT NULL DEFAULT 'description'"
            )

    def add_event(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
        scene_id: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO events(project_id, scene_id, event_type, data_json, created_at) VALUES(?,?,?,?,?)",
            (project_id, scene_id, event_type, json.dumps(data or {}), utc_now()),
        )

    def create_project(self, request: ProjectCreate, mode: ExecutionMode) -> Project:
        project_id = str(uuid.uuid4())
        now = utc_now()
        title = request.title or request.idea.strip().split(".")[0][:80]
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO projects(id,title,idea,duration_seconds,visual_style,research_required,source_mode,state,resume_state,execution_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    title,
                    request.idea,
                    request.duration_seconds,
                    request.visual_style,
                    int(request.research_required),
                    request.source_mode,
                    ProjectState.DRAFT,
                    None,
                    mode,
                    now,
                    now,
                ),
            )
            for url in request.source_urls:
                connection.execute(
                    "INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        "User-provided source",
                        url,
                        "",
                        "[]",
                        now,
                    ),
                )
            self.add_event(connection, project_id, "project.created", request.model_dump())
        return self.get_project(project_id)

    def add_reference_source(
        self,
        project_id: str,
        *,
        origin: str,
        original_name: str,
        path: Path,
        sha256: str,
        metadata: dict[str, Any],
        artifact_id: str,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        source_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM reference_sources WHERE project_id=?",
                (project_id,),
            )
            connection.execute(
                "INSERT INTO reference_sources(id,project_id,origin,original_name,source_url,path,sha256,metadata_json,artifact_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    source_id, project_id, origin, original_name, source_url,
                    str(path), sha256, json.dumps(metadata), artifact_id, now,
                ),
            )
            self.add_event(
                connection, project_id, "reference.source_added",
                {"source_id": source_id, "origin": origin, "original_name": original_name},
            )
        source = self.get_reference_source(project_id)
        assert source is not None
        return source

    def get_reference_source(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reference_sources WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["metadata"] = json.loads(value.pop("metadata_json"))
        return value

    def save_reference_analysis(
        self,
        project_id: str,
        source_id: str,
        blueprint: dict[str, Any],
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM reference_analyses WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            analysis_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO reference_analyses(id,project_id,source_id,version,blueprint_json,trace_json,approved,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    analysis_id, project_id, source_id, version,
                    json.dumps(blueprint), json.dumps(trace), 0, now, now,
                ),
            )
            self.add_event(
                connection, project_id, "reference.analysis_saved",
                {"analysis_id": analysis_id, "version": version},
            )
        analysis = self.get_reference_analysis(project_id)
        assert analysis is not None
        return analysis

    def get_reference_analysis(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reference_analyses WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["blueprint"] = json.loads(value.pop("blueprint_json"))
        value["trace"] = json.loads(value.pop("trace_json"))
        value["approved"] = bool(value["approved"])
        return value

    def approve_reference_analysis(self, analysis_id: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM reference_analyses WHERE id=?", (analysis_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"reference analysis {analysis_id} not found")
            connection.execute(
                "UPDATE reference_analyses SET approved=1,updated_at=? WHERE id=?",
                (utc_now(), analysis_id),
            )
            self.add_event(
                connection, row["project_id"], "reference.analysis_approved",
                {"analysis_id": analysis_id},
            )

    def get_project(self, project_id: str) -> Project:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(f"project {project_id} not found")
        return Project(**dict(row))

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [Project(**dict(row)) for row in rows]

    def update_project(self, project_id: str, request: ProjectUpdate) -> Project:
        values = request.model_dump(exclude_none=True)
        if not values:
            return self.get_project(project_id)
        allowed = {"title", "idea", "duration_seconds", "visual_style"}
        if set(values) - allowed:
            raise ValueError("unsupported project field")
        with self.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone():
                raise KeyError(f"project {project_id} not found")
            fields = [f"{name}=?" for name in values]
            connection.execute(
                f"UPDATE projects SET {', '.join(fields)}, updated_at=? WHERE id=?",
                [*values.values(), utc_now(), project_id],
            )
            self.add_event(connection, project_id, "project.updated", values)
        return self.get_project(project_id)

    def has_active_jobs(self, project_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND state IN (?,?) LIMIT 1",
                (project_id, JobState.QUEUED, JobState.RUNNING),
            ).fetchone()
        return row is not None

    def reset_project(self, project_id: str) -> Project:
        if self.has_active_jobs(project_id):
            raise ValueError("cancel active jobs before restarting the project")
        with self.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM projects WHERE id=?", (project_id,)
            ).fetchone():
                raise KeyError(f"project {project_id} not found")
            for table in (
                "render_manifests", "qa_reports", "approvals", "canonical_assets", "artifacts",
                "jobs", "scenes", "character_bibles",
            ):
                connection.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            connection.execute(
                "UPDATE projects SET state=?, resume_state=NULL, updated_at=? WHERE id=?",
                (ProjectState.DRAFT, utc_now(), project_id),
            )
            self.add_event(connection, project_id, "project.restarted")
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        if self.has_active_jobs(project_id):
            raise ValueError("cancel active jobs before deleting the project")
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"project {project_id} not found")

    def transition_project(self, project_id: str, target: ProjectState, reason: str = "") -> Project:
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise KeyError(f"project {project_id} not found")
            current = ProjectState(row["state"])
            validate_project_transition(current, target)
            now = utc_now()
            resume_state = current if target == ProjectState.PAUSED else None
            connection.execute(
                "UPDATE projects SET state=?, resume_state=COALESCE(?, resume_state), updated_at=? WHERE id=?",
                (target, resume_state, now, project_id),
            )
            self.add_event(connection, project_id, "project.transition", {"from": current, "to": target, "reason": reason})
        return self.get_project(project_id)

    def resume_project(self, project_id: str) -> Project:
        with self.transaction() as connection:
            row = connection.execute("SELECT state, resume_state FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row or row["state"] != ProjectState.PAUSED:
                raise ValueError("project is not paused")
            target = ProjectState(row["resume_state"] or ProjectState.DRAFT)
            connection.execute(
                "UPDATE projects SET state=?, resume_state=NULL, updated_at=? WHERE id=?",
                (target, utc_now(), project_id),
            )
            self.add_event(connection, project_id, "project.resumed", {"to": target})
        return self.get_project(project_id)

    def replace_storyboard(
        self, project_id: str, scenes: list[SceneSpec], bible: dict[str, Any],
        canonical_assets: list[CanonicalAssetSpec] | None = None,
    ) -> list[Scene]:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute("DELETE FROM scenes WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM canonical_assets WHERE project_id=?", (project_id,))
            connection.execute(
                "INSERT INTO character_bibles(project_id,data_json,created_at,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
                (project_id, json.dumps(bible), now, now),
            )
            for asset in canonical_assets or []:
                connection.execute(
                    "INSERT INTO canonical_assets(id,project_id,key,role,label,prompt,description,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), project_id, asset.key, asset.role, asset.label,
                        asset.prompt, asset.description, now, now,
                    ),
                )
            for position, spec in enumerate(scenes, 1):
                connection.execute(
                    "INSERT INTO scenes(id,project_id,position,revision,state,spec_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), project_id, position, 1, SceneState.PLANNED, spec.model_dump_json(), now, now),
                )
            self.add_event(connection, project_id, "storyboard.replaced", {"scene_count": len(scenes), "bible": bible})
        return self.list_scenes(project_id)

    def list_scenes(self, project_id: str) -> list[Scene]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM scenes WHERE project_id=? ORDER BY position", (project_id,)).fetchall()
        return [self._scene(row) for row in rows]

    def get_scene(self, scene_id: str) -> Scene:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not row:
            raise KeyError(f"scene {scene_id} not found")
        return self._scene(row)

    def update_scene_spec(self, scene_id: str, spec: SceneSpec) -> Scene:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM scenes WHERE id=?", (scene_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"scene {scene_id} not found")
            connection.execute(
                "UPDATE scenes SET spec_json=?, revision=revision+1, updated_at=? WHERE id=?",
                (spec.model_dump_json(), utc_now(), scene_id),
            )
            self.add_event(
                connection, row["project_id"], "scene.updated", spec.model_dump(), scene_id
            )
        return self.get_scene(scene_id)

    def delete_scene(self, scene_id: str) -> None:
        scene = self.get_scene(scene_id)
        with self.transaction() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM scenes WHERE project_id=?", (scene.project_id,)
            ).fetchone()[0]
            if count <= 3:
                raise ValueError("a storyboard must keep at least three scenes")
            connection.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
            connection.execute(
                "UPDATE scenes SET position=-position WHERE project_id=?",
                (scene.project_id,),
            )
            rows = connection.execute(
                "SELECT id FROM scenes WHERE project_id=? ORDER BY position DESC",
                (scene.project_id,),
            ).fetchall()
            for position, row in enumerate(rows, 1):
                connection.execute(
                    "UPDATE scenes SET position=?, updated_at=? WHERE id=?",
                    (position, utc_now(), row["id"]),
                )
            self.add_event(
                connection, scene.project_id, "scene.deleted", {"scene_id": scene_id}
            )

    @staticmethod
    def _scene(row: sqlite3.Row) -> Scene:
        data = dict(row)
        data["spec"] = json.loads(data.pop("spec_json"))
        data["continuity_stale"] = bool(data["continuity_stale"])
        return Scene(**data)

    def transition_scene(self, scene_id: str, target: SceneState, reason: str = "") -> Scene:
        with self.transaction() as connection:
            row = connection.execute("SELECT project_id,state FROM scenes WHERE id=?", (scene_id,)).fetchone()
            if not row:
                raise KeyError(f"scene {scene_id} not found")
            current = SceneState(row["state"])
            validate_scene_transition(current, target)
            connection.execute("UPDATE scenes SET state=?, updated_at=? WHERE id=?", (target, utc_now(), scene_id))
            self.add_event(connection, row["project_id"], "scene.transition", {"from": current, "to": target, "reason": reason}, scene_id)
        return self.get_scene(scene_id)

    def select_artifacts(
        self,
        scene_id: str,
        *,
        start_id: str | None = None,
        end_id: str | None = None,
        video_id: str | None = None,
    ) -> Scene:
        scene = self.get_scene(scene_id)
        fields: list[str] = []
        values: list[Any] = []
        mapping = {
            "selected_start_artifact_id": start_id,
            "selected_end_artifact_id": end_id,
            "selected_video_artifact_id": video_id,
        }
        for field, value in mapping.items():
            if value is not None:
                fields.append(f"{field}=?")
                values.append(value)
        if not fields:
            return scene
        with self.transaction() as connection:
            for artifact_id, expected_kind in (
                (start_id, ArtifactKind.IMAGE),
                (end_id, ArtifactKind.IMAGE),
                (video_id, ArtifactKind.VIDEO),
            ):
                if artifact_id is None:
                    continue
                artifact = connection.execute(
                    "SELECT scene_id,kind FROM artifacts WHERE id=?", (artifact_id,)
                ).fetchone()
                if (
                    not artifact
                    or artifact["scene_id"] != scene_id
                    or artifact["kind"] != expected_kind
                ):
                    raise ValueError(
                        "selected artifact does not belong to this scene or has the wrong type"
                    )
            values.extend([utc_now(), scene_id])
            connection.execute(f"UPDATE scenes SET {', '.join(fields)}, updated_at=? WHERE id=?", values)
            if end_id is not None:
                next_row = connection.execute(
                    "SELECT id FROM scenes WHERE project_id=? AND position=?", (scene.project_id, scene.position + 1)
                ).fetchone()
                if next_row:
                    connection.execute("UPDATE scenes SET continuity_stale=1, updated_at=? WHERE id=?", (utc_now(), next_row["id"]))
            self.add_event(connection, scene.project_id, "scene.artifacts_selected", mapping, scene_id)
        return self.get_scene(scene_id)

    def clear_continuity_stale(self, scene_id: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE scenes SET continuity_stale=0, updated_at=? WHERE id=?", (utc_now(), scene_id))

    def invalidate_keyframe_chain(self, scene_id: str) -> tuple[list[Scene], bool]:
        """Reset a scene and its connected successors before keyframe regeneration.

        Earlier artifacts and attempts remain available as history, but they are no
        longer selected and queued/running downstream work is cancelled.
        """
        scene = self.get_scene(scene_id)
        scenes = self.list_scenes(scene.project_id)
        start = next(index for index, item in enumerate(scenes) if item.id == scene_id)
        affected = [scene]
        for item in scenes[start + 1:]:
            if item.spec.starts_new_continuity or item.spec.continuity_group != scene.spec.continuity_group:
                break
            affected.append(item)
        ids = [item.id for item in affected]
        placeholders = ",".join("?" for _ in ids)
        now = utc_now()
        with self.transaction() as connection:
            running = connection.execute(
                f"SELECT 1 FROM jobs WHERE scene_id IN ({placeholders}) AND kind IN (?,?) AND state=? LIMIT 1",
                [*ids, JobKind.KEYFRAME, JobKind.VIDEO, JobState.RUNNING],
            ).fetchone() is not None
            rows = connection.execute(
                f"SELECT id FROM jobs WHERE scene_id IN ({placeholders}) AND kind IN (?,?) AND state IN (?,?)",
                [*ids, JobKind.KEYFRAME, JobKind.VIDEO, JobState.QUEUED, JobState.RUNNING],
            ).fetchall()
            connection.execute(
                f"UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                f"WHERE scene_id IN ({placeholders}) AND kind IN (?,?) AND state IN (?,?)",
                [JobState.CANCELLED, now, *ids, JobKind.KEYFRAME, JobKind.VIDEO,
                 JobState.QUEUED, JobState.RUNNING],
            )
            for index, item in enumerate(affected):
                target = SceneState.KEYFRAMES_QUEUED if index == 0 else SceneState.PLANNED
                connection.execute(
                    "UPDATE scenes SET state=?, continuity_stale=?, "
                    "selected_start_artifact_id=NULL, selected_end_artifact_id=NULL, "
                    "selected_video_artifact_id=NULL, updated_at=? WHERE id=?",
                    (target, int(index > 0), now, item.id),
                )
                self.add_event(
                    connection, scene.project_id,
                    "scene.keyframes_invalidated" if index else "scene.keyframes_regeneration_started",
                    {"root_scene_id": scene_id}, item.id,
                )
            if rows:
                self.add_event(
                    connection, scene.project_id, "scene.jobs_cancelled",
                    {"job_ids": [row["id"] for row in rows], "root_scene_id": scene_id},
                    scene_id,
                )
        return [self.get_scene(item.id) for item in affected], running

    def create_job(
        self,
        project_id: str,
        kind: JobKind,
        idempotency_key: str,
        payload: dict[str, Any],
        scene_id: str | None = None,
    ) -> Job:
        now = utc_now()
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                return self._job(existing)
            job_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO jobs(id,project_id,scene_id,kind,state,idempotency_key,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, scene_id, kind, JobState.QUEUED, idempotency_key, json.dumps(payload), now, now),
            )
            self.add_event(connection, project_id, "job.created", {"job_id": job_id, "kind": kind}, scene_id)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(f"job {job_id} not found")
        return self._job(row)

    def list_jobs(self, project_id: str | None = None) -> list[Job]:
        with self.connect() as connection:
            if project_id:
                rows = connection.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._job(row) for row in rows]

    def list_attempts(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT a.*, j.project_id, j.scene_id, j.kind AS job_kind "
                "FROM attempts a JOIN jobs j ON j.id=a.job_id "
                "WHERE j.project_id=? ORDER BY a.created_at",
                (project_id,),
            ).fetchall()
        attempts = []
        for row in rows:
            attempt = dict(row)
            attempt["parameters"] = json.loads(attempt.pop("parameters_json"))
            attempt["input_hashes"] = json.loads(
                attempt.pop("input_hashes_json")
            )
            raw_error = attempt.pop("error_json")
            attempt["error"] = json.loads(raw_error) if raw_error else None
            attempts.append(attempt)
        return attempts

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        for key in ("lease_owner", "lease_expires_at"):
            data.pop(key, None)
        return Job(**data)

    def claim_next_job(self, owner: str, lease_seconds: int = 900) -> Job | None:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.transaction() as connection:
            # A crashed worker cannot strand an expensive job forever.
            connection.execute(
                "UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE state=? AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (JobState.QUEUED, now_text, JobState.RUNNING, now_text),
            )
            row = connection.execute(
                "SELECT j.* FROM jobs j JOIN projects p ON p.id=j.project_id "
                "WHERE j.state=? AND p.state<>? ORDER BY j.created_at LIMIT 1",
                (JobState.QUEUED, ProjectState.PAUSED),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE jobs SET state=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=? "
                "WHERE id=? AND state=?",
                (JobState.RUNNING, owner, lease_expires, now_text, row["id"], JobState.QUEUED),
            )
        return self.get_job(row["id"])

    def finish_job(self, job_id: str, *, error_code: str | None = None, error_message: str | None = None) -> Job:
        target = JobState.FAILED if error_code else JobState.SUCCEEDED
        with self.transaction() as connection:
            row = connection.execute("SELECT project_id,scene_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["state"] == JobState.CANCELLED:
                return self._job(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            connection.execute(
                "UPDATE jobs SET state=?, error_code=?, error_message=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (target, error_code, error_message, utc_now(), job_id),
            )
            self.add_event(connection, row["project_id"], "job.finished", {"job_id": job_id, "state": target, "error_code": error_code}, row["scene_id"])
        return self.get_job(job_id)

    def cancel_project_jobs(self, project_id: str) -> list[Job]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE project_id=? AND state IN (?,?)",
                (project_id, JobState.QUEUED, JobState.RUNNING),
            ).fetchall()
            connection.execute(
                "UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                "WHERE project_id=? AND state IN (?,?)",
                (JobState.CANCELLED, utc_now(), project_id, JobState.QUEUED, JobState.RUNNING),
            )
            self.add_event(connection, project_id, "project.jobs_cancelled", {"job_ids": [row["id"] for row in rows]})
        return [self.get_job(row["id"]) for row in rows]

    def requeue_job(self, job_id: str, reason: str) -> Job:
        with self.transaction() as connection:
            row = connection.execute("SELECT project_id,scene_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            connection.execute(
                "UPDATE jobs SET state=?, error_code=NULL, error_message=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (JobState.QUEUED, utc_now(), job_id),
            )
            self.add_event(connection, row["project_id"], "job.requeued", {"job_id": job_id, "reason": reason}, row["scene_id"])
        return self.get_job(job_id)

    def create_attempt(self, job: Job, workflow_version: str, seed: int, parameters: dict[str, Any]) -> str:
        attempt_id = str(uuid.uuid4())
        with self.transaction() as connection:
            ordinal = connection.execute("SELECT COUNT(*) FROM attempts WHERE job_id=?", (job.id,)).fetchone()[0] + 1
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, job.id, ordinal, workflow_version, seed, json.dumps(parameters), "[]", "running", None, utc_now(), None),
            )
        return attempt_id

    def finish_attempt(self, attempt_id: str, status: str, error: dict[str, Any] | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE attempts SET status=?, error_json=?, completed_at=? WHERE id=?",
                (status, json.dumps(error) if error else None, utc_now(), attempt_id),
            )

    def add_artifact(
        self,
        project_id: str,
        kind: ArtifactKind,
        path: Path,
        sha256: str,
        metadata: dict[str, Any],
        scene_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Artifact:
        artifact_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                (artifact_id, project_id, scene_id, attempt_id, kind, str(path), sha256, json.dumps(metadata), now),
            )
            self.add_event(connection, project_id, "artifact.created", {"artifact_id": artifact_id, "kind": kind, "path": str(path)}, scene_id)
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row:
            raise KeyError(f"artifact {artifact_id} not found")
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json"))
        return Artifact(**data)

    def list_artifacts(self, project_id: str, scene_id: str | None = None) -> list[Artifact]:
        with self.connect() as connection:
            if scene_id:
                rows = connection.execute("SELECT * FROM artifacts WHERE project_id=? AND scene_id=? ORDER BY created_at", (project_id, scene_id)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["metadata"] = json.loads(data.pop("metadata_json"))
            result.append(Artifact(**data))
        return result

    @staticmethod
    def _canonical_asset(row: sqlite3.Row) -> CanonicalAsset:
        data = dict(row)
        data["approved"] = bool(data["approved"])
        return CanonicalAsset(**data)

    def list_canonical_assets(self, project_id: str) -> list[CanonicalAsset]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM canonical_assets WHERE project_id=? "
                "ORDER BY CASE role WHEN 'character' THEN 0 WHEN 'environment' THEN 1 "
                "WHEN 'prop' THEN 2 ELSE 3 END, created_at",
                (project_id,),
            ).fetchall()
        return [self._canonical_asset(row) for row in rows]

    def get_canonical_asset(self, asset_id: str) -> CanonicalAsset:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM canonical_assets WHERE id=?", (asset_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"canonical asset {asset_id} not found")
        return self._canonical_asset(row)

    def attach_canonical_artifact(self, asset_id: str, artifact_id: str) -> CanonicalAsset:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT project_id FROM canonical_assets WHERE id=?", (asset_id,)
            ).fetchone()
            artifact = connection.execute(
                "SELECT project_id,scene_id,kind FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
            if not row or not artifact:
                raise KeyError("canonical asset or artifact not found")
            if artifact["project_id"] != row["project_id"] or artifact["scene_id"] is not None or artifact["kind"] != ArtifactKind.IMAGE:
                raise ValueError("canonical artifact must be a project-level image")
            connection.execute(
                "UPDATE canonical_assets SET artifact_id=?, approved=0, updated_at=? WHERE id=?",
                (artifact_id, utc_now(), asset_id),
            )
            self.add_event(
                connection, row["project_id"], "canonical_asset.generated",
                {"canonical_asset_id": asset_id, "artifact_id": artifact_id},
            )
        return self.get_canonical_asset(asset_id)

    def reset_canonical_asset(
        self, asset_id: str, prompt: str, feedback: str = "", rationale: str = ""
    ) -> bool:
        asset = self.get_canonical_asset(asset_id)
        active = [
            job for job in self.list_jobs(asset.project_id)
            if job.kind == JobKind.CANONICAL_ASSET
            and job.payload.get("canonical_asset_id") == asset_id
            and job.state in {JobState.QUEUED, JobState.RUNNING}
        ]
        running = any(job.state == JobState.RUNNING for job in active)
        now = utc_now()
        with self.transaction() as connection:
            for job in active:
                connection.execute(
                    "UPDATE jobs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE id=? AND state IN (?,?)",
                    (JobState.CANCELLED, now, job.id, JobState.QUEUED, JobState.RUNNING),
                )
            connection.execute(
                "UPDATE canonical_assets SET prompt=?, artifact_id=NULL, approved=0, updated_at=? "
                "WHERE id=?",
                (prompt, now, asset_id),
            )
            self.add_event(
                connection,
                asset.project_id,
                "canonical_asset.regeneration_started",
                {
                    "canonical_asset_id": asset_id,
                    "feedback": feedback,
                    "revision_rationale": rationale,
                    "cancelled_job_ids": [job.id for job in active],
                },
            )
        return running

    def approve_canonical_assets(self, project_id: str, approved: bool) -> list[CanonicalAsset]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id,artifact_id FROM canonical_assets WHERE project_id=?", (project_id,)
            ).fetchall()
            if approved and any(not row["artifact_id"] for row in rows):
                raise ValueError("all canonical assets must be generated before approval")
            connection.execute(
                "UPDATE canonical_assets SET approved=?, updated_at=? WHERE project_id=?",
                (int(approved), utc_now(), project_id),
            )
            self.add_event(
                connection, project_id, "canonical_assets.approved" if approved else "canonical_assets.rejected",
                {"count": len(rows)},
            )
        return self.list_canonical_assets(project_id)

    def list_qa_reports(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM qa_reports WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        reports = []
        for row in rows:
            report = dict(row)
            report["report"] = json.loads(report.pop("report_json"))
            reports.append(report)
        return reports

    def record_approval(self, project_id: str, gate: str, approved: bool, notes: str, scene_id: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), project_id, scene_id, gate, int(approved), notes, utc_now()),
            )
            self.add_event(connection, project_id, "approval.recorded", {"gate": gate, "approved": approved, "notes": notes}, scene_id)

    def add_qa_report(self, project_id: str, scene_id: str, artifact_id: str, report: dict[str, Any]) -> str:
        report_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO qa_reports VALUES(?,?,?,?,?,?)",
                (report_id, project_id, scene_id, artifact_id, json.dumps(report), utc_now()),
            )
            self.add_event(connection, project_id, "qa.completed",
                {"report_id": report_id, "artifact_id": artifact_id, "technical_pass": report.get("technical_pass")}, scene_id)
        return report_id

    def save_render_manifest(self, project_id: str, manifest: dict[str, Any], artifact_id: str | None = None) -> str:
        manifest_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO render_manifests VALUES(?,?,?,?,?)",
                (manifest_id, project_id, json.dumps(manifest), artifact_id, utc_now()),
            )
            self.add_event(connection, project_id, "render.manifest_saved",
                {"manifest_id": manifest_id, "artifact_id": artifact_id})
        return manifest_id

    def list_render_manifests(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM render_manifests WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        manifests = []
        for row in rows:
            manifest = dict(row)
            manifest["manifest"] = json.loads(manifest.pop("manifest_json"))
            manifests.append(manifest)
        return manifests

    def list_approvals(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM approvals WHERE project_id=? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        return [
            {**dict(row), "approved": bool(row["approved"])}
            for row in rows
        ]

    def save_research_sources(self, project_id: str, sources: list[dict[str, Any]]) -> None:
        with self.transaction() as connection:
            for source in sources:
                connection.execute(
                    "INSERT INTO research_sources VALUES(?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        source.get("title", ""),
                        source["url"],
                        source.get("description", source.get("extract", "")),
                        json.dumps(source.get("claims", [])),
                        utc_now(),
                    ),
                )
            self.add_event(
                connection,
                project_id,
                "research.sources_saved",
                {"count": len(sources)},
            )

    def list_research_sources(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM research_sources WHERE project_id=? ORDER BY retrieved_at",
                (project_id,),
            ).fetchall()
        result = []
        for row in rows:
            source = dict(row)
            source["claims"] = json.loads(source.pop("claims_json"))
            result.append(source)
        return result

    def get_character_bible(self, project_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT data_json FROM character_bibles WHERE project_id=?", (project_id,)).fetchone()
        return json.loads(row["data_json"]) if row else {}

    def events_since(self, project_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id", (project_id, after_id)
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["data"] = json.loads(event.pop("data_json"))
            events.append(event)
        return events
