from __future__ import annotations

import hashlib
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
    def generate_reference_asset(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult: ...
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
        self._active_prompt_id: str | None = None

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

    @staticmethod
    def _upload_filename(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"{path.stem}-{digest.hexdigest()[:16]}{path.suffix}"

    def _upload_image(self, path: Path) -> str:
        try:
            upload_name = self._upload_filename(path)
            with path.open("rb") as stream:
                response = httpx.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (upload_name, stream, "application/octet-stream")},
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
            self._active_prompt_id = prompt_id
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
        self._active_prompt_id = None
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
        seed: int, source_path: Path, reference_path: Path | None,
        output_prefix: str,
    ) -> GenerationResult:
        template = (
            payload.get("edit_template", "flux_edit_api")
            if reference_path is not None
            else payload.get(
                "single_edit_template",
                "image_flux2_klein_image_edit_4b_distilled_api",
            )
        )
        required = {"prompt", "seed", "source_image", "output_prefix"}
        if reference_path is not None:
            required.add("reference_image")
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
        if reference_path is not None:
            values["reference_image"] = self._upload_image(reference_path)
        result = self._run(
            template, {**payload, "patches": self._mapped_patches(mapping, values)}, output_dir
        )
        references = [
            {"role": "source", "path": str(source_path)},
            *(
                [{"role": "canonical", "path": str(reference_path)}]
                if reference_path is not None else []
            ),
        ]
        return GenerationResult(
            files=result.files,
            metadata={**result.metadata, "prompt": prompt, "references": references},
        )

    def _generate_multiref_image(
        self, output_dir: Path, payload: dict[str, Any], *, prompt: str,
        seed: int, references: list[dict[str, Any]], output_prefix: str,
    ) -> GenerationResult:
        if not 1 <= len(references) <= 4:
            raise GenerationError(
                "reference_count_invalid", "Klein multi-reference requires 1-4 images"
            )
        prefix = payload.get("multiref_template_prefix", "flux_klein_multiref")
        template = f"{prefix}_{len(references)}_api"
        required = {"prompt", "seed", "width", "height", "output_prefix"}
        required.update(f"reference_{index}" for index in range(1, len(references) + 1))
        mapping = self._load_mapping(template, required)
        values: dict[str, Any] = {
            "prompt": prompt,
            "seed": seed,
            "width": int(payload["width"]),
            "height": int(payload["height"]),
            "output_prefix": output_prefix,
        }
        audit_references: list[dict[str, Any]] = []
        for index, reference in enumerate(references, 1):
            path = Path(reference["path"])
            values[f"reference_{index}"] = self._upload_image(path)
            audit_references.append({
                key: reference.get(key)
                for key in ("asset_id", "artifact_id", "key", "role", "label", "path")
            })
        result = self._run(
            template, {**payload, "patches": self._mapped_patches(mapping, values)}, output_dir
        )
        return GenerationResult(
            files=result.files,
            metadata={**result.metadata, "prompt": prompt, "references": audit_references},
        )

    @staticmethod
    def _reference_directive(references: list[dict[str, Any]]) -> str:
        assignments = []
        for index, reference in enumerate(references, 1):
            label = (
                reference.get("label") or reference.get("key")
                or reference.get("role") or "reference"
            )
            role = reference.get("role", "reference")
            assignments.append(f"image {index} is {label} ({role})")
        return "Reference assignments: " + "; ".join(assignments) + ". "

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

    def generate_reference_asset(
        self, output_dir: Path, payload: dict[str, Any]
    ) -> GenerationResult:
        role = str(payload.get("asset_role", "reference"))
        suffix = {
            "character": (
                "One person only, clean three-quarter identity view, neutral pose, "
                "plain background, no collage or reference-sheet layout."
            ),
            "environment": "Environment only, no people, stable architecture, palette and lighting.",
            "prop": "One isolated prop only, no people or hands, clean readable silhouette.",
            "style": "A clean style exemplar with a coherent palette, materials and lighting language.",
        }.get(role, "One clean reusable visual reference.")
        references = list(payload.get("reference_frames", []))[:2]
        visual_lock = (
            "Use the supplied source evidence images as the primary visual truth for identity, "
            "shape, wardrobe, architecture, palette, materials, and lighting. Extract only the "
            "requested asset; do not merge unrelated people, objects, or locations from the images. "
            f"{self._reference_directive(references)}"
            if references else ""
        )
        prompt = (
            f"{self._style_directive(payload)}{visual_lock}{payload['prompt']}. {suffix}"
        )
        output_prefix = (
            f"{payload.get('project_id', 'project')}-{payload.get('asset_key', role)}"
        )
        if references:
            result = self._generate_edit_image(
                output_dir / "run", payload, prompt=prompt, seed=int(payload["seed"]),
                source_path=Path(references[0]["path"]),
                reference_path=(
                    Path(references[1]["path"]) if len(references) == 2 else None
                ),
                output_prefix=output_prefix,
            )
        else:
            result = self._generate_t2i_image(
                output_dir / "run", payload, prompt=prompt, seed=int(payload["seed"]),
                output_prefix=output_prefix,
            )
        target = self._move_first(
            result, output_dir / f"canonical-{payload.get('asset_key', role)}"
        )
        return GenerationResult(
            files=[target], metadata={"run": result.metadata, "prompt": prompt}
        )

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
        candidates = int(payload.get("candidates", 1))
        base_seed = int(payload["seed"])
        style = str(payload.get("visual_style", "")).strip()
        style_directive = self._style_directive(payload)
        negative_parts = list(spec.get("negative_constraints", []))
        negative_parts.append(self._style_negative(style))
        resolved_negative = payload.get("negative_prompt_override")
        if resolved_negative is None:
            resolved_negative = ", ".join(filter(None, negative_parts))
        common = {**payload, "negative_prompt": resolved_negative}
        canonical = list(payload.get("canonical_references", []))[:2]
        opening_references = list(
            payload.get(
                "opening_reference_frames",
                payload.get("opening_canonical_references", canonical),
            )
        )[:2]
        closing_references = list(
            payload.get(
                "closing_reference_frames",
                payload.get("closing_canonical_references", canonical),
            )
        )[:1]
        legacy_sheet = (
            Path(payload["character_sheet_path"])
            if payload.get("character_sheet_path") else None
        )
        source_frames = {
            int(item["candidate"]): item for item in payload.get("source_frames", [])
        }
        character_count = spec.get("character_count")
        count_rule = (
            f"Exactly {character_count} people appear in the image. "
            if character_count is not None else ""
        )
        state_rule = " ".join(filter(None, [
            spec.get("opening_state", ""),
            (
                f"Object ownership: {spec.get('object_owner')}."
                if spec.get("object_owner") else ""
            ),
        ]))
        files: list[Path] = []
        runs: list[dict[str, Any]] = []
        for candidate in range(1, candidates + 1):
            if mode == "anchor":
                reference_rule = (
                    self._reference_directive(opening_references)
                    if opening_references else ""
                )
                start_prompt = payload.get("start_prompt_override") or (
                    f"{style_directive}{count_rule}{reference_rule}"
                    "Create one coherent vertical scene, not a collage or reference sheet. "
                    f"{spec['narrative_purpose']}. Opening composition: {spec['start_composition']}. "
                    f"{state_rule} Preserve every referenced character identity, age, gender, "
                    "face, hair and wardrobe; preserve the referenced environment architecture "
                    "and lighting. Use only the opening environment lock; do not import architecture "
                    "or furniture from a different room. No duplicate or hybrid characters. "
                    f"Opening environment lock: {spec.get('opening_environment_key') or spec.get('environment_key') or 'unspecified'}. "
                    f"{spec.get('safe_area', '')}"
                )
                if opening_references:
                    start_result = self._generate_edit_image(
                        output_dir / f"candidate-{candidate}-start-run", common,
                        prompt=start_prompt, seed=base_seed + candidate * 10,
                        source_path=Path(opening_references[0]["path"]),
                        reference_path=(
                            Path(opening_references[1]["path"])
                            if len(opening_references) > 1 else None
                        ),
                        output_prefix=f"{payload['scene_id']}-c{candidate}-start",
                    )
                else:
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
                    "candidate": candidate, "role": "start", "prompt": start_prompt,
                    "negative_prompt": common["negative_prompt"], **start_result.metadata,
                })
            else:
                if candidate not in source_frames:
                    raise GenerationError(
                        "continuity_dependency_missing",
                        f"candidate {candidate} is missing its previous frame",
                    )
                start_path = Path(source_frames[candidate]["path"])

            continuity_reference = {
                "key": "opening_frame", "role": "continuity",
                "label": "the exact opening frame", "path": str(start_path),
            }
            end_references = [continuity_reference, *closing_references]
            closing_state = spec.get("closing_state") or spec["end_composition"]
            end_prompt = payload.get("end_prompt_override") or (
                f"{style_directive}{count_rule}{self._reference_directive(end_references)}"
                "Create the closing keyframe of the same shot. Image 1 controls immediate "
                f"composition and continuity. {spec['narrative_purpose']}. "
                f"Closing composition: {spec['end_composition']}. Closing state: {closing_state}. "
                f"Motion progression: {spec['motion']}. Preserve identity, gender, age, wardrobe, "
                "environment and object ownership. The closing environment lock controls the final "
                "background; do not retain furniture or architecture from the opening room when the "
                f"lock changes. Closing environment lock: {spec.get('closing_environment_key') or spec.get('opening_environment_key') or spec.get('environment_key') or 'unspecified'}. "
                "No duplicate or hybrid characters, no collage, and no additional people."
            )
            selected_reference = (
                Path(closing_references[0]["path"])
                if closing_references else legacy_sheet
            )
            end_result = self._generate_edit_image(
                output_dir / f"candidate-{candidate}-end-run", common,
                prompt=end_prompt, seed=base_seed + candidate * 10 + 1,
                source_path=start_path, reference_path=selected_reference,
                output_prefix=f"{payload['scene_id']}-c{candidate}-end",
            )
            end_path = self._move_first(
                end_result, output_dir / f"candidate-{candidate}-end"
            )
            files.append(end_path)
            runs.append({
                "candidate": candidate, "role": "end", "prompt": end_prompt,
                "negative_prompt": common["negative_prompt"],
                "source_image": str(start_path), **end_result.metadata,
            })
        return GenerationResult(
            files=files,
            metadata={
                "runs": runs, "generation_mode": mode,
                "canonical_reference_count": len(canonical),
                "opening_reference_count": len(opening_references),
                "closing_reference_count": len(closing_references),
                "reference_selection": "user_selectable",
                "opening_environment_key": spec.get("opening_environment_key") or spec.get("environment_key"),
                "closing_environment_key": spec.get("closing_environment_key") or spec.get("opening_environment_key") or spec.get("environment_key"),
            },
        )

    def generate_video(self, output_dir: Path, payload: dict[str, Any]) -> GenerationResult:
        template = payload.get("template", "wan14b_lightx2v_flf2v_api")
        if payload.get("patches") is not None:
            return self._run(template, payload, output_dir)
        workflow = self._load_template(template)

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
        start_pointer = mapping["start_image"]
        end_pointer = mapping["end_image"]
        if isinstance(start_pointer, list):
            start_pointer = start_pointer[0]
        if isinstance(end_pointer, list):
            end_pointer = end_pointer[0]
        start_binding = (str(start_pointer["node_id"]), start_pointer["input"])
        end_binding = (str(end_pointer["node_id"]), end_pointer["input"])
        if start_binding == end_binding:
            raise GenerationError("workflow_mapping_invalid", "start and end images map to the same Comfy input")
        if workflow:
            try:
                start_node = workflow[start_binding[0]]
                end_node = workflow[end_binding[0]]
                flf_nodes = [
                    node for node in workflow.values()
                    if node.get("class_type") == "WanFirstLastFrameToVideo"
                ]
                correctly_connected = any(
                    node.get("inputs", {}).get("start_image", [None])[0] == start_binding[0]
                    and node.get("inputs", {}).get("end_image", [None])[0] == end_binding[0]
                    for node in flf_nodes
                )
            except (KeyError, TypeError, IndexError) as exc:
                raise GenerationError("workflow_mapping_invalid", "invalid Wan start/end node mapping") from exc
            if (
                start_node.get("class_type") != "LoadImage"
                or end_node.get("class_type") != "LoadImage"
                or not correctly_connected
            ):
                raise GenerationError(
                    "workflow_mapping_invalid",
                    "mapped start/end LoadImage nodes are not connected to WanFirstLastFrameToVideo in order",
                )
        spec = payload["spec"]
        start_name = self._upload_image(Path(payload["start_image_path"]))
        end_name = self._upload_image(Path(payload["end_image_path"]))
        style = str(payload.get("visual_style", "")).strip()
        prompt_override = payload.get("prompt_override")
        action = (
            f"{spec['narrative_purpose']}. Motion: {spec['motion']}. "
            f"Camera: {spec.get('camera', 'stable camera')}."
        )
        negative_parts = list(spec.get("negative_constraints", []))
        negative_parts.append(self._style_negative(style))
        negative_prompt = payload.get("negative_prompt_override")
        if negative_prompt is None:
            negative_prompt = ", ".join(filter(None, negative_parts))
        values = {
            "prompt": prompt_override or (
                f"{self._style_directive(payload)}{action} Preserve the exact visual style of both "
                "reference keyframes throughout the motion. Preserve identity, wardrobe, environment "
                "and vertical composition."
            ),
            "negative_prompt": negative_prompt,
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
                    "start_artifact_id": payload.get("start_artifact_id"),
                    "end_artifact_id": payload.get("end_artifact_id"),
                    "keyframe_binding": payload.get("keyframe_binding", {}),
                    "comfy_inputs": {
                        "start": {"uploaded_name": start_name, "node_id": start_binding[0], "input": start_binding[1]},
                        "end": {"uploaded_name": end_name, "node_id": end_binding[0], "input": end_binding[1]},
                    },
                    "seed": values["seed"],
                    "frames": values["frames"],
                },
            },
        )

    def interrupt(self) -> None:
        # Never send an unscoped global interrupt. It can cancel an unrelated
        # prompt and ComfyUI logs it as "Global interrupt (no prompt_id specified)".
        prompt_id = self._active_prompt_id
        if not prompt_id:
            return
        try:
            httpx.post(
                f"{self.base_url}/interrupt",
                json={"prompt_id": prompt_id},
                timeout=10,
            )
        except httpx.HTTPError:
            pass
        finally:
            if self._active_prompt_id == prompt_id:
                self._active_prompt_id = None

    def free_memory(self) -> None:
        try:
            httpx.post(
                f"{self.base_url}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=30,
            )
        except httpx.HTTPError:
            pass
