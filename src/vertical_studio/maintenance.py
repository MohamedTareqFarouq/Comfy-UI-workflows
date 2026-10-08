from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(source: Path, target: Path) -> Path:
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
    return target


def restore_database(target: Path, backup: Path) -> Path:
    backup = backup.expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(backup)
    with sqlite3.connect(backup) as candidate:
        result = candidate.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"backup integrity check failed: {result}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safety_copy = target.with_name(f"{target.name}.before-restore-{stamp}")
    if target.exists():
        backup_database(target, safety_copy)
    with sqlite3.connect(backup) as source_db, sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
    return safety_copy
