"""Prepare conversation data without access to the loop's mutable state."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tools.context import BackendProbes, ToolAvailabilityContext
from tools.lifecycle import SessionResourceKey
from .runtime_cwd import ResolvedWorkspace, resolve_workspace

from .interfaces import TurnHostContext
from .message_history import validate_complete_history
from .models import Message, UserInput
from .session_context import (
    RecoverySessionMissing, SessionBuildRequest, SessionContextFactory, SessionRuntimeSnapshot,
)
from .storage import SQLiteStore
from .turn_persistence import TurnPersistence

if TYPE_CHECKING:
    from .turn_state import TurnRuntime

logger = logging.getLogger(__name__)


class RecoveryNotEligible(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedConversation:
    context: SessionRuntimeSnapshot
    messages: tuple[Message, ...]
    workspace: ResolvedWorkspace
    resource_key: SessionResourceKey
    warnings: tuple[str, ...] = ()
    session_created: bool | None = None
    user_driven_recovery: bool = False

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("prepared conversation requires a submitted message")
        validate_complete_history(self.messages)
        object.__setattr__(self, "context", self.context.copy())
        object.__setattr__(self, "messages", tuple(m.model_copy(deep=True) for m in self.messages))


def prepare_memory_turn(
    runtime: TurnRuntime, *, context: SessionRuntimeSnapshot,
    history: tuple[Message, ...], user_input: UserInput, workspace=None,
    resource_key: SessionResourceKey | None = None,
) -> PreparedConversation:
    workspace = resolve_workspace(workspace, None, runtime.services.config.workspace)
    resource_key = resource_key or SessionResourceKey(uuid.uuid4().hex)
    message = Message.user(user_input.text, metadata=user_input.metadata)
    return PreparedConversation(context, (*history, message), workspace, resource_key)


def prepare_stored_turn(
    runtime: TurnRuntime, *,
    store: SQLiteStore, contexts: SessionContextFactory, persistence: TurnPersistence,
    user_input: UserInput, request: SessionBuildRequest | None,
    host_context: TurnHostContext, recovery: bool,
    resource_key: SessionResourceKey | None = None,
) -> PreparedConversation:
    """Read/build under the caller's lease; writes use the supplied writer.

    The caller claims recovery before preparation so a failed build can still
    finish the claim. Workspace and resource identity are ordinary preparation data.
    """
    session_id = persistence.session_id
    warnings: list[str] = []
    user_driven_recovery = False
    if not recovery and persistence.enabled:
        lifecycle = store.get_session_recovery(session_id)
        user_driven_recovery = bool(lifecycle and lifecycle["resume_pending"])

    def publish(name: str, **payload) -> None:
        logger.info("%s", name)
        warnings.extend(runtime.observability.publish(
            name, trace_id=runtime.turn_id, session_id=session_id,
            turn_id=runtime.turn_id, payload=payload,
        ))

    repaired = persistence.repair_stored_tool_batch()
    warnings.extend(repaired.warnings)
    if repaired.warnings:
        publish("recovery.incomplete_turn_found", warning_count=len(repaired.warnings))
    if repaired.messages:
        publish("recovery.tool_pair_repaired", repair_count=len(repaired.messages))
    if repaired.warnings or repaired.messages:
        publish("recovery.history_repaired")

    persisted = store.load_session(session_id)
    services = runtime.services
    defaults = services.config.workspace
    previous_workspace = (
        ResolvedWorkspace(Path(persisted.workspace), defaults.allowed_root)
        if persisted is not None and persisted.workspace is not None else None
    )
    workspace = resolve_workspace(request.workspace if request else None, previous_workspace, defaults)
    resource_key = resource_key or SessionResourceKey(session_id)
    availability = ToolAvailabilityContext(
        services.config.tools, workspace, host_context.capabilities,
        BackendProbes(services.available),
    )
    resolution = contexts.build(
        session_id, user_input, request, persisted=persisted,
        availability=availability, recovery=recovery,
    )
    warnings.extend(resolution.warnings)

    if recovery:
        latest = store.latest_message(session_id)
        if latest is None:
            raise RecoverySessionMissing("recovery session has no message tail")
        message = latest[1]
    else:
        message = Message.user(user_input.text, metadata=user_input.metadata)
        if resolution.created:
            persistence.save_new_session(
                system_prompt_bytes=resolution.snapshot.prompt.content_bytes, user_message=message,
                workspace=str(workspace.cwd),
            )
        else:
            persistence.save_user_message(message, workspace=str(workspace.cwd))

    messages = store.load_messages(session_id)
    validate_complete_history(messages)
    if not persistence.enabled:
        messages.append(message)
    elif not messages or messages[-1] != message:
        raise RuntimeError("loaded history does not end with the submitted message")
    return PreparedConversation(
        context=resolution.snapshot, messages=tuple(messages), workspace=workspace,
        resource_key=resource_key, warnings=tuple(warnings),
        session_created=resolution.created, user_driven_recovery=user_driven_recovery,
    )


__all__ = ["PreparedConversation", "prepare_memory_turn", "prepare_stored_turn", "RecoveryNotEligible"]
