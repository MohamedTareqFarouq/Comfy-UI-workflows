from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .comfy import Generator
from .model_gateway import ModelGateway


class GpuCoordinator:
    """Serializes ComfyUI and managed llama.cpp ownership of one GPU."""

    def __init__(self, generator: Generator, model: ModelGateway, *, simulation: bool):
        self.generator = generator
        self.model = model
        self.simulation = simulation
        self._lock = threading.RLock()

    @contextmanager
    def qwen(self) -> Iterator[None]:
        with self._lock:
            if not self.simulation:
                self.generator.free_memory()
                self.model.ensure_available()
            try:
                yield
            finally:
                if not self.simulation:
                    self.model.unload()

    @contextmanager
    def comfy(self, *, reset_models: bool = True) -> Iterator[None]:
        with self._lock:
            if not self.simulation:
                self.model.unload()
                if reset_models:
                    self.generator.free_memory()
            yield

    def release_comfy(self) -> None:
        with self._lock:
            if not self.simulation:
                self.generator.free_memory()
