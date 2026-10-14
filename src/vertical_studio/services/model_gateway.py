from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(RuntimeError):
    pass


class ModelGateway:
    """Narrow, non-agentic interface to a local OpenAI-compatible llama.cpp server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 600.0,
        *,
        managed: bool = False,
        executable: str = "llama",
        hf_repo: str = "Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M",
        gpu_layers: int = 99,
        context_size: int = 8192,
        start_timeout: int = 300,
        log_path: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.managed = managed
        self.executable = executable
        self.hf_repo = hf_repo
        self.gpu_layers = gpu_layers
        self.context_size = context_size
        self.start_timeout = start_timeout
        self.log_path = log_path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._log_stream: Any = None
        self.last_trace: dict[str, Any] | None = None

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=3)
            return {"available": response.is_success, "status": response.status_code}
        except httpx.HTTPError as exc:
            return {"available": False, "error": str(exc)}

    @property
    def owned(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def ensure_available(self) -> None:
        with self._lock:
            if self.health().get("available"):
                if self.managed and not self.owned:
                    raise ModelUnavailable(
                        "Qwen is already running externally; stop it so Vertical Studio "
                        "can manage GPU loading and unloading"
                    )
                return
            if not self.managed:
                raise ModelUnavailable(f"Qwen is unavailable at {self.base_url}")
            if self._process is not None and self._process.poll() is not None:
                self._close_log()
                self._process = None
            command = [
                self.executable,
                "serve",
                "-hf",
                self.hf_repo,
                "--alias",
                self.model,
                "--host",
                "127.0.0.1",
                "--port",
                self.base_url.rsplit(":", 1)[-1],
                "--n-gpu-layers",
                str(self.gpu_layers),
                "--ctx-size",
                str(self.context_size),
                "--parallel",
                "1",
            ]
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_stream = self.log_path.open("ab")
            try:
                self._process = subprocess.Popen(
                    command,
                    stdout=self._log_stream or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                self._close_log()
                raise ModelUnavailable(f"could not start managed Qwen: {exc}") from exc
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    code = self._process.returncode
                    self._process = None
                    self._close_log()
                    raise ModelUnavailable(
                        f"managed Qwen exited during startup with code {code}"
                    )
                if self.health().get("available"):
                    return
                time.sleep(0.5)
            self.unload()
            raise ModelUnavailable(
                "managed Qwen did not become healthy before the startup timeout"
            )

    def generate_structured(
        self,
        output_type: type[T],
        *,
        system: str,
        context: str,
        max_tokens: int = 4096,
    ) -> T:
        self.ensure_available()
        schema = output_type.model_json_schema()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        system_with_schema = (
            system.rstrip()
            + "\n\nYou MUST return exactly one JSON object that validates against the "
            "JSON Schema below. Use the schema's field names exactly; do not invent "
            "wrappers or rename fields. Return JSON only, without Markdown.\n"
            + schema_text
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_with_schema},
                {"role": "user", "content": context},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "schema": schema},
        }
        base_messages = list(payload["messages"])
        last_error: Exception | None = None
        last_content = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
                self._raise_for_status_with_detail(response)
                last_content = response.json()["choices"][0]["message"]["content"]
                parsed = self._parse_structured(output_type, last_content)
                self.last_trace = {
                    "model": self.model,
                    "request": payload,
                    "raw_response": last_content,
                    "parsed_response": parsed.model_dump(mode="json"),
                    "structured_attempt": attempt + 1,
                }
                return parsed
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt < 2 and last_content:
                    economy = (
                        " On this final attempt, keep every string concise, do not repeat list "
                        "items or phrases, and finish the complete JSON object."
                        if attempt == 1 else ""
                    )
                    payload["messages"] = [
                        *base_messages,
                        {
                            "role": "user",
                            "content": (
                                "Regenerate the complete object from scratch as strict JSON. "
                                "The previous response was discarded because it was malformed or "
                                "failed schema validation. Do not add Markdown, commentary, a "
                                "wrapper object, trailing commas, or unescaped line breaks. The "
                                "JSON Schema is already present in the system message."
                                + economy
                                + "\nValidation error:\n"
                                + str(exc)[:1200]
                            ),
                        },
                    ]
                    payload["temperature"] = 0
                    last_content = ""
                    continue
                break
        self._record_invalid_response(last_content, last_error)
        raise ModelUnavailable(f"invalid structured model response: {last_error}") from last_error

    @staticmethod
    def _parse_structured(output_type: type[T], content: str) -> T:
        value = content.strip()
        if value.startswith("```"):
            lines = value.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
        try:
            parsed = json.loads(value)
        except ValueError:
            start, end = value.find("{"), value.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(value[start : end + 1])
        if isinstance(parsed, dict) and len(parsed) == 1:
            key, wrapped = next(iter(parsed.items()))
            schema_title = str(output_type.model_json_schema().get("title", ""))
            accepted = {output_type.__name__.casefold(), schema_title.casefold()}
            if key.casefold() in accepted and isinstance(wrapped, dict):
                parsed = wrapped
        return output_type.model_validate(parsed)

    def _record_invalid_response(
        self, content: str, error: Exception | None
    ) -> None:
        if not self.log_path:
            return
        target = self.log_path.with_name("qwen-invalid-response.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"error": str(error), "content": content}, indent=2)
        )

    @staticmethod
    def _raise_for_status_with_detail(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            message = f"Qwen request failed with HTTP {response.status_code}"
            if detail:
                message += f": {detail[:2000]}"
            raise ModelUnavailable(message) from exc

    def analyze_images(
        self,
        output_type: type[T],
        *,
        system: str,
        prompt: str,
        image_urls: list[str],
        max_tokens: int = 2048,
    ) -> T:
        self.ensure_available()
        schema = output_type.model_json_schema()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        constrained_prompt = (
            prompt.rstrip()
            + "\n\nReturn exactly one JSON object using the schema's field names. "
            "Do not add a wrapper or Markdown. Exact JSON Schema:\n"
            + schema_text
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": constrained_prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": image_url}}
            for image_url in image_urls
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "schema": schema},
        }
        base_messages = list(payload["messages"])
        last_error: Exception | None = None
        last_content = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    timeout=self.timeout,
                )
                self._raise_for_status_with_detail(response)
                last_content = response.json()["choices"][0]["message"]["content"]
                parsed = self._parse_structured(output_type, last_content)
                self.last_trace = {
                    "model": self.model,
                    "request": payload,
                    "raw_response": last_content,
                    "parsed_response": parsed.model_dump(mode="json"),
                    "structured_attempt": attempt + 1,
                    "image_count": len(image_urls),
                }
                return parsed
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                economy = (
                    " Keep every string and list concise. Omit optional detail rather than "
                    "repeating information, and finish the complete JSON object within the "
                    "output limit."
                    if attempt == 1 else ""
                )
                payload["messages"] = [
                    *base_messages,
                    {
                        "role": "user",
                        "content": (
                            "Regenerate the complete object from scratch as strict JSON. "
                            "The previous response was discarded because it was malformed, "
                            "truncated, or failed schema validation. Do not add Markdown, "
                            "commentary, wrapper objects, trailing commas, or unescaped line "
                            "breaks. The JSON Schema is already in the first user message."
                            + economy
                            + "\nValidation error:\n"
                            + str(exc)[:1200]
                        ),
                    },
                ]
                payload["temperature"] = 0
                last_content = ""
        self._record_invalid_response(last_content, last_error)
        raise ModelUnavailable(
            f"invalid structured image-analysis response after 3 attempts: {last_error}"
        ) from last_error

    def unload(self) -> None:
        with self._lock:
            if not self.owned:
                return
            assert self._process is not None
            self._process.terminate()
            try:
                self._process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
            self._process = None
            self._close_log()

    def _close_log(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None
