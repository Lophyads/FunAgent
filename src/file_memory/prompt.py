"""Inject file memory only while a new session prompt is being built."""

from __future__ import annotations

from dataclasses import replace

from agentcore.prompt_builder import PromptBuildRequest, SystemPromptBuilder, SystemPromptSnapshot

from .store import FileMemoryStore


class FileMemoryPromptBuilder:
    def __init__(self, base: SystemPromptBuilder, store: FileMemoryStore, max_chars: int) -> None:
        self.base = base
        self.store = store
        self.max_chars = max_chars

    def build(self, request: PromptBuildRequest) -> SystemPromptSnapshot:
        available = max(0, self.max_chars - sum(map(len, request.bootstrap_memory)))
        blocks = self.store.read_prompt_blocks(available)
        return self.base.build(replace(
            request, bootstrap_memory=(*request.bootstrap_memory, *blocks),
        ))
