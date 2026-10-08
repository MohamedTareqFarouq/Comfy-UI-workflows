from __future__ import annotations

import re
import subprocess
from pathlib import Path


class PlatformPathError(ValueError):
    pass


class PlatformAdapter:
    """Keeps Windows/WSL path translation out of production services."""

    _windows_path = re.compile(r"^[A-Za-z]:[\\\\/]")

    def __init__(self, mode: str):
        self.mode = mode

    def export_path(self, value: str | None, default: Path) -> Path:
        if not value:
            return default.expanduser().resolve()
        if self.mode == "wsl2" and self._windows_path.match(value):
            result = subprocess.run(
                ["wslpath", "-u", value],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode:
                raise PlatformPathError(
                    result.stderr.strip() or f"cannot translate Windows path: {value}"
                )
            return Path(result.stdout.strip())
        if self.mode == "linux" and self._windows_path.match(value):
            raise PlatformPathError(
                "Windows export paths are accepted only while running under WSL2"
            )
        return Path(value).expanduser().resolve()
