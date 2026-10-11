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
    def generate_character_sheet(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult: ...
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

    def _generate_t2i_image(
        self, output_dir: Path, payload: dict[str, Any], *, prompt: str,
        seed: int, output_prefix: str,
    ) -> GenerationResult:
        template = payload.get("t2i_template", payload.get("template", "flux_t2i_api"))
        mapping = self._load_mapping(
            template, {"prompt", "negative_prompt", "seed", "width", "height", "output_prefix"}
        )
        values = {
            "prompt": prompt,
            "seed": seed,
            "width": int(payload["width"]),
            "height": int(payload["height"]),
            "output_prefix": output_prefix,
        }
        return self._run(
            template, {**payload, "patches": self._mapped_patches(mapping, values)}, output_dir
        )

    def _generate_edit_image(
        self, output_dir: Path, payload: dict[str, Any], *, prompt: str,
        seed: int, source_path: Path, character_sheet_path: Path | None,
        output_prefix: str,
    ) -> GenerationResult:
        template = payload.get("edit_template", "flux_edit_api")
        required = {"prompt", "seed", "source_image", "output_prefix"}
        if character_sheet_path is not None:
            required.add("character_sheet")
        mapping = self._load_mapping(template, required)
        source_name = self._upload_image(source_path)
        values: dict[str, Any] = {
            "prompt": prompt,
            "seed": seed,
            "source_image": source_name,
            "output_prefix": output_prefix,
        }
        if "negative_prompt" in mapping:
            values["negative_prompt"] = payload.get("negative_prompt", "")
        # Flux Klein derives dimensions from the source image. Other edit graphs
        # may still expose explicit dimensions, so patch them only when mapped.
        if "width" in mapping:
            values["width"] = int(payload["width"])
        if "height" in mapping:
            values["height"] = int(payload["height"])
        if character_sheet_path is not None:
            values["character_sheet"] = self._upload_image(character_sheet_path)
        elif "character_sheet" in mapping:
            # An always-on two-image export otherwise keeps its sample placeholder.
            # Repeating the source is neutral and prevents unrelated reference leakage.
            values["character_sheet"] = source_name
        return self._run(
            template, {**payload, "patches": self._mapped_patches(mapping, values)}, output_dir
        )

    @staticmethod
    def _move_first(result: GenerationResult, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = result.files[0]
        target = target.with_suffix(source.suffix)
        shutil.move(str(source), target)
        return target

    @staticmethod
    def _style_directive(payload: dict[str, Any]) -> str:
        style = str(payload.get("visual_style", "")).strip()
        if not style:
            return ""
        directive = (
            f"MANDATORY VISUAL STYLE: {style}. Keep this exact rendering medium, palette, "
            "shading, line treatment, material language and level of stylization in every frame. "
        )
        stylized = (
            "cartoon", "anime", "illustrat", "comic", "cel", "clay", "stylized",
            "animation", "paint", "sketch", "pixel",
        )
        if any(token in style.lower() for token in stylized):
            directive += "Do not convert it to photorealistic, live-action or photographic imagery. "
        return directive

    @staticmethod
    def _style_negative(visual_style: str) -> str:
        base = "visual style drift, inconsistent rendering medium"
        stylized = (
            "cartoon", "anime", "illustrat", "comic", "cel", "clay", "stylized",
            "animation", "paint", "sketch", "pixel",
        )
        if any(token in visual_style.lower() for token in stylized):
            return f"{base}, photorealistic, live action, photographic skin, documentary footage"
        return base

    def generate_character_sheet(
        self, output_dir: Path, payload: dict[str, Any]
    ) -> GenerationResult:
        prompt = (
            f"{self._style_directive(payload)}{payload['prompt']}. Character reference sheet, "
            "front, three-quarter and profile "
            "views, consistent face, hair, wardrobe, proportions, neutral background, vertical 9:16."
        )
        result = self._generate_t2i_image(
            output_dir / "run", payload, prompt=prompt, seed=int(payload["seed"]),
            output_prefix=f"{payload.get('project_id', 'project')}-character-sheet",
        )
        target = self._move_first(result, output_dir / "character-sheet")
        return GenerationResult(files=[target], metadata={"run": result.metadata})

    def generate_keyframes(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        template = payload.get("template")
        if payload.get("patches") is not None:
            return self._run(template or "flux_t2i_api", payload, output_dir)
        if template:
            self._load_template(template)
        spec = payload["spec"]
        mode = payload.get("generation_mode", "anchor")
        candidates = int(payload.get("candidates", 2))
        base_seed = int(payload["seed"])
        style = str(payload.get("visual_style", "")).strip()
        style_directive = self._style_directive(payload)
        negative_parts = list(spec.get("negative_constraints", []))
        negative_parts.append(self._style_negative(style))
        common = {**payload, "negative_prompt": ", ".join(filter(None, negative_parts))}
        sheet = Path(payload["character_sheet_path"]) if payload.get("character_sheet_path") else None
        source_frames = {int(item["candidate"]): item for item in payload.get("source_frames", [])}
        files: list[Path] = []
        runs: list[dict[str, Any]] = []
        for candidate in range(1, candidates + 1):
            if mode == "anchor":
                start_prompt = (
                    f"{style_directive}{spec['narrative_purpose']}. {spec['start_composition']}. "
                    f"Vertical 9:16. {spec.get('safe_area', '')}"
                )
                start_result = self._generate_t2i_image(
                    output_dir / f"candidate-{candidate}-start-run", common,
                    prompt=start_prompt, seed=base_seed + candidate * 10,
                    output_prefix=f"{payload['scene_id']}-c{candidate}-start",
                )
                start_path = self._move_first(
                    start_result, output_dir / f"candidate-{candidate}-start"
                )
                files.append(start_path)
                runs.append({
                    "candidate": candidate, "role": "start",
                    "prompt": start_prompt,
                    "negative_prompt": common["negative_prompt"],
                    **start_result.metadata,
                })
            else:
                if candidate not in source_frames:
                    raise GenerationError(
                        "continuity_dependency_missing",
                        f"candidate {candidate} is missing its previous frame",
                    )
                start_path = Path(source_frames[candidate]["path"])
            edit_prompt = (
                f"{style_directive}Edit the source into the next keyframe without changing its "
                f"rendering style. {spec['narrative_purpose']}. "
                f"Target composition: {spec['end_composition']}. Motion progression: {spec['motion']}. "
                "Use the source image for immediate continuity and the character sheet for identity "
                "when supplied. Preserve face, "
                f"wardrobe, background and vertical framing. {spec.get('safe_area', '')}"
            )
            end_result = self._generate_edit_image(
                output_dir / f"candidate-{candidate}-end-run", common,
                prompt=edit_prompt, seed=base_seed + candidate * 10 + 1,
                source_path=start_path, character_sheet_path=sheet,
                output_prefix=f"{payload['scene_id']}-c{candidate}-end",
            )
            end_path = self._move_first(end_result, output_dir / f"candidate-{candidate}-end")
            files.append(end_path)
            runs.append({
                "candidate": candidate, "role": "end",
                "prompt": edit_prompt,
                "negative_prompt": common["negative_prompt"],
                "source_image": str(start_path),
                "character_sheet": str(sheet) if sheet else None,
                **end_result.metadata,
            })
        return GenerationResult(files=files, metadata={"runs": runs, "generation_mode": mode})

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
        style = str(payload.get("visual_style", "")).strip()
        action = payload.get("prompt_override") or (
            f"{spec['narrative_purpose']}. Motion: {spec['motion']}. "
            f"Camera: {spec.get('camera', 'stable camera')}."
        )
        negative_parts = list(spec.get("negative_constraints", []))
        negative_parts.append(self._style_negative(style))
        values = {
            "prompt": (
                f"{self._style_directive(payload)}{action} Preserve the exact visual style of both "
                "reference keyframes throughout the motion. Preserve identity, wardrobe, environment "
                "and vertical composition."
            ),
            "negative_prompt": ", ".join(filter(None, negative_parts)),
            "seed": int(payload["seed"]),
            "width": int(payload["width"]),
            "height": int(payload["height"]),
            "frames": int(payload["frames"]),
            "start_image": start_name,
            "end_image": end_name,
            "output_prefix": f"{payload['scene_id']}-attempt-{payload.get('attempt', 1)}",
        }
        result = self._run(
            template,
            {**payload, "patches": self._mapped_patches(mapping, values)},
            output_dir,
        )
        return GenerationResult(
            files=result.files,
            metadata={
                **result.metadata,
                "prompt_audit": {
                    "prompt": values["prompt"],
                    "negative_prompt": values["negative_prompt"],
                    "start_image": str(payload["start_image_path"]),
                    "end_image": str(payload["end_image_path"]),
                    "seed": values["seed"],
                    "frames": values["frames"],
                },
            },
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
