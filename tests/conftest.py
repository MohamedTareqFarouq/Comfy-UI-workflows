from __future__ import annotations

from pathlib import Path

import pytest

from vertical_studio.api import Components
from vertical_studio.config import Settings
from vertical_studio.services.renderer import Renderer


@pytest.fixture
def components(tmp_path: Path) -> Components:
    repo = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        db_path=data / "studio.sqlite3",
        projects_dir=data / "projects",
        export_dir=data / "exports",
        static_dir=repo / "src" / "vertical_studio" / "static",
        workflows_dir=repo / "workflows",
        execution_mode="simulation",
    )
    settings.ensure_directories()
    result = Components(settings)
    result.worker.renderer = Renderer(width=270, height=480)
    return result
