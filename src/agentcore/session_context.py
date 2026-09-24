from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from .interfaces import MemoryProvider
from .models import LeaseHandle, Message, SessionDefaults, UserInput
from .prompt_builder import PromptBuildRequest, SystemPromptBuilder, SystemPromptSnapshot
from .storage import PersistedSession, SQLiteStore
from tools.model_tools import load_tools
from tools.registry import ToolEntry, ToolRegistry
from tools.toolsets import ToolsetRequest, ToolsetResolver


class SessionContextError(RuntimeError):
    pass


class SessionSnapshotConflict(SessionContextError):
    pass


class RecoverySessionMissing(SessionContextError):
    pass


@dataclass(frozen=True, slots=True)
class SessionBuildRequest:
    profile: str
    surface: str
    platform: str
    config_version: str
    enabled_skills: tuple[str, ...] = ()
    enabled_toolsets: tuple[str, ...] = ("default",)
    disabled_toolsets: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("profile", "surface", "platform", "config_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")

    @classmethod
    def from_defaults(cls, defaults: SessionDefaults) -> "SessionBuildRequest":
        return cls(
            profile=defaults.profile, surface=defaults.surface,
            platform=defaults.platform, config_version=defaults.config_version,
            enabled_skills=defaults.enabled_skills,
            enabled_toolsets=defaults.enabled_toolsets,
            disabled_toolsets=defaults.disabled_toolsets,
            permissions=defaults.permissions,
        )


@dataclass(frozen=True, slots=True)
class SessionRuntimeSnapshot:
    session_id: str
    build_request: SessionBuildRequest
    prompt: SystemPromptSnapshot
    tools: tuple[Mapping[str, Any], ...]
    valid_tool_names: frozenset[str]
    entries_by_name: Mapping[str, ToolEntry[Any]]
    tool_schema_bytes: bytes
    created_at: float


@dataclass(frozen=True, slots=True)
class SessionContextResolution:
    snapshot: SessionRuntimeSnapshot
    created: bool
    warnings: tuple[str, ...] = ()


