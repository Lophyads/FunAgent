from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import LeaseFence, LeaseHandle, Message, ToolCall, ToolOutcome
from tools.results import encode_tool_error


SCHEMA_VERSION = 10
_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    system_prompt_bytes BLOB NOT NULL,
    workspace TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resume_pending INTEGER NOT NULL DEFAULT 0 CHECK(resume_pending IN (0,1)),
    resume_reason TEXT,
    resume_attempts INTEGER NOT NULL DEFAULT 0,
    resume_not_before REAL,
    resume_suspended INTEGER NOT NULL DEFAULT 0 CHECK(resume_suspended IN (0,1))
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
    kind TEXT NOT NULL DEFAULT 'normal' CHECK(kind IN ('normal','internal_recovery')),
    content TEXT,
    api_content TEXT,
    tool_calls_json TEXT,
    tool_call_effects_json TEXT,
    reply_to_message_id INTEGER REFERENCES messages(id),
    tool_call_id TEXT,
    tool_name TEXT,
    tool_outcome_state TEXT,
    effect_disposition TEXT,
    tool_error_type TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    CHECK (
        (role='user' AND (content IS NOT NULL OR api_content IS NOT NULL)
         AND tool_calls_json IS NULL AND reply_to_message_id IS NULL
         AND tool_call_id IS NULL AND tool_outcome_state IS NULL)
        OR
        (role='assistant' AND (content IS NOT NULL OR tool_calls_json IS NOT NULL)
         AND api_content IS NULL AND reply_to_message_id IS NULL
         AND tool_call_id IS NULL AND tool_outcome_state IS NULL)
        OR
        (role='tool' AND content IS NOT NULL AND reply_to_message_id IS NOT NULL
         AND tool_call_id IS NOT NULL AND tool_name IS NOT NULL
         AND tool_outcome_state IS NOT NULL AND effect_disposition IS NOT NULL
         AND tool_calls_json IS NULL AND api_content IS NULL)
    )
);
CREATE INDEX messages_session_tail ON messages(session_id,id);
CREATE UNIQUE INDEX messages_tool_reply ON messages(reply_to_message_id,tool_call_id)
    WHERE role = 'tool';
CREATE TABLE session_leases (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT,
    fencing_token INTEGER NOT NULL,
    expires_at REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE delivery_obligations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    destination TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','attempting','failed','delivered')),
    attempts INTEGER NOT NULL DEFAULT 0,
    owner_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    delivered_at REAL,
    last_error TEXT,
    UNIQUE(message_id,destination)
);
CREATE INDEX delivery_obligations_state ON delivery_obligations(state,updated_at);
"""


class StorageError(RuntimeError):
    pass


class LeaseLostError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedSession:
    session_id: str
    system_prompt_bytes: bytes
    created_at: float
    workspace: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryState:
    warnings: tuple[str, ...] = ()
    messages: tuple[Message, ...] = ()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteStore:
    """Four-table, message-tail store."""

    def __init__(self, path: str | Path, *, tool_result_max_chars: int = 32_768) -> None:
        self.path = str(path)
        self.tool_result_max_chars = tool_result_max_chars
        self._uri = self.path == ":memory:"
        self._database = (
            f"file:agentcore-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._uri else self.path
        )
        self._anchor: sqlite3.Connection | None = None
        self._opened = False
        self._legacy_identity_columns = False
        self._state_lock = threading.RLock()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database, timeout=5.0, isolation_level=None, uri=self._uri
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not self._uri:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def open(self) -> None:
        with self._state_lock:
            if self._opened:
                return
            if not self._uri:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            connection = self._new_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    tables = {
                        str(row[0]) for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    expected = {
                        "sessions", "messages", "session_leases", "delivery_obligations"
                    }
                    if (version not in {0, 8, 9, SCHEMA_VERSION}
                            or (tables and (tables != expected or version == 0))):
                        raise StorageError(
                            "incompatible SQLite schema; stop all runtimes and migrate "
                            "the database before using this schema"
                        )
                    if not tables:
                        for statement in _SCHEMA.split(";"):
                            if statement.strip():
                                connection.execute(statement)
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    elif version in {8, 9}:
                        if version == 8:
                            connection.execute("ALTER TABLE sessions ADD COLUMN workspace TEXT")
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    columns = {
                        str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")
                    }
                    legacy = columns & {"profile", "surface", "platform"}
                    if ("workspace" not in columns or
                            legacy not in (set(), {"profile", "surface", "platform"})):
                        raise StorageError("incompatible sessions table layout")
                    self._legacy_identity_columns = bool(legacy)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                self._anchor = connection if self._uri else None
                self._opened = True
            except BaseException:
                connection.close()
                raise
            if not self._uri:
                connection.close()

    def close(self) -> None:
        with self._state_lock:
            self._opened = False
            if self._anchor is not None:
                self._anchor.close()
                self._anchor = None

    def _connection(self) -> sqlite3.Connection:
        with self._state_lock:
            if not self._opened:
                raise StorageError("SQLiteStore is not open")
        return self._new_connection()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _assert_fence(
        connection: sqlite3.Connection, fence: LeaseFence, *, session_id: str
    ) -> None:
        if fence.session_id != session_id:
            raise LeaseLostError("fence session does not match write target")
        row = connection.execute(
            "SELECT owner_id,fencing_token,expires_at FROM session_leases WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if (
            row is None or row["owner_id"] != fence.owner_id
            or int(row["fencing_token"]) != fence.fencing_token
            or float(row["expires_at"] or 0) <= time.time()
        ):
            raise LeaseLostError(f"fenced write rejected for session {session_id}")

    def acquire_lease(
        self, *, session_id: str, owner_id: str, turn_id: str, ttl_seconds: float
    ) -> LeaseHandle | None:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_leases WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is not None and row["owner_id"] is not None:
                if float(row["expires_at"] or 0) > now:
                    return None
                self._mark_resume_if_unfinished(connection, session_id, "lease_expired")
            token = int(row["fencing_token"]) + 1 if row else 1
            expires_at = now + ttl_seconds
            connection.execute(
                "INSERT INTO session_leases(session_id,owner_id,fencing_token,expires_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "owner_id=excluded.owner_id,fencing_token=excluded.fencing_token,"
                "expires_at=excluded.expires_at,updated_at=excluded.updated_at",
                (session_id, owner_id, token, expires_at, now),
            )
        return LeaseHandle(
            session_id=session_id, owner_id=owner_id, turn_id=turn_id,
            fencing_token=token, expires_at=expires_at,
        )

    def renew_lease(self, lease: LeaseHandle, *, ttl_seconds: float) -> LeaseHandle:
        now = time.time()
        expires_at = now + ttl_seconds
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE session_leases SET expires_at=?,updated_at=? WHERE "
                "session_id=? AND owner_id=? AND fencing_token=? AND expires_at>?",
                (expires_at, now, lease.session_id, lease.owner_id,
                 lease.fencing_token, now),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"lease is no longer current: {lease.session_id}")
        return lease.model_copy(update={"expires_at": expires_at})

    def assert_lease(self, lease: LeaseHandle) -> None:
        connection = self._connection()
        try:
            self._assert_fence(connection, lease.fence, session_id=lease.session_id)
        finally:
            connection.close()

    def release_lease(self, lease: LeaseHandle) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE session_leases SET owner_id=NULL,expires_at=NULL,updated_at=? "
                "WHERE session_id=? AND owner_id=? AND fencing_token=?",
                (time.time(), lease.session_id, lease.owner_id, lease.fencing_token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"stale lease cannot be released: {lease.session_id}")

    @staticmethod
    def _tail(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

    @classmethod
    def _mark_resume_if_unfinished(
        cls, connection: sqlite3.Connection, session_id: str, reason: str
    ) -> bool:
        tail = cls._tail(connection, session_id)
        if tail is None or (tail["role"] == "assistant" and tail["tool_calls_json"] is None):
            return False
        cursor = connection.execute(
            "UPDATE sessions SET resume_pending=1,resume_reason=?,updated_at=? "
            "WHERE id=? AND resume_pending=0",
            (reason, time.time(), session_id),
        )
        return cursor.rowcount == 1

    def mark_stale_leases(self, session_ids: frozenset[str] | None = None) -> list[str]:
        now = time.time()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT session_id FROM session_leases WHERE owner_id IS NOT NULL "
                "AND expires_at<=?", (now,),
            ).fetchall()
            if session_ids is not None:
                rows = [row for row in rows if str(row["session_id"]) in session_ids]
            marked = [
                str(row["session_id"]) for row in rows
                if self._mark_resume_if_unfinished(
                    connection, str(row["session_id"]), "lease_expired"
                )
            ]
            for row in rows:
                connection.execute(
                    "UPDATE session_leases SET owner_id=NULL,expires_at=NULL,updated_at=? "
                    "WHERE session_id=? AND owner_id IS NOT NULL AND expires_at<=?",
                    (now, row["session_id"], now),
                )
            return marked

    def create_session(
        self, session_id: str, *,
        system_prompt_bytes: bytes, user_message: Message, fence: LeaseFence,
        workspace: str | None = None,
    ) -> PersistedSession:
        if user_message.role != "user" or user_message.kind != "normal":
            raise StorageError("new session requires a normal user message")
        now = time.time()
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            if self._legacy_identity_columns:
                # Existing databases retain these unused NOT NULL columns to preserve history.
                connection.execute(
                    "INSERT INTO sessions(id,profile,surface,platform,system_prompt_bytes,"
                    "created_at,updated_at,workspace) VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, "", "", "", system_prompt_bytes, now, now, workspace),
                )
            else:
                connection.execute(
                    "INSERT INTO sessions(id,system_prompt_bytes,created_at,updated_at,workspace) "
                    "VALUES(?,?,?,?,?)",
                    (session_id, system_prompt_bytes, now, now, workspace),
                )
            self._insert_message(connection, session_id, user_message)
        return PersistedSession(
            session_id, system_prompt_bytes, now, workspace
        )

    def load_session(self, session_id: str) -> PersistedSession | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT id,system_prompt_bytes,created_at,workspace "
                "FROM sessions WHERE id=?", (session_id,),
            ).fetchone()
            return None if row is None else PersistedSession(
                str(row["id"]), bytes(row["system_prompt_bytes"]),
                float(row["created_at"]), row["workspace"],
            )
        finally:
            connection.close()

    def get_session_recovery(self, session_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT resume_pending,resume_reason,resume_attempts,resume_not_before,"
                "resume_suspended FROM sessions WHERE id=?", (session_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def list_pending_recoveries(self, max_attempts: int, session_ids: frozenset[str] | None = None) -> list[str]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT id FROM sessions WHERE resume_pending=1 AND resume_suspended=0 "
                "AND resume_attempts<? AND (resume_not_before IS NULL OR resume_not_before<=?) "
                "ORDER BY updated_at,id", (max_attempts, time.time()),
            ).fetchall()
            return [str(row["id"]) for row in rows
                    if session_ids is None or str(row["id"]) in session_ids]
        finally:
            connection.close()

    def claim_recovery(
        self, session_id: str, *, fence: LeaseFence, max_attempts: int
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            cursor = connection.execute(
                "UPDATE sessions SET resume_attempts=resume_attempts+1,updated_at=? "
                "WHERE id=? AND resume_pending=1 AND resume_suspended=0 "
                "AND resume_attempts<? AND (resume_not_before IS NULL OR resume_not_before<=?)",
                (time.time(), session_id, max_attempts, time.time()),
            )
            return cursor.rowcount == 1

    def finish_recovery(
        self, session_id: str, *, completed: bool, max_attempts: int,
        fence: LeaseFence,
    ) -> None:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            if completed:
                connection.execute(
                    "UPDATE sessions SET resume_pending=0,resume_reason=NULL,"
                    "resume_attempts=0,resume_not_before=NULL,resume_suspended=0,updated_at=? "
                    "WHERE id=?", (time.time(), session_id),
                )
            else:
                connection.execute(
                    "UPDATE sessions SET resume_suspended=CASE WHEN resume_attempts>=? "
                    "THEN 1 ELSE 0 END,resume_not_before=?,updated_at=? WHERE id=?",
                    (max_attempts, time.time()+min(60, 2**max_attempts),
                     time.time(), session_id),
                )

    def load_messages(self, session_id: str) -> list[Message]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,),
            ).fetchall()
            messages = [self._row_to_message(row) for row in rows]
            parent_calls: dict[int, dict[str, str]] = {}
            for row, message in zip(rows, messages):
                if message.role == "assistant" and message.tool_calls:
                    parent_calls[int(row["id"])] = {
                        call.id: call.name for call in message.tool_calls
                    }
                if message.role == "tool":
                    expected = parent_calls.get(message.reply_to_message_id or -1, {})
                    if expected.get(message.tool_call_id) != message.tool_name:
                        raise StorageError("invalid tool reply relationship in history")
            return messages
        finally:
            connection.close()

    def latest_message(self, session_id: str) -> tuple[int, Message] | None:
        connection = self._connection()
        try:
            row = self._tail(connection, session_id)
            return None if row is None else (int(row["id"]), self._row_to_message(row))
        finally:
            connection.close()

    @classmethod
    def _unresolved_batch(
        cls, connection: sqlite3.Connection, session_id: str
    ) -> tuple[int, list[ToolCall], dict[str, str]] | None:
        """Find missing replies in the tool-call batch at the session tail.

        A tool reply points back to its assistant message because one assistant
        message may contain several calls. Compare that full call list with the
        persisted replies. Return None when the tail is not a tool batch; an
        empty missing list means the batch exists and all replies are present.
        This method only reads state; repair_latest_tool_batch writes repairs.
        """
        tail = cls._tail(connection, session_id)
        if tail is None:
            return None

        if tail["role"] == "assistant" and tail["tool_calls_json"]:
            parent = tail
        elif tail["role"] == "tool" and tail["reply_to_message_id"] is not None:
            parent = connection.execute(
                "SELECT * FROM messages WHERE id=? AND session_id=? AND role='assistant'",
                (tail["reply_to_message_id"], session_id),
            ).fetchone()
            if parent is None or not parent["tool_calls_json"]:
                raise StorageError("tool tail has no assistant parent")
        else:
            return None
        calls = [ToolCall.model_validate(item) for item in json.loads(parent["tool_calls_json"])]
        replies = {
            str(row["tool_call_id"]) for row in connection.execute(
                "SELECT tool_call_id FROM messages WHERE reply_to_message_id=? AND role='tool'",
                (parent["id"],),
            )
        }
        missing = [call for call in calls if call.id not in replies]
        effects = json.loads(parent["tool_call_effects_json"] or "{}")
        return int(parent["id"]), missing, effects

    def repair_latest_tool_batch(
        self, session_id: str, *, fence: LeaseFence
    ) -> RecoveryState:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            batch = self._unresolved_batch(connection, session_id)
            if batch is None:
                return RecoveryState()
            parent_id, missing, effects = batch
            repaired: list[Message] = []
            for call in missing:
                side_effects = effects.get(call.id, "possible")
                effect = "none" if side_effects == "none" else "unknown"
                message = Message.tool_result(
                    encode_tool_error(
                        "interrupted", "Tool result was not committed before interruption.",
                        self.tool_result_max_chars,
                    ),
                    tool_call_id=call.id, tool_name=call.name,
                    reply_to_message_id=parent_id, tool_outcome_state="unknown",
                    effect_disposition=effect, tool_error_type="interrupted",
                )
                self._insert_message(connection, session_id, message)
                repaired.append(message)
            return RecoveryState(
                warnings=(f"repaired {len(repaired)} incomplete tool replies",)
                if repaired else (),
                messages=tuple(repaired),
            )

    def append_user_message(
        self, session_id: str, message: Message, *, fence: LeaseFence,
        workspace: str | None = None,
    ) -> int:
        if message.role != "user" or message.kind != "normal":
            raise StorageError("append_user_message requires a normal user message")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            batch = self._unresolved_batch(connection, session_id)
            if batch is not None and batch[1]:
                raise StorageError("cannot append user before repairing tool replies")
            if workspace is not None:
                connection.execute("UPDATE sessions SET workspace=? WHERE id=?", (workspace, session_id))
            return self._insert_message(connection, session_id, message)

    def append_tool_calls(
        self, session_id: str, message: Message, *, fence: LeaseFence
    ) -> int:
        if message.role != "assistant" or not message.tool_calls or not message.tool_call_effects:
            raise StorageError("assistant tool calls require effect snapshots")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            batch = self._unresolved_batch(connection, session_id)
            if batch is not None and batch[1]:
                raise StorageError("previous tool batch is incomplete")
            return self._insert_message(connection, session_id, message)

    def append_tool_result(
        self, session_id: str, message: Message, *, fence: LeaseFence
    ) -> int:
        if message.role != "tool" or message.reply_to_message_id is None:
            raise StorageError("append_tool_result requires a tool message with a database parent ID")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            return self._insert_message(connection, session_id, message)

    def append_final_assistant(
        self, session_id: str, message: Message, *, fence: LeaseFence,
        delivery_destination: str | None = None, recovery: bool = False,
    ) -> int:
        if message.role != "assistant" or message.tool_calls:
            raise StorageError("final message must be plain assistant text")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            tail = self._tail(connection, session_id)
            if tail is not None and tail["role"] == "assistant" and tail["tool_calls_json"] is None:
                raise StorageError("session tail is already a final assistant reply")
            batch = self._unresolved_batch(connection, session_id)
            if batch is not None and batch[1]:
                raise StorageError("cannot finalize with unresolved tool calls")
            message_id = self._insert_message(connection, session_id, message)
            if delivery_destination is not None:
                if not delivery_destination.strip():
                    raise StorageError("delivery destination must not be blank")
                obligation_id = hashlib.sha256(
                    _json([session_id, message_id, delivery_destination]).encode("utf-8")
                ).hexdigest()
                now = time.time()
                connection.execute(
                    "INSERT INTO delivery_obligations(id,session_id,message_id,destination,"
                    "state,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",
                    (obligation_id, session_id, message_id, delivery_destination, now, now),
                )
            if recovery:
                connection.execute(
                    "UPDATE sessions SET resume_pending=0,resume_reason=NULL,"
                    "resume_attempts=0,resume_not_before=NULL,resume_suspended=0,updated_at=? "
                    "WHERE id=?", (time.time(), session_id),
                )
            return message_id

    @staticmethod
    def _insert_message(
        connection: sqlite3.Connection, session_id: str, message: Message
    ) -> int:
        if message.role == "tool":
            parent = connection.execute(
                "SELECT tool_calls_json FROM messages WHERE id=? AND session_id=? "
                "AND role='assistant'", (message.reply_to_message_id, session_id),
            ).fetchone()
            if parent is None or not parent["tool_calls_json"]:
                raise StorageError("tool result references an unknown assistant message")
            calls = [ToolCall.model_validate(item) for item in json.loads(parent["tool_calls_json"])]
            if not any(call.id == message.tool_call_id and call.name == message.tool_name for call in calls):
                raise StorageError("tool result does not match its parent call")
            effects_row = connection.execute(
                "SELECT tool_call_effects_json FROM messages WHERE id=?",
                (message.reply_to_message_id,),
            ).fetchone()
            effects = json.loads(effects_row[0] or "{}")
            ToolOutcome(
                tool_call_id=message.tool_call_id or "",
                tool_name=message.tool_name or "",
                side_effects=effects.get(message.tool_call_id, "possible"),
                state=message.tool_outcome_state or "unknown",
                effect_disposition=message.effect_disposition or "unknown",
                error_type=message.tool_error_type,
            )
        cursor = connection.execute(
            "INSERT INTO messages(session_id,role,kind,content,api_content,tool_calls_json,"
            "tool_call_effects_json,reply_to_message_id,tool_call_id,tool_name,"
            "tool_outcome_state,effect_disposition,tool_error_type,metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, message.role, message.kind, message.content,
                message.api_content,
                _json([call.model_dump(mode="json") for call in message.tool_calls])
                if message.tool_calls else None,
                _json(message.tool_call_effects) if message.tool_call_effects else None,
                message.reply_to_message_id, message.tool_call_id, message.tool_name,
                message.tool_outcome_state, message.effect_disposition,
                message.tool_error_type, _json(message.metadata), time.time(),
            ),
        )
        connection.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), session_id)
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            role=row["role"], kind=row["kind"], content=row["content"],
            api_content=row["api_content"],
            tool_calls=json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else None,
            tool_call_effects=json.loads(row["tool_call_effects_json"])
            if row["tool_call_effects_json"] else None,
            reply_to_message_id=row["reply_to_message_id"],
            tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
            tool_outcome_state=row["tool_outcome_state"],
            effect_disposition=row["effect_disposition"],
            tool_error_type=row["tool_error_type"],
            metadata=json.loads(row["metadata_json"]),
        )

    def list_pending_deliveries(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT d.*,m.content FROM delivery_obligations d JOIN messages m "
                "ON m.id=d.message_id WHERE d.state IN ('pending','attempting','failed') "
                "ORDER BY d.created_at,d.id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def claim_delivery(
        self, obligation_id: str, *, fence: LeaseFence
    ) -> dict[str, Any] | None:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=fence.session_id)
            row = connection.execute(
                "SELECT d.*,m.content FROM delivery_obligations d JOIN messages m "
                "ON m.id=d.message_id WHERE d.id=? AND d.session_id=?",
                (obligation_id, fence.session_id),
            ).fetchone()
            if row is None or row["state"] not in {"pending", "attempting", "failed"}:
                return None
            connection.execute(
                "UPDATE delivery_obligations SET state='attempting',attempts=attempts+1,"
                "owner_id=?,updated_at=? WHERE id=?",
                (fence.owner_id, time.time(), obligation_id),
            )
            return dict(row)

    def finish_delivery(
        self, obligation_id: str, *, delivered: bool,
        error_type: str | None, fence: LeaseFence,
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=fence.session_id)
            now = time.time()
            cursor = connection.execute(
                "UPDATE delivery_obligations SET state=?,updated_at=?,delivered_at=?,"
                "last_error=?,owner_id=NULL WHERE id=? AND session_id=? "
                "AND state='attempting' AND owner_id=?",
                ("delivered" if delivered else "failed", now,
                 now if delivered else None, error_type, obligation_id,
                 fence.session_id, fence.owner_id),
            )
            return cursor.rowcount == 1


__all__ = [
    "LeaseLostError", "PersistedSession", "RecoveryState", "SCHEMA_VERSION",
    "SQLiteStore", "StorageError",
]
