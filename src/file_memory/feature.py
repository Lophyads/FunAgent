"""Composition and lifecycle for the optional file-memory feature."""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agentcore.background_review import BackgroundReviewScheduler
from agentcore.interfaces import NoopReviewScheduler
from agentcore.models import AgentConfig
from agentcore.prompt_builder import StaticSystemPromptBuilder
from tools.registry import ToolRegistry
from tools.runtime import get_tool_services

from .prompt import FileMemoryPromptBuilder
from .review import MemoryReviewRunner, MemoryReviewScheduler
from .store import FileMemoryStore
from .tool import memory_entry


@dataclass(frozen=True, slots=True)
class FileMemoryConfig:
    enabled: bool = False
    directory: Path = Path("memories")
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    review_every_turns: int = 10

    def __post_init__(self) -> None:
        if self.memory_char_limit < 1 or self.user_char_limit < 1 or self.review_every_turns < 0:
            raise ValueError("invalid [file_memory] limits or review cadence")


def load_file_memory_config(path: str | Path) -> FileMemoryConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle).get("file_memory", {})
    if not isinstance(raw, dict):
        raise ValueError("[file_memory] must be a TOML table")
    unknown = set(raw) - {"enabled", "directory", "memory_char_limit", "user_char_limit", "review_every_turns"}
    if unknown:
        raise ValueError(f"unknown [file_memory] options: {', '.join(sorted(unknown))}")
    if "directory" in raw:
        raw["directory"] = Path(raw["directory"])
    config = FileMemoryConfig(**raw)
    if not config.directory.is_absolute():
        config = FileMemoryConfig(
            enabled=config.enabled, directory=(config_path.parent / config.directory).absolute(),
            memory_char_limit=config.memory_char_limit,
            user_char_limit=config.user_char_limit,
            review_every_turns=config.review_every_turns,
        )
    return config


class FileMemoryFeature:
    def __init__(self, settings: FileMemoryConfig, agent_config: AgentConfig) -> None:
        self.settings = settings
        self.store = FileMemoryStore(
            settings.directory, memory_char_limit=settings.memory_char_limit,
            user_char_limit=settings.user_char_limit,
        ) if settings.enabled else None
        self.prompt_builder = (
            FileMemoryPromptBuilder(StaticSystemPromptBuilder(agent_config.system_prompt),
                                    self.store, agent_config.memory_max_chars)
            if self.store is not None else None
        )
        self.runner = None
        self.queue = None
        self.review_scheduler = NoopReviewScheduler()
        self._installed = False
        if self.store is not None and settings.review_every_turns:
            self.runner = MemoryReviewRunner(agent_config, self.store)
            self.queue = BackgroundReviewScheduler(self.runner, workers=1, capacity=32)
            self.review_scheduler = MemoryReviewScheduler(self.queue, settings.review_every_turns)

    def install_tool(self, registry: ToolRegistry) -> None:
        if self.store is not None and not self._installed:
            services = get_tool_services()
            services.protect_path(self.store.directory)
            try:
                registry.replace_all((*registry.entries(), memory_entry(self.store)))
            except BaseException:
                services.unprotect_path(self.store.directory)
                raise
            self._installed = True

    def close(self, timeout: float = 5) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        if self.queue is not None and not self.queue.close(timeout=max(0, deadline - time.monotonic())):
            return False
        stopped = self.runner is None or self.runner.close(max(0, deadline - time.monotonic()))
        if stopped and self._installed:
            get_tool_services().unprotect_path(self.store.directory)
            self._installed = False
        return stopped
