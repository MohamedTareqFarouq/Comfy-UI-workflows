from __future__ import annotations

import json
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(RuntimeError):
    pass


class ModelGateway:
    """Narrow, non-agentic interface to a local OpenAI-compatible llama.cpp server."""

    def __init__(self, base_url: str, model: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=3)
            return {"available": response.is_success, "status": response.status_code}
        except httpx.HTTPError as exc:
            return {"available": False, "error": str(exc)}

    def generate_structured(
        self,
        output_type: type[T],
        *,
        system: str,
        context: str,
        max_tokens: int = 4096,
    ) -> T:
        schema = output_type.model_json_schema()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": context},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "schema": schema},
        }
        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return output_type.model_validate(json.loads(content))
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ModelUnavailable(str(exc)) from exc

    def analyze_images(
        self,
        output_type: type[T],
        *,
        system: str,
        prompt: str,
        image_urls: list[str],
    ) -> T:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
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
            "max_tokens": 2048,
            "response_format": {"type": "json_schema", "schema": output_type.model_json_schema()},
        }
        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions", json=payload, timeout=self.timeout
            )
            response.raise_for_status()
            value = response.json()["choices"][0]["message"]["content"]
            return output_type.model_validate_json(value)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ModelUnavailable(str(exc)) from exc

    def unload(self) -> None:
        # llama.cpp may not expose model unloading in all builds. Stopping the
        # supervised process is the authoritative unload operation.
        return None
