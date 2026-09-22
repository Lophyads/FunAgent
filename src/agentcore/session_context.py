from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Mapping

from .interfaces import MemoryProvider
from .models import LeaseHandle, Message, SessionDefaults, UserInput
from .prompt_builder import (
    PromptBuildRequest,
    SystemPromptBuilder,
    SystemPromptSnapshot,
)
from .storage import PersistedSessionContext, SQLiteStore
from .tools import ToolCatalog
from .toolsets import (
    ToolsetRequest,
    ToolsetResolver,
    ToolsetSnapshot,
    rebind_toolset,
)


class SessionContextError(RuntimeError):
    pass


class SessionSnapshotConflict(SessionContextError):
    pass


class ToolsetRebindError(SessionContextError):
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
            profile=defaults.profile,
            surface=defaults.surface,
            platform=defaults.platform,
            config_version=defaults.config_version,
            enabled_skills=defaults.enabled_skills,
            enabled_toolsets=defaults.enabled_toolsets,
            permissions=defaults.permissions,
        )

    def to_json_value(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "surface": self.surface,
            "platform": self.platform,
            "config_version": self.config_version,
            "enabled_skills": list(self.enabled_skills),
            "enabled_toolsets": list(self.enabled_toolsets),
            "permissions": sorted(self.permissions),
        }

    @classmethod
    def from_json_value(cls, value: Mapping[str, object]) -> "SessionBuildRequest":
        return cls(
            profile=str(value.get("profile", "legacy")),
            surface=str(value.get("surface", "legacy")),
            platform=str(value.get("platform", "legacy")),
            config_version=str(value.get("config_version", "legacy")),
            enabled_skills=tuple(value.get("enabled_skills", ())),
            enabled_toolsets=tuple(value.get("enabled_toolsets", ("default",))),
            permissions=frozenset(value.get("permissions", ())),
        )


@dataclass(frozen=True, slots=True)
class SessionRuntimeSnapshot:
    session_id: str
    context_version: int
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
    def __init__(
        self, *, store: SQLiteStore, prompt_builder: SystemPromptBuilder,
        toolset_resolver: ToolsetResolver, catalog: ToolCatalog,
        memory: MemoryProvider, defaults: SessionBuildRequest,
        memory_max_chars: int,
        legacy_bindings: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        self._store = store
        self._prompt_builder = prompt_builder
        self._toolset_resolver = toolset_resolver
        self._catalog = catalog
        self._memory = memory
        self._defaults = defaults
        self._memory_max_chars = memory_max_chars
        self._legacy_bindings = dict(legacy_bindings or {})

    def create_or_load(
        self, session_id: str, user_input: UserInput,
        request: SessionBuildRequest | None,
        lease: LeaseHandle,
    ) -> SessionContextResolution:
        persisted = self._store.load_session_context(session_id)
        if persisted is None:
            return self._create(
                session_id, user_input, request or self._defaults, lease
            )
        return self._load(persisted, request, lease)

    def _create(
        self, session_id: str, user_input: UserInput,
        request: SessionBuildRequest,
        lease: LeaseHandle,
    ) -> SessionContextResolution:
        warnings: list[str] = []
        try:
            memory_items = self._memory.recall(user_input, session_id)
        except BaseException as exc:
            memory_items = ()
            warnings.append(
                f"memory bootstrap failed: {type(exc).__name__}: {exc}"
            )
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
            warnings.append(
                f"memory bootstrap data was ignored: {type(exc).__name__}: {exc}"
            )
        prompt = self._prompt_builder.build(PromptBuildRequest(
            profile=request.profile,
            surface=request.surface,
            platform=request.platform,
            config_version=request.config_version,
            enabled_skills=request.enabled_skills,
            bootstrap_memory=tuple(bootstrap),
        ))
        if (
            prompt.content_bytes != prompt.text.encode("utf-8")
            or hashlib.sha256(prompt.content_bytes).hexdigest()
            != prompt.fingerprint
        ):
            raise SessionContextError(
                "prompt builder returned inconsistent bytes or fingerprint"
            )
        toolset = self._toolset_resolver.resolve(ToolsetRequest(
            profile=request.profile,
            surface=request.surface,
            platform=request.platform,
            enabled_toolsets=request.enabled_toolsets,
            permissions=request.permissions,
        ), self._catalog)
        try:
            toolset = rebind_toolset(
                schema_bytes=toolset.schema_bytes,
                fingerprint=toolset.fingerprint,
                descriptors=tuple(dict(item) for item in toolset.descriptors()),
                catalog=self._catalog,
            )
        except (KeyError, LookupError, ValueError) as exc:
            raise ToolsetRebindError(
                f"toolset resolver returned an invalid snapshot: {exc}"
            ) from exc
        created_at = time.time()
        persisted = self._store.create_session_context(
            session_id,
            build_request=request.to_json_value(),
            system_prompt_bytes=prompt.content_bytes,
            system_prompt_fingerprint=prompt.fingerprint,
            tool_schema_bytes=toolset.schema_bytes,
            toolset_fingerprint=toolset.fingerprint,
            tool_bindings=tuple(dict(item) for item in toolset.descriptors()),
            source_versions=prompt.source_versions,
            turn_id=lease.turn_id,
            user_message=Message.user(
                user_input.text, metadata=user_input.metadata
            ),
            fence=lease.fence,
        )
        return SessionContextResolution(
            snapshot=self._runtime_snapshot(persisted, request, prompt, toolset),
            created=True,
            warnings=tuple(warnings),
        )

    def _load(
        self, persisted: PersistedSessionContext,
        request: SessionBuildRequest | None,
        lease: LeaseHandle,
    ) -> SessionContextResolution:
        stored_request = SessionBuildRequest.from_json_value(persisted.build_request)
        if request is not None and request != stored_request:
            raise SessionSnapshotConflict(
                "explicit SessionBuildRequest differs from the persisted snapshot"
            )
        warnings: list[str] = []
        if request is None and stored_request != self._defaults:
            warnings.append(
                "current session defaults differ; persisted snapshot was retained"
            )
        descriptors = persisted.tool_bindings
        if persisted.binding_state == "legacy_unverified":
            descriptors = self._verify_legacy_bindings(persisted, lease)
        try:
            toolset = rebind_toolset(
                schema_bytes=persisted.tool_schema_bytes,
                fingerprint=persisted.toolset_fingerprint,
                descriptors=descriptors,
                catalog=self._catalog,
            )
        except (KeyError, LookupError, ValueError) as exc:
            raise ToolsetRebindError(str(exc)) from exc
        prompt = SystemPromptSnapshot(
            text=persisted.system_prompt_bytes.decode("utf-8"),
            content_bytes=persisted.system_prompt_bytes,
            fingerprint=persisted.system_prompt_fingerprint,
            source_versions=persisted.source_versions,
        )
        return SessionContextResolution(
            snapshot=self._runtime_snapshot(
                persisted, stored_request, prompt, toolset
            ),
            created=False,
            warnings=tuple(warnings),
        )

    def _verify_legacy_bindings(
        self, persisted: PersistedSessionContext, lease: LeaseHandle,
    ) -> tuple[dict[str, str], ...]:
        if not persisted.tool_bindings:
            return ()
        verified: list[dict[str, str]] = []
        for descriptor in persisted.tool_bindings:
            legacy_name = descriptor["name"]
            identity = self._legacy_bindings.get(legacy_name)
            if identity is None:
                raise ToolsetRebindError(
                    f"legacy tool binding requires an explicit mapping: {legacy_name}"
                )
            tool_id, version = identity
            spec = self._catalog.get_versioned(tool_id, version)
            if spec is None or spec.name != legacy_name:
                raise ToolsetRebindError(
                    f"legacy mapping cannot be rebound: {legacy_name}"
                )
            verified.append({
                "tool_id": tool_id,
                "implementation_version": version,
                "name": legacy_name,
                "schema_fingerprint": descriptor["schema_fingerprint"],
            })
        result = tuple(verified)
        rebound = rebind_toolset(
            schema_bytes=persisted.tool_schema_bytes,
            fingerprint=persisted.toolset_fingerprint,
            descriptors=result,
            catalog=self._catalog,
        )
        if not self._store.verify_legacy_session_context(
            persisted.session_id, persisted.version,
            tuple(dict(item) for item in rebound.descriptors()),
            fence=lease.fence,
        ):
            raise ToolsetRebindError("legacy binding verification was not persisted")
        return tuple(dict(item) for item in rebound.descriptors())

    @staticmethod
    def _runtime_snapshot(
        persisted: PersistedSessionContext,
        request: SessionBuildRequest,
        prompt: SystemPromptSnapshot,
        toolset: ToolsetSnapshot,
    ) -> SessionRuntimeSnapshot:
        return SessionRuntimeSnapshot(
            session_id=persisted.session_id,
            context_version=persisted.version,
            build_request=request,
            prompt=prompt,
            toolset=toolset,
            created_at=persisted.created_at,
        )


__all__ = [
    "SessionBuildRequest",
    "SessionContextError",
    "SessionContextFactory",
    "SessionContextResolution",
    "SessionRuntimeSnapshot",
    "SessionSnapshotConflict",
    "ToolsetRebindError",
]
