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
from typing import Any, Iterator, Literal

from .models import Message, ToolCall, ToolOutcome, TurnError
from .tools import encode_tool_error


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, current_context_version INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_contexts (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, system_prompt TEXT NOT NULL, tools_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (session_id, version)
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    context_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    started_at REAL NOT NULL, completed_at REAL, final_message_id INTEGER,
    error_json TEXT, route_name TEXT, iterations INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}', completion_reason TEXT NOT NULL DEFAULT 'unknown',
    provider_attempts INTEGER NOT NULL DEFAULT 0, route_attempts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS turns_session_status_idx ON turns(session_id, status);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT, api_content TEXT, tool_calls_json TEXT, tool_call_id TEXT,
    tool_name TEXT, finish_reason TEXT, effect_disposition TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
    UNIQUE (session_id, seq)
);
CREATE INDEX IF NOT EXISTS messages_session_seq_idx ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS tool_executions (
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
    side_effects TEXT NOT NULL CHECK (side_effects IN ('none', 'possible')),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'running', 'succeeded', 'failed', 'denied', 'not_executed', 'unknown')
    ),
    effect_disposition TEXT, error_type TEXT, started_at REAL, finished_at REAL,
    updated_at REAL NOT NULL, PRIMARY KEY (turn_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS tool_executions_session_idx
    ON tool_executions(session_id, turn_id);
"""


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionContextState:
    version: int
    changed: bool


@dataclass(frozen=True, slots=True)
class RecoveryState:
    warnings: tuple[str, ...] = ()
    messages: tuple[Message, ...] = ()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _context_fingerprint(system_prompt: str, tools: list[dict[str, Any]]) -> tuple[str, str]:
    tools_json = _canonical_json(tools)
    payload = _canonical_json({"system_prompt": system_prompt, "tools": json.loads(tools_json)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), tools_json


class SQLiteStore:
    """Synchronous SQLite store; a transaction never crosses an await boundary."""

    def __init__(self, path: str | Path, *, tool_result_max_chars: int = 32_768) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.tool_result_max_chars = tool_result_max_chars
        self._state_lock = threading.RLock()
        self._opened = False
        self._anchor: sqlite3.Connection | None = None
        if str(self.path) == ":memory:":
            self._database = f"file:agentcore-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        else:
            self._database = str(self.path)
            self._uri = False

    def open(self) -> None:
        with self._state_lock:
            if self._opened:
                return
            if not self._uri:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._new_connection()
            if self._uri:
                self._anchor = connection
            try:
                connection.executescript(_SCHEMA)
                self._migrate_v2(connection)
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                connection.commit()
            finally:
                if not self._uri:
                    connection.close()
            self._opened = True

    def close(self) -> None:
        with self._state_lock:
            if self._anchor is not None:
                self._anchor.close()
                self._anchor = None
            self._opened = False

    def _migrate_v2(self, connection: sqlite3.Connection) -> None:
        turn_columns = {row[1] for row in connection.execute("PRAGMA table_info(turns)")}
        additions = {
            "completion_reason": "TEXT NOT NULL DEFAULT 'unknown'",
            "provider_attempts": "INTEGER NOT NULL DEFAULT 0",
            "route_attempts_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in turn_columns:
                connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {definition}")
        message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        if "effect_disposition" not in message_columns:
            connection.execute("ALTER TABLE messages ADD COLUMN effect_disposition TEXT")

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

    def ensure_session_context(
        self, session_id: str, system_prompt: str, tools: list[dict[str, Any]]
    ) -> SessionContextState:
        fingerprint, tools_json = _context_fingerprint(system_prompt, tools)
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT current_context_version FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                version = 1
                connection.execute(
                    "INSERT INTO sessions(id, current_context_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)", (session_id, version, now, now),
                )
                connection.execute(
                    "INSERT INTO session_contexts(session_id, version, system_prompt, tools_json, "
                    "fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, version, system_prompt, tools_json, fingerprint, now),
                )
                return SessionContextState(version, False)
            current = int(row["current_context_version"])
            context = connection.execute(
                "SELECT fingerprint FROM session_contexts WHERE session_id = ? AND version = ?",
                (session_id, current),
            ).fetchone()
            if context is not None and context["fingerprint"] == fingerprint:
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
                )
                return SessionContextState(current, False)
            version = current + 1
            connection.execute(
                "INSERT INTO session_contexts(session_id, version, system_prompt, tools_json, "
                "fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, version, system_prompt, tools_json, fingerprint, now),
            )
            connection.execute(
                "UPDATE sessions SET current_context_version = ?, updated_at = ? WHERE id = ?",
                (version, now, session_id),
            )
            return SessionContextState(version, True)

    def recover_incomplete_turns(self, session_id: str) -> RecoveryState:
        warnings: list[str] = []
        repaired: list[Message] = []
        with self._transaction() as connection:
            turns = connection.execute(
                "SELECT id, status FROM turns WHERE session_id = ? ORDER BY started_at",
                (session_id,),
            ).fetchall()
            for turn_row in turns:
                turn_id = str(turn_row["id"])
                messages = self._load_turn_messages(connection, turn_id)
                added = self._repair_tool_pairs(connection, session_id, turn_id, messages)
                repaired.extend(added)
                if added:
                    warnings.append(f"repaired {len(added)} incomplete tool call(s) in turn {turn_id}")
                    messages.extend(added)
                if turn_row["status"] != "running":
                    continue
                last = messages[-1] if messages else None
                completed = bool(
                    last and last.role == "assistant" and not last.tool_calls
                    and last.content and last.content.strip()
                )
                status = "completed" if completed else "failed"
                error = None if completed else _canonical_json({
                    "code": "recovered_incomplete_turn",
                    "message": "Recovered an unfinished turn after process interruption.",
                    "retryable": True,
                })
                connection.execute(
                    "UPDATE turns SET status = ?, completed_at = ?, error_json = ?, "
                    "completion_reason = ? WHERE id = ? AND status = 'running'",
                    (status, time.time(), error, "recovered", turn_id),
                )
                warnings.append(f"recovered unfinished turn {turn_id} as {status}")
        return RecoveryState(tuple(warnings), tuple(repaired))

    def repair_turn_tool_calls(self, session_id: str, turn_id: str) -> RecoveryState:
        with self._transaction() as connection:
            messages = self._load_turn_messages(connection, turn_id)
            repaired = self._repair_tool_pairs(connection, session_id, turn_id, messages)
        warnings = (f"repaired {len(repaired)} incomplete tool call(s) in current turn",) if repaired else ()
        return RecoveryState(warnings, tuple(repaired))

    def _repair_tool_pairs(
        self, connection: sqlite3.Connection, session_id: str,
        turn_id: str, messages: list[Message]
    ) -> list[Message]:
        answered = {
            message.tool_call_id for message in messages
            if message.role == "tool" and message.tool_call_id
        }
        missing = [
            call for message in messages for call in (message.tool_calls or [])
            if call.id not in answered
        ]
        repaired: list[Message] = []
        for call in missing:
            execution = connection.execute(
                "SELECT state, side_effects FROM tool_executions "
                "WHERE turn_id = ? AND tool_call_id = ?", (turn_id, call.id),
            ).fetchone()
            pending = execution is not None and execution["state"] == "pending"
            side_effects = str(execution["side_effects"]) if execution else "possible"
            state = "not_executed" if pending else "unknown"
            effect = "not_applied" if pending else ("none" if side_effects == "none" else "unknown")
            error_type = "not_executed" if pending else "outcome_unknown"
            content = encode_tool_error(
                error_type,
                "The tool handler was never entered." if pending else
                "The tool outcome is unknown after interruption; it was not replayed.",
                self.tool_result_max_chars,
                execution_state=state,
                effect_disposition=effect,
            )
            message = Message(
                role="tool", content=content, tool_call_id=call.id, tool_name=call.name,
                effect_disposition=effect,
                metadata={"execution_state": state, "recovered": True},
            )
            self._insert_message(connection, session_id, turn_id, message)
            now = time.time()
            connection.execute(
                "INSERT INTO tool_executions(turn_id, session_id, tool_call_id, tool_name, "
                "side_effects, state, effect_disposition, error_type, finished_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(turn_id, tool_call_id) DO UPDATE SET state = excluded.state, "
                "effect_disposition = excluded.effect_disposition, error_type = excluded.error_type, "
                "finished_at = excluded.finished_at, updated_at = excluded.updated_at",
                (turn_id, session_id, call.id, call.name, side_effects, state,
                 effect, error_type, now, now),
            )
            repaired.append(message)
        return repaired

    def load_messages(self, session_id: str) -> list[Message]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
            return [self._row_to_message(row) for row in rows]
        finally:
            connection.close()

    def begin_turn(
        self, *, turn_id: str, session_id: str,
        context_version: int, user_message: Message
    ) -> int:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO turns(id, session_id, context_version, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (turn_id, session_id, context_version, time.time()),
            )
            return self._insert_message(connection, session_id, turn_id, user_message)

    def append_message(self, session_id: str, turn_id: str, message: Message) -> int:
        with self._transaction() as connection:
            return self._insert_message(connection, session_id, turn_id, message)

    def stage_tool_calls(
        self, session_id: str, turn_id: str, assistant_message: Message,
        calls: list[tuple[ToolCall, Literal["none", "possible"]]],
    ) -> int:
        with self._transaction() as connection:
            message_id = self._insert_message(connection, session_id, turn_id, assistant_message)
            now = time.time()
            for call, side_effects in calls:
                connection.execute(
                    "INSERT INTO tool_executions(turn_id, session_id, tool_call_id, tool_name, "
                    "side_effects, state, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (turn_id, session_id, call.id, call.name, side_effects, now),
                )
            return message_id

    def mark_tool_running(self, turn_id: str, tool_call_id: str) -> bool:
        with self._transaction() as connection:
            now = time.time()
            cursor = connection.execute(
                "UPDATE tool_executions SET state = 'running', started_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND tool_call_id = ? AND state = 'pending'",
                (now, now, turn_id, tool_call_id),
            )
            return cursor.rowcount == 1

    def complete_tool_call(
        self, *, session_id: str, turn_id: str, message: Message,
        state: Literal["succeeded", "failed", "denied", "not_executed", "unknown"],
        effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"],
        error_type: str | None,
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM tool_executions WHERE turn_id = ? AND tool_call_id = ?",
                (turn_id, message.tool_call_id),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown tool execution: {message.tool_call_id}")
            if row["state"] not in {"pending", "running"}:
                return False
            self._insert_message(connection, session_id, turn_id, message)
            now = time.time()
            connection.execute(
                "UPDATE tool_executions SET state = ?, effect_disposition = ?, error_type = ?, "
                "finished_at = ?, updated_at = ? WHERE turn_id = ? AND tool_call_id = ?",
                (state, effect_disposition, error_type, now, now, turn_id, message.tool_call_id),
            )
            return True

    def finish_turn_with_message(
        self, *, session_id: str, turn_id: str, message: Message,
        status: str, error: TurnError | None, route_name: str | None,
        iterations: int, usage: dict[str, int], completion_reason: str = "unknown",
        provider_attempts: int = 0, route_attempts: dict[str, int] | None = None,
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise StorageError(f"unknown turn: {turn_id}")
            if row["status"] != "running":
                return False
            message_id = self._insert_message(connection, session_id, turn_id, message)
            self._finish_turn(
                connection, session_id=session_id, turn_id=turn_id, status=status,
                error=error, route_name=route_name, iterations=iterations, usage=usage,
                final_message_id=message_id, completion_reason=completion_reason,
                provider_attempts=provider_attempts, route_attempts=route_attempts or {},
            )
            return True

    def finish_turn(
        self, *, session_id: str, turn_id: str, status: str,
        error: TurnError | None, route_name: str | None, iterations: int,
        usage: dict[str, int], completion_reason: str = "unknown",
        provider_attempts: int = 0, route_attempts: dict[str, int] | None = None,
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise StorageError(f"unknown turn: {turn_id}")
            if row["status"] != "running":
                return False
            self._finish_turn(
                connection, session_id=session_id, turn_id=turn_id, status=status,
                error=error, route_name=route_name, iterations=iterations, usage=usage,
                final_message_id=None, completion_reason=completion_reason,
                provider_attempts=provider_attempts, route_attempts=route_attempts or {},
            )
            return True

    def _finish_turn(
        self, connection: sqlite3.Connection, *, session_id: str, turn_id: str,
        status: str, error: TurnError | None, route_name: str | None,
        iterations: int, usage: dict[str, int], final_message_id: int | None,
        completion_reason: str, provider_attempts: int, route_attempts: dict[str, int],
    ) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"invalid terminal turn status: {status}")
        connection.execute(
            "UPDATE turns SET status = ?, completed_at = ?, final_message_id = ?, error_json = ?, "
            "route_name = ?, iterations = ?, usage_json = ?, completion_reason = ?, "
            "provider_attempts = ?, route_attempts_json = ? "
            "WHERE id = ? AND status = 'running'",
            (status, time.time(), final_message_id,
             _canonical_json(error.model_dump()) if error else None,
             route_name, iterations, _canonical_json(usage), completion_reason,
             provider_attempts, _canonical_json(route_attempts), turn_id),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            error_json = result.pop("error_json")
            result["error"] = json.loads(error_json) if error_json else None
            result["usage"] = json.loads(result.pop("usage_json"))
            result["route_attempts"] = json.loads(result.pop("route_attempts_json"))
            return result
        finally:
            connection.close()

    def get_contexts(self, session_id: str) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT * FROM session_contexts WHERE session_id = ? ORDER BY version", (session_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_tool_outcomes(self, turn_id: str) -> list[ToolOutcome]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT tool_call_id, tool_name, state, effect_disposition, error_type "
                "FROM tool_executions WHERE turn_id = ? ORDER BY rowid", (turn_id,),
            ).fetchall()
            return [
                ToolOutcome(
                    tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
                    state=row["state"], effect_disposition=row["effect_disposition"] or "unknown",
                    error_type=row["error_type"], may_still_be_running=row["state"] == "unknown",
                )
                for row in rows if row["state"] not in {"pending", "running"}
            ]
        finally:
            connection.close()

    def _load_turn_messages(
        self, connection: sqlite3.Connection, turn_id: str
    ) -> list[Message]:
        rows = connection.execute(
            "SELECT * FROM messages WHERE turn_id = ? ORDER BY seq", (turn_id,)
        ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def _insert_message(
        self, connection: sqlite3.Connection, session_id: str,
        turn_id: str, message: Message,
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        tool_calls_json = (
            _canonical_json([call.model_dump() for call in message.tool_calls])
            if message.tool_calls else None
        )
        cursor = connection.execute(
            "INSERT INTO messages(session_id, turn_id, seq, role, content, api_content, "
            "tool_calls_json, tool_call_id, tool_name, finish_reason, effect_disposition, "
            "metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn_id, int(row["next_seq"]), message.role, message.content,
             message.api_content, tool_calls_json, message.tool_call_id, message.tool_name,
             message.finish_reason, message.effect_disposition,
             _canonical_json(message.metadata), time.time()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            role=row["role"], content=row["content"], api_content=row["api_content"],
            tool_calls=json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else None,
            tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
            finish_reason=row["finish_reason"], effect_disposition=row["effect_disposition"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )