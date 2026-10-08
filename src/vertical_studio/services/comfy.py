from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from websockets.sync.client import connect as websocket_connect


class GenerationError(RuntimeError):
    def __init__(self, code: str, message: str, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


@dataclass(slots=True)
class GenerationResult:
    files: list[Path]
    metadata: dict[str, Any]


class Generator(Protocol):
    def generate_keyframes(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult: ...
    def generate_video(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult: ...
    def health(self) -> dict[str, Any]: ...
    def interrupt(self) -> None: ...
    def free_memory(self) -> None: ...


class ComfyClient:
    """Real ComfyUI adapter using API templates and deterministic node maps."""

    def __init__(self, base_url: str, workflow_dir: Path, timeout: float = 3600):
        self.base_url = base_url.rstrip("/")
        self.workflow_dir = workflow_dir
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/system_stats", timeout=5)
            return {"available": response.is_success, "status": response.status_code}
        except httpx.HTTPError as exc:
            return {"available": False, "error": str(exc)}

    def _load_template(self, name: str) -> dict[str, Any]:
        path = self.workflow_dir / f"{name}.json"
        if not path.exists():
            raise GenerationError("workflow_missing", f"API workflow template not found: {path}")
        value = json.loads(path.read_text())
        if "nodes" in value or not value or not all(
            isinstance(node, dict) and "class_type" in node for node in value.values()
        ):
            raise GenerationError(
                "workflow_not_api_format",
                f"{path.name} is not ComfyUI API format; export it with File > Export (API)",
            )
        return value

    def _load_mapping(
        self, template_name: str, required: set[str]
    ) -> dict[str, Any]:
        path = self.workflow_dir / "workflow_maps.json"
        if not path.exists():
            raise GenerationError(
                "workflow_mapping_missing",
                f"explicit node map not found: {path}",
            )
        mapping = json.loads(path.read_text()).get(template_name)
        if not isinstance(mapping, dict):
            raise GenerationError(
                "workflow_mapping_missing",
                f"node map for {template_name} is missing",
            )
        missing = sorted(required - set(mapping))
        if missing:
            raise GenerationError(
                "workflow_mapping_incomplete",
                f"{template_name} mapping is missing: {', '.join(missing)}",
            )
        return mapping

    @staticmethod
    def _mapped_patches(
        mapping: dict[str, Any], values: dict[str, Any]
    ) -> list[dict[str, Any]]:
        patches: list[dict[str, Any]] = []
        for name, value in values.items():
            pointers = mapping.get(name, [])
            if isinstance(pointers, dict):
                pointers = [pointers]
            for pointer in pointers:
                patches.append(
                    {
                        "node_id": str(pointer["node_id"]),
                        "input": pointer["input"],
                        "value": value,
                    }
                )
        return patches

    def _upload_image(self, path: Path) -> str:
        try:
            with path.open("rb") as stream:
                response = httpx.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (path.name, stream, "application/octet-stream")},
                    data={"overwrite": "true", "type": "input"},
                    timeout=120,
                )
            response.raise_for_status()
            value = response.json()
            subfolder = value.get("subfolder", "")
            return f"{subfolder}/{value['name']}" if subfolder else value["name"]
        except (OSError, httpx.HTTPError, KeyError) as exc:
            raise GenerationError("comfy_upload", str(exc), transient=True) from exc

    def _run(self, template_name: str, payload: dict[str, Any], output_dir: Path) -> GenerationResult:
        workflow = self._load_template(template_name)
        for pointer in payload.get("patches", []):
            try:
                node = workflow[str(pointer["node_id"])]
                node["inputs"][pointer["input"]] = pointer["value"]
            except KeyError as exc:
                raise GenerationError(
                    "workflow_patch_invalid",
                    f"invalid patch {pointer} for {template_name}",
                ) from exc

        client_id = str(uuid.uuid4())
        websocket = None
        websocket_url = self.base_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        try:
            websocket = websocket_connect(
                f"{websocket_url}/ws?clientId={client_id}",
                open_timeout=10,
                close_timeout=2,
            )
        except Exception:
            websocket = None
        try:
            response = httpx.post(
                f"{self.base_url}/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=30,
            )
            response.raise_for_status()
            prompt_id = response.json()["prompt_id"]
        except (httpx.HTTPError, KeyError) as exc:
            if websocket is not None:
                websocket.close()
            raise GenerationError("comfy_submit", str(exc), transient=True) from exc

        deadline = time.monotonic() + self.timeout
        if websocket is not None:
            try:
                while time.monotonic() < deadline:
                    try:
                        message = json.loads(
                            websocket.recv(
                                timeout=min(5, max(0.1, deadline - time.monotonic()))
                            )
                        )
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    data = message.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    if message.get("type") == "execution_error":
                        detail = data.get("exception_message", "ComfyUI node failed")
                        lower = detail.lower()
                        is_oom = "out of memory" in lower or "cuda oom" in lower
                        raise GenerationError(
                            "oom" if is_oom else "comfy_node_failure",
                            detail,
                            transient=is_oom,
                        )
                    if message.get("type") == "executing" and data.get("node") is None:
                        break
            finally:
                websocket.close()

        history: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{self.base_url}/history/{prompt_id}", timeout=15)
                response.raise_for_status()
                value = response.json()
                if prompt_id in value:
                    history = value[prompt_id]
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        if history is None:
            raise GenerationError("comfy_timeout", "ComfyUI job timed out", transient=True)

        output_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for node_output in history.get("outputs", {}).values():
            entries = (
                node_output.get("images", [])
                + node_output.get("videos", [])
                + node_output.get("gifs", [])
            )
            for entry in entries:
                params = {
                    "filename": entry["filename"],
                    "subfolder": entry.get("subfolder", ""),
                    "type": entry.get("type", "output"),
                }
                response = httpx.get(
                    f"{self.base_url}/view", params=params, timeout=120
                )
                response.raise_for_status()
                target = output_dir / Path(entry["filename"]).name
                target.write_bytes(response.content)
                files.append(target)
        if not files:
            raise GenerationError("comfy_no_output", "ComfyUI completed without media output")
        return GenerationResult(files=files, metadata={"prompt_id": prompt_id, "history": history})

    def generate_keyframes(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        template = payload.get("template", "flux_t2i_api")
        if payload.get("patches") is not None:
            return self._run(template, payload, output_dir)
        self._load_template(template)

        mapping = self._load_mapping(
            template, {"prompt", "negative_prompt", "seed", "width", "height", "output_prefix"}
        )
        spec = payload["spec"]
        all_files: list[Path] = []
        prompts: list[dict[str, Any]] = []
        base_seed = int(payload["seed"])
        for candidate in range(1, int(payload.get("candidates", 2)) + 1):
            for role_offset, role in enumerate(("start", "end")):
                composition = spec[f"{role}_composition"]
                prompt = (
                    f"{spec['narrative_purpose']}. {composition}. "
                    f"Vertical 9:16. {spec.get('safe_area', '')}"
                )
                values = {
                    "prompt": prompt,
                    "negative_prompt": ", ".join(spec.get("negative_constraints", [])),
                    "seed": base_seed + candidate * 10 + role_offset,
                    "width": int(payload["width"]),
                    "height": int(payload["height"]),
                    "output_prefix": f"{payload['scene_id']}-c{candidate}-{role}",
                }
                result = self._run(
                    template,
                    {**payload, "patches": self._mapped_patches(mapping, values)},
                    output_dir / f"candidate-{candidate}-{role}",
                )
                source = result.files[0]
                target = output_dir / f"candidate-{candidate}-{role}{source.suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), target)
                all_files.append(target)
                prompts.append({"candidate": candidate, "role": role, "prompt_id": result.metadata["prompt_id"]})
                self.free_memory()
        return GenerationResult(files=all_files, metadata={"runs": prompts})

    def generate_video(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        template = payload.get("template", "wan14b_lightx2v_flf2v_api")
        if payload.get("patches") is not None:
            return self._run(template, payload, output_dir)
        self._load_template(template)

        mapping = self._load_mapping(
            template,
            {
                "prompt",
                "negative_prompt",
                "seed",
                "width",
                "height",
                "frames",
                "start_image",
                "end_image",
                "output_prefix",
            },
        )
        spec = payload["spec"]
        start_name = self._upload_image(Path(payload["start_image_path"]))
        end_name = self._upload_image(Path(payload["end_image_path"]))
        values = {
            "prompt": (
                f"{spec['narrative_purpose']}. Motion: {spec['motion']}. "
                f"Camera: {spec.get('camera', 'stable camera')}. Preserve identity, "
                "wardrobe, environment and vertical composition."
            ),
            "negative_prompt": ", ".join(spec.get("negative_constraints", [])),
            "seed": int(payload["seed"]),
            "width": int(payload["width"]),
            "height": int(payload["height"]),
            "frames": int(payload["frames"]),
            "start_image": start_name,
            "end_image": end_name,
            "output_prefix": f"{payload['scene_id']}-attempt-{payload.get('attempt', 1)}",
        }
        return self._run(
            template,
            {**payload, "patches": self._mapped_patches(mapping, values)},
            output_dir,
        )

    def interrupt(self) -> None:
        try:
            httpx.post(f"{self.base_url}/interrupt", timeout=10)
        except httpx.HTTPError:
            pass

    def free_memory(self) -> None:
        try:
            httpx.post(
                f"{self.base_url}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=30,
            )
        except httpx.HTTPError:
            pass
