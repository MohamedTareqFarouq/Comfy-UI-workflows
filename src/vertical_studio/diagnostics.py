from __future__ import annotations
import json, platform, shutil, subprocess
from pathlib import Path
from typing import Any
from .config import Settings

def _command(command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable: return {"ok": False, "error": f"{command[0]} not found"}
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    return {"ok": result.returncode == 0, "output": (result.stdout or result.stderr).strip()[:4000]}

def diagnose(settings: Settings) -> dict[str, Any]:
    checks = {
        "platform": {"ok": True, "mode": settings.platform_mode, "release": platform.release()},
        "python": {"ok": True, "version": platform.python_version()},
        "ffmpeg": _command(["ffmpeg", "-version"]),
        "ffprobe": _command(["ffprobe", "-version"]),
        "nvidia": _command(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
        "data_dir": {"ok": settings.data_dir.exists(), "path": str(settings.data_dir)},
        "workflows_dir": {"ok": settings.workflows_dir.exists(), "path": str(settings.workflows_dir)},
    }
    checks["ready_for_simulation"] = {"ok": checks["ffmpeg"]["ok"] and checks["ffprobe"]["ok"]}
    checks["ready_for_real_generation"] = {
        "ok": checks["nvidia"]["ok"] and settings.execution_mode == "real",
        "qualified": False,
        "message": "Real profiles remain unqualified until an explicit smoke or full qualification run.",
    }
    return checks
