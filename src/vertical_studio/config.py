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
    model_provider: str = "llamacpp"
    controller_model: str = "Qwen3-VL-4B-Instruct-GGUF"
    vision_model: str = "Qwen3-VL-4B-Instruct-GGUF"
    qwen_model: str = "Qwen3-VL-4B-Instruct-GGUF"
    manage_qwen: bool = False
    evict_ollama_models: bool = False
    enable_qwen_image: bool = True
    llama_executable: str = "llama"
    qwen_hf_repo: str = "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M"
    qwen_gpu_layers: int = 99
    qwen_context_size: int = 16384
    qwen_start_timeout: int = 300
    qwen_request_timeout: int = 600
    brave_api_key: str | None = None
    allow_unqualified_capabilities: bool = False
    keyframe_image_backend: str = "klein"

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
    keyframe_image_backend = (os.getenv("VVS_KEYFRAME_IMAGE_BACKEND") or "klein").lower()
    if keyframe_image_backend not in {"klein", "qwen_image21"}:
        raise ValueError("VVS_KEYFRAME_IMAGE_BACKEND must be 'klein' or 'qwen_image21'")
    model_provider = (os.getenv("VVS_MODEL_PROVIDER") or "llamacpp").lower()
    if model_provider not in {"llamacpp", "ollama"}:
        raise ValueError("VVS_MODEL_PROVIDER must be 'llamacpp' or 'ollama'")
    qwen_model = os.getenv("VVS_QWEN_MODEL", "Qwen3-VL-4B-Instruct-GGUF")
    controller_model = os.getenv("VVS_CONTROLLER_MODEL") or qwen_model
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
        model_provider=model_provider,
        controller_model=controller_model,
        vision_model=os.getenv("VVS_VISION_MODEL") or qwen_model,
        qwen_model=qwen_model,
        manage_qwen=os.getenv("VVS_MANAGE_QWEN", "false").lower()
        in {"1", "true", "yes", "on"},
        evict_ollama_models=os.getenv("VVS_OLLAMA_EVICT", "true").lower()
        in {"1", "true", "yes", "on"},
        enable_qwen_image=os.getenv("VVS_ENABLE_QWEN_IMAGE", "true").lower()
        in {"1", "true", "yes", "on"},
        llama_executable=os.getenv("VVS_LLAMA_EXECUTABLE", "llama"),
        qwen_hf_repo=os.getenv(
            "VVS_QWEN_HF_REPO", "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M"
        ),
        qwen_gpu_layers=int(os.getenv("VVS_QWEN_GPU_LAYERS", "99")),
        qwen_context_size=int(os.getenv("VVS_QWEN_CONTEXT_SIZE", "16384")),
        qwen_start_timeout=int(os.getenv("VVS_QWEN_START_TIMEOUT", "300")),
        qwen_request_timeout=int(os.getenv("VVS_QWEN_REQUEST_TIMEOUT", "600")),
        brave_api_key=os.getenv("VVS_BRAVE_API_KEY") or None,
        allow_unqualified_capabilities=os.getenv(
            "VVS_ALLOW_UNQUALIFIED_CAPABILITIES", "false"
        ).lower() in {"1", "true", "yes", "on"},
        keyframe_image_backend=keyframe_image_backend,
    )
    settings.ensure_directories()
    return settings
