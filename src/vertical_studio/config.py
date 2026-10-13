from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


def _path_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default.expanduser().resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    db_path: Path
    projects_dir: Path
    export_dir: Path
    static_dir: Path
    workflows_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    execution_mode: str = "simulation"
    comfy_url: str = "http://127.0.0.1:8188"
    llama_url: str = "http://127.0.0.1:8080"
    qwen_model: str = "Qwen3-VL-4B-Instruct-GGUF"
    manage_qwen: bool = False
    llama_executable: str = "llama"
    qwen_hf_repo: str = "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M"
    qwen_gpu_layers: int = 99
    qwen_context_size: int = 16384
    qwen_start_timeout: int = 300
    brave_api_key: str | None = None

    @property
    def is_simulation(self) -> bool:
        return self.execution_mode == "simulation"

    @property
    def platform_mode(self) -> str:
        release = platform.release().lower()
        return "wsl2" if "microsoft" in release else "linux"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.projects_dir, self.export_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    package_dir = Path(__file__).resolve().parent
    repo_dir = package_dir.parents[1]
    data_dir = _path_env(
        "VVS_DATA_DIR", Path.home() / ".local" / "share" / "vertical-video-studio"
    )
    export_dir = _path_env("VVS_EXPORT_DIR", data_dir / "exports")
    settings = Settings(
        data_dir=data_dir,
        db_path=data_dir / "studio.sqlite3",
        projects_dir=data_dir / "projects",
        export_dir=export_dir,
        static_dir=package_dir / "static",
        workflows_dir=repo_dir / "workflows",
        host=os.getenv("VVS_HOST", "127.0.0.1"),
        port=int(os.getenv("VVS_PORT", "8765")),
        execution_mode=os.getenv("VVS_EXECUTION_MODE", "simulation"),
        comfy_url=os.getenv("VVS_COMFY_URL", "http://127.0.0.1:8188").rstrip("/"),
        llama_url=os.getenv("VVS_LLAMA_URL", "http://127.0.0.1:8080").rstrip("/"),
        qwen_model=os.getenv("VVS_QWEN_MODEL", "Qwen3-VL-4B-Instruct-GGUF"),
        manage_qwen=os.getenv("VVS_MANAGE_QWEN", "false").lower()
        in {"1", "true", "yes", "on"},
        llama_executable=os.getenv("VVS_LLAMA_EXECUTABLE", "llama"),
        qwen_hf_repo=os.getenv(
            "VVS_QWEN_HF_REPO", "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M"
        ),
        qwen_gpu_layers=int(os.getenv("VVS_QWEN_GPU_LAYERS", "99")),
        qwen_context_size=int(os.getenv("VVS_QWEN_CONTEXT_SIZE", "16384")),
        qwen_start_timeout=int(os.getenv("VVS_QWEN_START_TIMEOUT", "300")),
        brave_api_key=os.getenv("VVS_BRAVE_API_KEY") or None,
    )
    settings.ensure_directories()
    return settings
