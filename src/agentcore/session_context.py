from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .interfaces import MemoryProvider
from .models import LeaseHandle, Message, SessionDefaults, UserInput
from .prompt_builder import PromptBuildRequest, SystemPromptBuilder, SystemPromptSnapshot
from .storage import PersistedSession, SQLiteStore
from tools.tools import ToolCatalog
from tools.toolsets import ToolsetRequest, ToolsetResolver, ToolsetSnapshot


class SessionContextError(RuntimeError):
    pass


class SessionSnapshotConflict(SessionContextError):
    pass


@dataclass(frozen=True, slots=True)
class SessionBuildRequest:
    profile: str
    surface: str
    platform: str
    config_version: str
    enabled_skills: tuple[str, ...] = ()
    enabled_toolsets: tuple[str, ...] = ("default",)
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
            permissions=defaults.permissions,
        )


@dataclass(frozen=True, slots=True)
class SessionRuntimeSnapshot:
    session_id: str
    build_request: SessionBuildRequest
    prompt: SystemPromptSnapshot
    toolset: ToolsetSnapshot
    created_at: float


@dataclass(frozen=True, slots=True)
class SessionContextResolution:
    snapshot: SessionRuntimeSnapshot
    created: bool
    warnings: tuple[str, ...] = ()


class SessionContextFactory:
    """Freeze prompt at bootstrap; resolve current tool bindings for every turn."""

    def __init__(
        self, *, store: SQLiteStore, prompt_builder: SystemPromptBuilder,
        toolset_resolver: ToolsetResolver, catalog: ToolCatalog,
        memory: MemoryProvider, defaults: SessionBuildRequest,
        memory_max_chars: int,
    ) -> None:
        self._store = store
        self._prompt_builder = prompt_builder
        self._toolset_resolver = toolset_resolver
        self._catalog = catalog
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
            raise SessionContextError("cannot recover a missing session")
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
        toolset = self._resolve_tools(request)
        persisted = self._store.create_session(
            session_id, profile=request.profile, surface=request.surface,
            platform=request.platform, system_prompt_bytes=prompt.content_bytes,
            user_message=Message.user(user_input.text, metadata=user_input.metadata),
            fence=lease.fence,
        )
        return SessionContextResolution(
            SessionRuntimeSnapshot(session_id, request, prompt, toolset,
                                   persisted.created_at),
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
            permissions=current.permissions,
        )
        prompt_bytes = persisted.system_prompt_bytes
        prompt = SystemPromptSnapshot(
            text=prompt_bytes.decode("utf-8"), content_bytes=prompt_bytes,
            fingerprint=hashlib.sha256(prompt_bytes).hexdigest(),
        )
        return SessionContextResolution(
            SessionRuntimeSnapshot(
                persisted.session_id, effective, prompt,
                self._resolve_tools(effective), persisted.created_at,
            ),
            False,
        )

    def _resolve_tools(self, request: SessionBuildRequest) -> ToolsetSnapshot:
        return self._toolset_resolver.resolve(ToolsetRequest(
            profile=request.profile, surface=request.surface,
            platform=request.platform,
            enabled_toolsets=request.enabled_toolsets,
            permissions=request.permissions,
        ), self._catalog)


__all__ = [
    "SessionBuildRequest", "SessionContextError", "SessionContextFactory",
    "SessionContextResolution", "SessionRuntimeSnapshot", "SessionSnapshotConflict",
]
