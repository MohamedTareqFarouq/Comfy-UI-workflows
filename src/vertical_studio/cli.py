from __future__ import annotations
import argparse, json, os
from pathlib import Path
from .api import Components, create_app
from .config import load_settings
from .diagnostics import diagnose
from .maintenance import backup_database, restore_database
from .qualification import QualificationError, QualificationService
from .schemas import ApprovalRequest, ArtifactSelection, ProjectCreate, RenderRequest
from .services.renderer import Renderer

def demo() -> int:
    settings = load_settings()
    if not settings.is_simulation:
        raise SystemExit("demo is simulation-only; set VVS_EXECUTION_MODE=simulation")
    components = Components(settings)
    components.worker.renderer = Renderer(width=270, height=480)
    project = components.orchestrator.create_project(ProjectCreate(
        title="Simulation demo", idea="A railway officer welcomes a mysterious train in the fog",
        duration_seconds=10, visual_style="cinematic 3D cartoon"))
    components.orchestrator.plan_project(project.id)
    components.orchestrator.approve_storyboard(project.id, ApprovalRequest(approved=True))
    components.worker.run_until_idle()
    for scene in components.db.list_scenes(project.id):
        artifacts = components.db.list_artifacts(project.id, scene.id)
        start = next(a for a in artifacts if a.metadata.get("candidate") == 1 and a.metadata.get("role") == "start")
        end = next(a for a in artifacts if a.metadata.get("candidate") == 1 and a.metadata.get("role") == "end")
        components.orchestrator.approve_keyframes(scene.id, ArtifactSelection(
            start_artifact_id=start.id, end_artifact_id=end.id), ApprovalRequest(approved=True))
    components.worker.run_until_idle()
    for scene in components.db.list_scenes(project.id):
        components.orchestrator.approve_scene(scene.id, ApprovalRequest(approved=True))
    components.orchestrator.queue_render(project.id, RenderRequest())
    components.worker.run_until_idle()
    print(json.dumps(components.db.get_project(project.id).model_dump(), indent=2, default=str))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(prog="vertical-studio")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve"); serve.add_argument("--host"); serve.add_argument("--port", type=int)
    sub.add_parser("worker"); sub.add_parser("diagnose"); sub.add_parser("demo"); sub.add_parser("init-db")
    qualify = sub.add_parser("qualify")
    qualify.add_argument("level", choices=["diagnostics", "smoke", "full"])
    qualify.add_argument("--confirm-real-generation", action="store_true")
    backup = sub.add_parser("backup"); backup.add_argument("path")
    restore = sub.add_parser("restore"); restore.add_argument("path")
    args = parser.parse_args(); settings = load_settings()
    if args.command == "diagnose":
        print(json.dumps(diagnose(settings), indent=2)); return 0
    if args.command == "init-db":
        Components(settings); print(settings.db_path); return 0
    if args.command == "backup":
        print(backup_database(settings.db_path, Path(args.path)))
        return 0
    if args.command == "restore":
        safety_copy = restore_database(settings.db_path, Path(args.path))
        print(json.dumps({"restored": str(args.path), "previous_database": str(safety_copy)}, indent=2))
        return 0
    if args.command == "qualify":
        try:
            report = QualificationService(settings).run(
                args.level, confirmed_real_generation=args.confirm_real_generation
            )
        except QualificationError as exc:
            print(json.dumps({"qualified": False, "error": str(exc)}, indent=2))
            return 2
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "worker":
        Components(settings).worker.serve(); return 0
    if args.command == "demo": return demo()
    if args.command == "serve":
        import uvicorn
        uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port)
        return 0
    return 2

if __name__ == "__main__": raise SystemExit(main())
