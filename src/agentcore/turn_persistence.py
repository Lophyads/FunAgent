"""Synchronous database writes for a turn; no history or execution lifecycle."""

from __future__ import annotations

from typing import Callable, TypeVar

from .models import LeaseFence, Message
from .storage import RecoveryState, SQLiteStore, StorageError

T = TypeVar("T")


class TurnPersistence:
    """Apply a fixed write policy and translate message metadata for SQLite.

    Disabling writes does not disable reads on the caller's store. Each loop
    owns its writer and tool-batch row mapping; writers are not shared by forks.
    A fence is a write credential, not proof of a currently valid lease.
    """

    def __init__(
        self, session_id: str, store: SQLiteStore | None = None, *,
        persist_disabled: bool = False, fence: LeaseFence | None = None,
        turn_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self.turn_id = turn_id
        self._store = store
        self._enabled = store is not None and not persist_disabled
        self._fence = fence
        self._tool_parent_id: int | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def bind_fence(self, fence: LeaseFence) -> None:
        if not self.enabled or fence.session_id != self.session_id:
            raise ValueError("write fence does not belong to this persistence target")
        if self._fence is not None and self._fence != fence:
            raise ValueError("a turn writer cannot change its write fence")
        self._fence = fence

    def _write(self, operation: Callable[[SQLiteStore, LeaseFence], T]) -> T | None:
        if not self.enabled:
            return None
        if self._fence is None:
            raise RuntimeError("persistent write requires a fence")
        assert self._store is not None
        return operation(self._store, self._fence)

    def save_new_session(
        self, *,
        system_prompt_bytes: bytes, user_message: Message, workspace: str | None = None,
    ) -> None:
        self._write(lambda db, fence: db.create_session(
            self.session_id,
            system_prompt_bytes=system_prompt_bytes, user_message=self._tag(user_message), fence=fence,
            workspace=workspace,
        ))

    def save_user_message(self, message: Message, *, workspace: str | None = None) -> None:
        self._write(lambda db, fence: db.append_user_message(
            self.session_id, self._tag(message), fence=fence, workspace=workspace,
        ))

    def save_tool_calls(self, message: Message) -> None:
        row_id = self._write(lambda db, fence: db.append_tool_calls(
            self.session_id, self._tag(message), fence=fence,
        ))
        self._tool_parent_id = row_id

    def save_tool_result(self, message: Message) -> None:
        def save(db: SQLiteStore, fence: LeaseFence) -> None:
            if self._tool_parent_id is None:
                raise StorageError("tool result has no persisted assistant batch")
            # Database linkage is never injected into the live message object.
            persisted = self._tag(message).model_copy(update={"reply_to_message_id": self._tool_parent_id})
            db.append_tool_result(self.session_id, persisted, fence=fence)

        self._write(save)

    def save_final_response(
        self, message: Message, *, delivery_destination: str | None = None,
        recovery: bool = False,
    ) -> None:
        self._write(lambda db, fence: db.append_final_assistant(
            self.session_id, self._tag(message), fence=fence,
            delivery_destination=delivery_destination, recovery=recovery,
        ))

    def save_recovery_claim(self, *, max_attempts: int) -> bool:
        return bool(self._write(lambda db, fence: db.claim_recovery(
            self.session_id, fence=fence, max_attempts=max_attempts,
        )))

    def persisted_message(self, message: Message) -> Message:
        if self.turn_id is None:
            return message
        return message.model_copy(update={"metadata": {**message.metadata, "turn_id": self.turn_id}})

    _tag = persisted_message

    def save_recovery_failure(self, *, max_attempts: int) -> None:
        self._write(lambda db, fence: db.finish_recovery(
            self.session_id, completed=False, max_attempts=max_attempts, fence=fence,
        ))

    def repair_stored_tool_batch(self) -> RecoveryState:
        """Repair a previous crashed turn before loading its durable history."""
        return self._write(lambda db, fence: db.repair_latest_tool_batch(
            self.session_id, fence=fence,
        )) or RecoveryState()


__all__ = ["TurnPersistence"]