class SessionContextFactory:
    """Freeze the prompt and load one consistent tool view for each turn."""

    def __init__(
        self, *, store: SQLiteStore, prompt_builder: SystemPromptBuilder,
        toolset_resolver: ToolsetResolver, registry: ToolRegistry,
        memory: MemoryProvider, defaults: SessionBuildRequest,
        memory_max_chars: int,
    ) -> None:
        self._store = store
        self._prompt_builder = prompt_builder
        self._toolset_resolver = toolset_resolver
        self._registry = registry
        self._memory = memory
        self._defaults = defaults
        self._memory_max_chars = memory_max_chars

    def create_or_load(
        self, session_id: str, user_input: UserInput,
        request: SessionBuildRequest | None, lease: LeaseHandle,
    ) -> SessionContextResolution:
        persisted = self._store.load_session(session_id)
        if persisted is None:
            return self._create(session_id, user_input, request or self._defaults, lease)
        return self._load(persisted, request)

    def load_existing(
        self, session_id: str, lease: LeaseHandle,
    ) -> SessionContextResolution:
        persisted = self._store.load_session(session_id)
        if persisted is None:
            raise RecoverySessionMissing("cannot recover a missing session")
        return self._load(persisted, None)

    def _create(
        self, session_id: str, user_input: UserInput,
        request: SessionBuildRequest, lease: LeaseHandle,
    ) -> SessionContextResolution:
        warnings: list[str] = []
        try:
            memory_items = self._memory.recall(user_input, session_id)
        except BaseException as exc:
            memory_items = ()
            warnings.append(f"memory bootstrap failed: {type(exc).__name__}: {exc}")
        remaining = self._memory_max_chars
        bootstrap: list[str] = []
        try:
            for item in memory_items:
                if remaining <= 0:
                    break
                content = item.content[:remaining]
                bootstrap.append(content)
                remaining -= len(content)
        except BaseException as exc:
            bootstrap.clear()
            warnings.append(f"memory bootstrap data was ignored: {type(exc).__name__}: {exc}")
        prompt = self._prompt_builder.build(PromptBuildRequest(
            profile=request.profile, surface=request.surface,
            platform=request.platform, config_version=request.config_version,
            enabled_skills=request.enabled_skills,
            bootstrap_memory=tuple(bootstrap),
        ))
        if (prompt.content_bytes != prompt.text.encode("utf-8") or
            hashlib.sha256(prompt.content_bytes).hexdigest() != prompt.fingerprint):
            raise SessionContextError("prompt builder returned inconsistent bytes")
        tools, names, entries, schema_bytes = self._load_tools(request)
        persisted = self._store.create_session(
            session_id, profile=request.profile, surface=request.surface,
            platform=request.platform, system_prompt_bytes=prompt.content_bytes,
            user_message=Message.user(user_input.text, metadata=user_input.metadata),
            fence=lease.fence,
        )
        return SessionContextResolution(
            SessionRuntimeSnapshot(
                session_id=session_id,
                build_request=request,
                prompt=prompt,
                tools=tools,
                valid_tool_names=names,
                entries_by_name=entries,
                tool_schema_bytes=schema_bytes,
                created_at=persisted.created_at,
            ),
            True, tuple(warnings),
        )

    def _load(
        self, persisted: PersistedSession, request: SessionBuildRequest | None,
    ) -> SessionContextResolution:
        current = request or self._defaults
        identity = (persisted.profile, persisted.surface, persisted.platform)
        if request is not None and (
            request.profile, request.surface, request.platform
        ) != identity:
            raise SessionSnapshotConflict(
                "explicit session identity differs from the persisted session"
            )
        effective = SessionBuildRequest(
            profile=persisted.profile, surface=persisted.surface,
            platform=persisted.platform, config_version=current.config_version,
            enabled_skills=current.enabled_skills,
            enabled_toolsets=current.enabled_toolsets,
            disabled_toolsets=current.disabled_toolsets,
            permissions=current.permissions,
        )
        prompt_bytes = persisted.system_prompt_bytes
        prompt = SystemPromptSnapshot(
            text=prompt_bytes.decode("utf-8"), content_bytes=prompt_bytes,
            fingerprint=hashlib.sha256(prompt_bytes).hexdigest(),
        )
        return SessionContextResolution(
            self._runtime_snapshot(persisted, effective, prompt),
            False,
        )

    def _runtime_snapshot(
        self,
        persisted: PersistedSession,
        request: SessionBuildRequest,
        prompt: SystemPromptSnapshot,
    ) -> SessionRuntimeSnapshot:
        tools, names, entries, schema_bytes = self._load_tools(request)
        return SessionRuntimeSnapshot(
            session_id=persisted.session_id,
            build_request=request,
            prompt=prompt,
            tools=tools,
            valid_tool_names=names,
            entries_by_name=entries,
            tool_schema_bytes=schema_bytes,
            created_at=persisted.created_at,
        )

    def _load_tools(
        self, request: SessionBuildRequest,
    ) -> tuple[
        tuple[Mapping[str, Any], ...],
        frozenset[str],
        Mapping[str, ToolEntry[Any]],
        bytes,
    ]:
        return load_tools(ToolsetRequest(
            profile=request.profile, surface=request.surface,
            platform=request.platform,
            enabled_toolsets=request.enabled_toolsets,
            disabled_toolsets=request.disabled_toolsets,
            permissions=request.permissions,
        ), self._registry, self._toolset_resolver)


__all__ = [
    "SessionBuildRequest", "SessionContextError", "SessionContextFactory",
    "SessionContextResolution", "SessionRuntimeSnapshot", "SessionSnapshotConflict",
    "RecoverySessionMissing",
]
