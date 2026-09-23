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

from .models import LeaseFence, LeaseHandle, Message, ToolCall, ToolOutcome, TurnError
from tools.tools import encode_tool_error


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, current_context_version INTEGER NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_contexts (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, system_prompt TEXT NOT NULL, tools_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL, build_request_json TEXT NOT NULL DEFAULT '{}',
    system_prompt_bytes BLOB, system_prompt_fingerprint TEXT,
    tool_schema_bytes BLOB, toolset_fingerprint TEXT,
    tool_bindings_json TEXT NOT NULL DEFAULT '[]',
    source_versions_json TEXT NOT NULL DEFAULT '[]',
    binding_state TEXT NOT NULL DEFAULT 'verified',
    created_at REAL NOT NULL,
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
    may_still_be_running INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL, PRIMARY KEY (turn_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS tool_executions_session_idx
    ON tool_executions(session_id, turn_id);
CREATE TABLE IF NOT EXISTS model_attempts (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    iteration INTEGER NOT NULL,
    route_index INTEGER NOT NULL,
    route_name TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started', 'succeeded', 'failed', 'ambiguous')),
    error_type TEXT,
    status_code INTEGER,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS model_attempts_turn_idx
    ON model_attempts(turn_id, iteration, route_index, attempt_number);
CREATE TABLE IF NOT EXISTS session_leases (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT,
    turn_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    updated_at REAL NOT NULL
);
"""


class StorageError(RuntimeError):
    pass


class LeaseLostError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedSessionContext:
    session_id: str
    version: int
    build_request: dict[str, Any]
    system_prompt_bytes: bytes
    system_prompt_fingerprint: str
    tool_schema_bytes: bytes
    toolset_fingerprint: str
    tool_bindings: tuple[dict[str, str], ...]
    source_versions: tuple[tuple[str, str], ...]
    binding_state: Literal["verified", "legacy_unverified"]
    created_at: float


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
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version_row = connection.execute(
                        "SELECT value FROM schema_meta "
                        "WHERE key = 'schema_version'"
                    ).fetchone()
                    if version_row is not None:
                        try:
                            existing_version = int(version_row["value"])
                        except (TypeError, ValueError) as exc:
                            raise StorageError(
                                "database schema version is invalid"
                            ) from exc
                        if existing_version > 7:
                            raise StorageError(
                                "database schema is newer than this AgentCore"
                            )
                    self._migrate_v2(connection)
                    self._migrate_v3(connection)
                    self._migrate_v4(connection)
                    self._migrate_v5(connection)
                    self._migrate_v6(connection)
                    self._migrate_v7(connection)
                    connection.execute(
                        "INSERT INTO schema_meta(key, value) "
                        "VALUES ('schema_version', '7') "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
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
        tool_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_executions)")
        }
        if "may_still_be_running" not in tool_columns:
            connection.execute(
                "ALTER TABLE tool_executions ADD COLUMN "
                "may_still_be_running INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_v3(self, connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(session_contexts)")}
        additions = {
            "build_request_json": "TEXT NOT NULL DEFAULT '{}'",
            "system_prompt_bytes": "BLOB",
            "system_prompt_fingerprint": "TEXT",
            "tool_schema_bytes": "BLOB",
            "toolset_fingerprint": "TEXT",
            "tool_bindings_json": "TEXT NOT NULL DEFAULT '[]'",
            "source_versions_json": "TEXT NOT NULL DEFAULT '[]'",
            "binding_state": "TEXT NOT NULL DEFAULT 'verified'",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE session_contexts ADD COLUMN {name} {definition}"
                )
        rows = connection.execute(
            "SELECT session_id, version, system_prompt, tools_json, "
            "system_prompt_bytes, system_prompt_fingerprint, "
            "tool_schema_bytes, toolset_fingerprint FROM session_contexts"
        ).fetchall()
        for row in rows:
            if all((
                row["system_prompt_bytes"] is not None,
                row["system_prompt_fingerprint"] is not None,
                row["tool_schema_bytes"] is not None,
                row["toolset_fingerprint"] is not None,
            )):
                continue
            prompt_bytes = (
                bytes(row["system_prompt_bytes"])
                if row["system_prompt_bytes"] is not None
                else str(row["system_prompt"]).encode("utf-8")
            )
            raw_tools = json.loads(row["tools_json"])
            tool_bytes = (
                bytes(row["tool_schema_bytes"])
                if row["tool_schema_bytes"] is not None
                else _canonical_json(raw_tools).encode("utf-8")
            )
            descriptors = []
            for definition in raw_tools:
                function = definition.get("function", {})
                name = str(function.get("name", ""))
                descriptors.append({
                    "tool_id": name,
                    "implementation_version": "legacy_unverified",
                    "name": name,
                    "schema_fingerprint": hashlib.sha256(
                        _canonical_json(definition).encode("utf-8")
                    ).hexdigest(),
                })
            connection.execute(
                "UPDATE session_contexts SET system_prompt_bytes = ?, "
                "system_prompt_fingerprint = ?, tool_schema_bytes = ?, "
                "toolset_fingerprint = ?, tool_bindings_json = ?, "
                "source_versions_json = '[]', binding_state = ? "
                "WHERE session_id = ? AND version = ?",
                (
                    prompt_bytes,
                    hashlib.sha256(prompt_bytes).hexdigest(),
                    tool_bytes,
                    hashlib.sha256(tool_bytes).hexdigest(),
                    _canonical_json(descriptors),
                    "verified" if not descriptors else "legacy_unverified",
                    row["session_id"],
                    row["version"],
                ),
            )

    def _migrate_v4(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_attempts ("
            "id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE, "
            "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, "
            "iteration INTEGER NOT NULL, route_index INTEGER NOT NULL, route_name TEXT NOT NULL, "
            "attempt_number INTEGER NOT NULL, request_fingerprint TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK (state IN ('started','succeeded','failed','ambiguous')), "
            "error_type TEXT, status_code INTEGER, started_at REAL NOT NULL, finished_at REAL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS model_attempts_turn_idx "
            "ON model_attempts(turn_id, iteration, route_index, attempt_number)"
        )

    def _migrate_v5(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS session_leases ("
            "session_id TEXT PRIMARY KEY, owner_id TEXT, turn_id TEXT, "
            "fencing_token INTEGER NOT NULL DEFAULT 0, expires_at REAL, updated_at REAL NOT NULL)"
        )

    def _migrate_v6(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS session_lifecycle ("
            "session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, "
            "active_turn_id TEXT, active_turn_token TEXT, active_turn_started_at REAL, "
            "resume_pending INTEGER NOT NULL DEFAULT 0, resume_reason TEXT, "
            "resume_source_turn_id TEXT, resume_attempts INTEGER NOT NULL DEFAULT 0, "
            "resume_not_before REAL, resume_suspended INTEGER NOT NULL DEFAULT 0, "
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_instances ("
            "id TEXT PRIMARY KEY, started_at REAL NOT NULL, "
            "closed_at REAL, clean_shutdown INTEGER NOT NULL DEFAULT 0)"
        )
        # v5 had no active markers. Preserve its unfinished turns as pending
        # recovery without modifying historical messages.
        connection.execute(
            "INSERT OR IGNORE INTO session_lifecycle("
            "session_id, resume_pending, resume_reason, "
            "resume_source_turn_id, updated_at) "
            "SELECT t.session_id, 1, 'legacy_interrupted', t.id, ? "
            "FROM turns t WHERE t.status = 'running' "
            "AND t.final_message_id IS NULL "
            "AND t.id = (SELECT t2.id FROM turns t2 "
            "WHERE t2.session_id = t.session_id AND t2.status = 'running' "
            "ORDER BY t2.started_at DESC LIMIT 1)",
            (time.time(),),
        )

    def _migrate_v7(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS delivery_obligations ("
            "id TEXT PRIMARY KEY, "
            "session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, "
            "turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE, "
            "message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE, "
            "destination TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK (state IN "
            "('pending','attempting','delivered','failed','abandoned')), "
            "attempts INTEGER NOT NULL DEFAULT 0, owner_id TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "delivered_at REAL, last_error TEXT)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS delivery_obligations_state_idx "
            "ON delivery_obligations(state, updated_at)"
        )

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

    def create_session_context(
        self, session_id: str, *, build_request: dict[str, Any],
        system_prompt_bytes: bytes, system_prompt_fingerprint: str,
        tool_schema_bytes: bytes, toolset_fingerprint: str,
        tool_bindings: tuple[dict[str, str], ...],
        source_versions: tuple[tuple[str, str], ...],
        turn_id: str, user_message: Message, fence: LeaseFence,
    ) -> PersistedSessionContext:
        if user_message.role != "user":
            raise StorageError("session admission requires a user message")
        if (
            hashlib.sha256(system_prompt_bytes).hexdigest()
            != system_prompt_fingerprint
        ):
            raise StorageError("system prompt fingerprint does not match bytes")
        if hashlib.sha256(tool_schema_bytes).hexdigest() != toolset_fingerprint:
            raise StorageError("toolset fingerprint does not match schema bytes")
        system_prompt = system_prompt_bytes.decode("utf-8")
        tools = json.loads(tool_schema_bytes.decode("utf-8"))
        if not isinstance(tools, list):
            raise StorageError("tool schema snapshot must be a JSON list")
        legacy_fingerprint, tools_json = _context_fingerprint(system_prompt, tools)
        now = time.time()
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            row = connection.execute(
                "SELECT current_context_version FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                raise StorageError(f"session already exists: {session_id}")
            version = 1
            connection.execute(
                "INSERT INTO sessions(id, current_context_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)", (session_id, version, now, now),
            )
            connection.execute(
                "INSERT INTO session_contexts("
                "session_id, version, system_prompt, tools_json, fingerprint, "
                "build_request_json, system_prompt_bytes, system_prompt_fingerprint, "
                "tool_schema_bytes, toolset_fingerprint, tool_bindings_json, "
                "source_versions_json, binding_state, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)",
                (
                    session_id, version, system_prompt, tools_json, legacy_fingerprint,
                    _canonical_json(build_request), system_prompt_bytes,
                    system_prompt_fingerprint, tool_schema_bytes, toolset_fingerprint,
                    _canonical_json(tool_bindings), _canonical_json(source_versions), now,
                ),
            )
            connection.execute(
                "INSERT INTO turns(id, session_id, context_version, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (turn_id, session_id, version, now),
            )
            self._insert_message(
                connection, session_id, turn_id, user_message
            )
            self._set_active_turn(connection, session_id, turn_id, now)
        return PersistedSessionContext(
            session_id=session_id, version=version, build_request=build_request,
            system_prompt_bytes=system_prompt_bytes,
            system_prompt_fingerprint=system_prompt_fingerprint,
            tool_schema_bytes=tool_schema_bytes, toolset_fingerprint=toolset_fingerprint,
            tool_bindings=tool_bindings, source_versions=source_versions,
            binding_state="verified", created_at=now,
        )

    def load_session_context(self, session_id: str) -> PersistedSessionContext | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT c.* FROM session_contexts c JOIN sessions s "
                "ON s.id = c.session_id AND s.current_context_version = c.version "
                "WHERE c.session_id = ?",
                (session_id,),
            ).fetchone()
            return self._row_to_session_context(row) if row is not None else None
        finally:
            connection.close()

    def verify_legacy_session_context(
        self, session_id: str, version: int,
        tool_bindings: tuple[dict[str, str], ...],
        *, fence: LeaseFence,
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            cursor = connection.execute(
                "UPDATE session_contexts SET tool_bindings_json = ?, "
                "binding_state = 'verified' "
                "WHERE session_id = ? AND version = ? "
                "AND binding_state = 'legacy_unverified'",
                (_canonical_json(tool_bindings), session_id, version),
            )
            return cursor.rowcount == 1

    def recover_incomplete_turns(
        self, session_id: str, *, fence: LeaseFence
    ) -> RecoveryState:
        warnings: list[str] = []
        repaired: list[Message] = []
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            turns = connection.execute(
                "SELECT id, status FROM turns WHERE session_id = ? ORDER BY started_at",
                (session_id,),
            ).fetchall()
            for turn_row in turns:
                turn_id = str(turn_row["id"])
                connection.execute(
                    "UPDATE model_attempts SET state = 'ambiguous', "
                    "error_type = 'process_interrupted', finished_at = ? "
                    "WHERE turn_id = ? AND state = 'started'",
                    (time.time(), turn_id),
                )
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
                attempt_rows = connection.execute(
                    "SELECT route_name, COUNT(*) AS attempt_count, "
                    "MAX(iteration) AS max_iteration "
                    "FROM model_attempts WHERE turn_id = ? GROUP BY route_name",
                    (turn_id,),
                ).fetchall()
                route_attempts = {
                    str(item["route_name"]): int(item["attempt_count"])
                    for item in attempt_rows
                }
                provider_attempts = sum(route_attempts.values())
                iterations = max(
                    (int(item["max_iteration"]) for item in attempt_rows),
                    default=0,
                )
                final_message_id = None
                if completed:
                    final_row = connection.execute(
                        "SELECT id FROM messages WHERE turn_id = ? "
                        "AND role = 'assistant' AND tool_calls_json IS NULL "
                        "ORDER BY seq DESC LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    final_message_id = (
                        int(final_row["id"]) if final_row is not None else None
                    )
                connection.execute(
                    "UPDATE turns SET status = ?, completed_at = ?, error_json = ?, "
                    "completion_reason = ?, provider_attempts = ?, "
                    "route_attempts_json = ?, iterations = ?, final_message_id = ? "
                    "WHERE id = ? AND status = 'running'",
                    (
                        status, time.time(), error, "recovered",
                        provider_attempts, _canonical_json(route_attempts),
                        iterations, final_message_id, turn_id,
                    ),
                )
                warnings.append(f"recovered unfinished turn {turn_id} as {status}")
        return RecoveryState(tuple(warnings), tuple(repaired))

    def repair_turn_tool_calls(
        self, session_id: str, turn_id: str, *, fence: LeaseFence
    ) -> RecoveryState:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            turn = connection.execute(
                "SELECT session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if turn is None or turn["session_id"] != session_id:
                raise StorageError(
                    "repair target does not belong to the fenced session"
                )
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
            message = Message.tool_result(
                content, tool_call_id=call.id, tool_name=call.name,
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
        context_version: int, user_message: Message, fence: LeaseFence,
    ) -> int:
        if user_message.role != "user":
            raise StorageError("begin_turn requires a user message")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            context = connection.execute(
                "SELECT 1 FROM session_contexts "
                "WHERE session_id = ? AND version = ?",
                (session_id, context_version),
            ).fetchone()
            if context is None:
                raise StorageError("turn references an unknown session context")
            connection.execute(
                "INSERT INTO turns(id, session_id, context_version, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (turn_id, session_id, context_version, time.time()),
            )
            message_id = self._insert_message(
                connection, session_id, turn_id, user_message
            )
            self._set_active_turn(connection, session_id, turn_id, time.time())
            return message_id

    @staticmethod
    def _set_active_turn(
        connection: sqlite3.Connection, session_id: str, turn_id: str, now: float
    ) -> None:
        connection.execute(
            "INSERT INTO session_lifecycle("
            "session_id, active_turn_id, active_turn_token, "
            "active_turn_started_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "active_turn_id = excluded.active_turn_id, "
            "active_turn_token = excluded.active_turn_token, "
            "active_turn_started_at = excluded.active_turn_started_at, "
            "updated_at = excluded.updated_at "
            "WHERE session_lifecycle.resume_pending = 0",
            (session_id, turn_id, turn_id, now, now),
        )

    def record_runtime_start(self, instance_id: str) -> bool:
        with self._transaction() as connection:
            previous = connection.execute(
                "SELECT clean_shutdown FROM runtime_instances "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
            connection.execute(
                "INSERT INTO runtime_instances(id, started_at) VALUES (?, ?)",
                (instance_id, time.time()),
            )
            return previous is None or bool(previous["clean_shutdown"])

    def mark_runtime_clean(self, instance_id: str) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE runtime_instances SET clean_shutdown = 1, "
                "closed_at = ? WHERE id = ? AND clean_shutdown = 0",
                (time.time(), instance_id),
            )
            if cursor.rowcount != 1:
                raise StorageError("runtime instance is not open")

    def clear_active_turn(
        self, session_id: str, turn_id: str, *, fence: LeaseFence
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            cursor = connection.execute(
                "UPDATE session_lifecycle SET active_turn_id = NULL, "
                "active_turn_token = NULL, active_turn_started_at = NULL, "
                "updated_at = ? WHERE session_id = ? AND active_turn_token = ?",
                (time.time(), session_id, turn_id),
            )
            return cursor.rowcount == 1

    def convert_stale_active_turns(self) -> list[str]:
        """Convert only markers whose session lease is no longer live."""
        converted: list[str] = []
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute(
                "SELECT l.session_id, l.active_turn_id, t.final_message_id, t.status "
                "FROM session_lifecycle l "
                "LEFT JOIN session_leases sl ON sl.session_id = l.session_id "
                "LEFT JOIN turns t ON t.id = l.active_turn_id "
                "WHERE l.active_turn_id IS NOT NULL "
                "AND (sl.owner_id IS NULL OR sl.expires_at <= ?)",
                (now,),
            ).fetchall()
            for row in rows:
                session_id = str(row["session_id"])
                source_turn_id = str(row["active_turn_id"])
                has_final = (
                    row["final_message_id"] is not None
                    and row["status"] == "completed"
                )
                cursor = connection.execute(
                    "UPDATE session_lifecycle SET active_turn_id = NULL, "
                    "active_turn_token = NULL, active_turn_started_at = NULL, "
                    "resume_pending = CASE WHEN ? THEN resume_pending ELSE 1 END, "
                    "resume_reason = CASE WHEN ? THEN resume_reason "
                    "ELSE 'restart_interrupted' END, "
                    "resume_source_turn_id = CASE WHEN ? THEN resume_source_turn_id "
                    "ELSE ? END, updated_at = ? "
                    "WHERE session_id = ? AND active_turn_id = ?",
                    (has_final, has_final, has_final, source_turn_id,
                     now, session_id, source_turn_id),
                )
                if cursor.rowcount and not has_final:
                    converted.append(session_id)
        return converted

    def list_pending_recoveries(self, max_attempts: int) -> list[str]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT l.session_id FROM session_lifecycle l "
                "WHERE l.resume_pending = 1 AND l.resume_suspended = 0 "
                "AND l.resume_attempts < ? "
                "AND (l.resume_not_before IS NULL OR l.resume_not_before <= ?) "
                "AND NOT EXISTS (SELECT 1 FROM delivery_obligations d "
                "WHERE d.session_id = l.session_id AND d.state NOT IN "
                "('delivered','abandoned')) ORDER BY l.updated_at",
                (max_attempts, time.time()),
            ).fetchall()
            return [str(row["session_id"]) for row in rows]
        finally:
            connection.close()

    def claim_recovery(
        self, session_id: str, *, fence: LeaseFence, max_attempts: int
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            cursor = connection.execute(
                "UPDATE session_lifecycle SET resume_attempts = resume_attempts + 1, "
                "updated_at = ? WHERE session_id = ? AND resume_pending = 1 "
                "AND resume_suspended = 0 AND resume_attempts < ? "
                "AND (resume_not_before IS NULL OR resume_not_before <= ?) "
                "AND NOT EXISTS (SELECT 1 FROM delivery_obligations d "
                "WHERE d.session_id = ? AND d.state NOT IN "
                "('delivered','abandoned'))",
                (time.time(), session_id, max_attempts, time.time(), session_id),
            )
            return cursor.rowcount == 1

    def finish_recovery(
        self, session_id: str, *, completed: bool,
        max_attempts: int, fence: LeaseFence
    ) -> None:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            if completed:
                connection.execute(
                    "UPDATE session_lifecycle SET resume_pending = 0, "
                    "resume_reason = NULL, resume_source_turn_id = NULL, "
                    "resume_suspended = 0, resume_attempts = 0, "
                    "resume_not_before = NULL, updated_at = ? WHERE session_id = ?",
                    (time.time(), session_id),
                )
            else:
                connection.execute(
                    "UPDATE session_lifecycle SET resume_suspended = "
                    "CASE WHEN resume_attempts >= ? THEN 1 ELSE resume_suspended END, "
                    "updated_at = ? WHERE session_id = ?",
                    (max_attempts, time.time(), session_id),
                )

    def get_session_lifecycle(self, session_id: str) -> dict[str, Any] | None:
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT * FROM session_lifecycle WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def begin_recovery_turn(
        self, *, turn_id: str, session_id: str, context_version: int,
        fence: LeaseFence,
    ) -> Message:
        message = Message.user(
            "Continue the interrupted task using the recorded conversation. "
            "Do not repeat any tool call whose outcome is already recorded or unknown.",
            metadata={"message_kind": "recovery", "visibility": "internal"},
        )
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            row = connection.execute(
                "SELECT 1 FROM session_contexts "
                "WHERE session_id = ? AND version = ?",
                (session_id, context_version),
            ).fetchone()
            if row is None:
                raise StorageError("recovery references an unknown session context")
            now = time.time()
            connection.execute(
                "INSERT INTO turns(id, session_id, context_version, status, started_at) "
                "VALUES (?, ?, ?, 'running', ?)",
                (turn_id, session_id, context_version, now),
            )
            self._insert_message(connection, session_id, turn_id, message)
            # resume_pending already records interrupted recovery work. Keeping
            # an active marker as well would create competing recovery signals.
        return message

    def append_message(
        self, session_id: str, turn_id: str, message: Message, *, fence: LeaseFence
    ) -> int:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            return self._insert_message(connection, session_id, turn_id, message)

    def stage_tool_calls(
        self, session_id: str, turn_id: str, assistant_message: Message,
        calls: list[tuple[ToolCall, Literal["none", "possible"]]],
        *, fence: LeaseFence,
    ) -> int:
        message_calls = assistant_message.tool_calls or []
        if assistant_message.role != "assistant" or not message_calls:
            raise StorageError("stage_tool_calls requires an assistant tool message")
        staged_calls = [call for call, _ in calls]
        if [
            (call.id, call.name, call.arguments) for call in message_calls
        ] != [
            (call.id, call.name, call.arguments) for call in staged_calls
        ]:
            raise StorageError("staged tool executions must match the assistant message")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            message_id = self._insert_message(connection, session_id, turn_id, assistant_message)
            now = time.time()
            for call, side_effects in calls:
                connection.execute(
                    "INSERT INTO tool_executions(turn_id, session_id, tool_call_id, tool_name, "
                    "side_effects, state, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (turn_id, session_id, call.id, call.name, side_effects, now),
                )
            return message_id

    def mark_tool_running(
        self, turn_id: str, tool_call_id: str, *, fence: LeaseFence
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=fence.session_id)
            now = time.time()
            cursor = connection.execute(
                "UPDATE tool_executions SET state = 'running', started_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND tool_call_id = ? AND session_id = ? "
                "AND state = 'pending'",
                (now, now, turn_id, tool_call_id, fence.session_id),
            )
            return cursor.rowcount == 1

    def complete_tool_call(
        self, *, session_id: str, turn_id: str, message: Message,
        state: Literal["succeeded", "failed", "denied", "not_executed", "unknown"],
        effect_disposition: Literal["none", "not_applied", "confirmed", "unknown"],
        error_type: str | None,
        may_still_be_running: bool,
        fence: LeaseFence,
    ) -> bool:
        if message.role != "tool":
            raise StorageError("complete_tool_call requires a tool result message")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            row = connection.execute(
                "SELECT session_id, tool_name, state FROM tool_executions "
                "WHERE turn_id = ? AND tool_call_id = ?",
                (turn_id, message.tool_call_id),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown tool execution: {message.tool_call_id}")
            if row["session_id"] != session_id:
                raise StorageError("tool execution belongs to another session")
            if row["tool_name"] != message.tool_name:
                raise StorageError("tool result name does not match the staged call")
            if row["state"] not in {"pending", "running"}:
                return False
            self._insert_message(connection, session_id, turn_id, message)
            now = time.time()
            connection.execute(
                "UPDATE tool_executions SET state = ?, effect_disposition = ?, error_type = ?, "
                "may_still_be_running = ?, finished_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND tool_call_id = ?",
                (
                    state, effect_disposition, error_type,
                    int(may_still_be_running), now, now,
                    turn_id, message.tool_call_id,
                ),
            )
            return True

    def finish_turn_with_message(
        self, *, session_id: str, turn_id: str, message: Message,
        status: str, error: TurnError | None, route_name: str | None,
        iterations: int, usage: dict[str, int], completion_reason: str = "unknown",
        provider_attempts: int = 0, route_attempts: dict[str, int] | None = None,
        fence: LeaseFence, delivery_destination: str | None = None,
        recovery: bool = False,
    ) -> bool:
        if message.role != "assistant" or message.tool_calls is not None:
            raise StorageError("terminal turn message must be assistant text")
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            row = connection.execute(
                "SELECT status, session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown turn: {turn_id}")
            if row["session_id"] != session_id:
                raise StorageError("turn belongs to another session")
            if row["status"] != "running":
                return False
            message_id = self._insert_message(connection, session_id, turn_id, message)
            self._finish_turn(
                connection, session_id=session_id, turn_id=turn_id, status=status,
                error=error, route_name=route_name, iterations=iterations, usage=usage,
                final_message_id=message_id, completion_reason=completion_reason,
                provider_attempts=provider_attempts, route_attempts=route_attempts or {},
            )
            if status == "completed" and error is None:
                if delivery_destination is not None:
                    self._create_delivery_obligation(
                        connection, session_id, turn_id, message_id,
                        delivery_destination,
                    )
                if recovery and completion_reason == "model_final":
                    connection.execute(
                        "UPDATE session_lifecycle SET resume_pending = 0, "
                        "resume_reason = NULL, resume_source_turn_id = NULL, "
                        "resume_suspended = 0, resume_attempts = 0, "
                        "resume_not_before = NULL, updated_at = ? "
                        "WHERE session_id = ? AND resume_pending = 1",
                        (time.time(), session_id),
                    )
            return True

    def finish_turn(
        self, *, session_id: str, turn_id: str, status: str,
        error: TurnError | None, route_name: str | None, iterations: int,
        usage: dict[str, int], completion_reason: str = "unknown",
        provider_attempts: int = 0, route_attempts: dict[str, int] | None = None,
        fence: LeaseFence,
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            row = connection.execute(
                "SELECT status, session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown turn: {turn_id}")
            if row["session_id"] != session_id:
                raise StorageError("turn belongs to another session")
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

    @staticmethod
    def _create_delivery_obligation(
        connection: sqlite3.Connection, session_id: str, turn_id: str,
        message_id: int, destination: str,
    ) -> str:
        if not destination.strip():
            raise StorageError("delivery destination must not be blank")
        payload = _canonical_json(
            [session_id, turn_id, message_id, destination]
        ).encode("utf-8")
        obligation_id = hashlib.sha256(payload).hexdigest()
        now = time.time()
        connection.execute(
            "INSERT OR IGNORE INTO delivery_obligations("
            "id, session_id, turn_id, message_id, destination, state, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (obligation_id, session_id, turn_id, message_id,
             destination, now, now),
        )
        return obligation_id

    def list_pending_deliveries(self) -> list[dict[str, Any]]:
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT d.*, m.content FROM delivery_obligations d "
                "JOIN messages m ON m.id = d.message_id "
                "WHERE d.state IN ('pending','attempting','failed') "
                "ORDER BY d.created_at, d.id"
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
                "SELECT d.*, m.content FROM delivery_obligations d "
                "JOIN messages m ON m.id = d.message_id WHERE d.id = ?",
                (obligation_id,),
            ).fetchone()
            if row is None or row["session_id"] != fence.session_id:
                return None
            if row["state"] not in {"pending", "attempting", "failed"}:
                return None
            cursor = connection.execute(
                "UPDATE delivery_obligations SET state = 'attempting', "
                "attempts = attempts + 1, owner_id = ?, updated_at = ? "
                "WHERE id = ? AND state IN ('pending','attempting','failed')",
                (fence.owner_id, time.time(), obligation_id),
            )
            return dict(row) if cursor.rowcount else None

    def finish_delivery(
        self, obligation_id: str, *, delivered: bool,
        error_type: str | None, fence: LeaseFence,
    ) -> bool:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=fence.session_id)
            now = time.time()
            cursor = connection.execute(
                "UPDATE delivery_obligations SET state = ?, updated_at = ?, "
                "delivered_at = ?, last_error = ?, owner_id = NULL "
                "WHERE id = ? AND session_id = ? AND state = 'attempting' "
                "AND owner_id = ?",
                (
                    "delivered" if delivered else "failed", now,
                    now if delivered else None, error_type,
                    obligation_id, fence.session_id, fence.owner_id,
                ),
            )
            return cursor.rowcount == 1

    def begin_model_attempt(
        self, *, attempt_id: str, turn_id: str, session_id: str,
        iteration: int, route_index: int, route_name: str,
        attempt_number: int, request_fingerprint: str, fence: LeaseFence,
    ) -> None:
        with self._transaction() as connection:
            self._assert_fence(connection, fence, session_id=session_id)
            turn = connection.execute(
                "SELECT session_id FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if turn is None or turn["session_id"] != session_id:
                raise StorageError(
                    "model attempt turn belongs to another session"
                )
            connection.execute(
                "INSERT INTO model_attempts("
                "id, turn_id, session_id, iteration, route_index, route_name, "
                "attempt_number, request_fingerprint, state, started_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)",
                (
                    attempt_id, turn_id, session_id, iteration, route_index,
                    route_name, attempt_number, request_fingerprint, time.time(),
                ),
            )

    def finish_model_attempt(
        self, attempt_id: str, *, state: Literal["succeeded", "failed", "ambiguous"],
        error_type: str | None, status_code: int | None, fence: LeaseFence,
    ) -> None:
        with self._transaction() as connection:
            self._assert_fence(
                connection, fence, session_id=fence.session_id
            )
            cursor = connection.execute(
                "UPDATE model_attempts SET state = ?, error_type = ?, status_code = ?, "
                "finished_at = ? WHERE id = ? AND session_id = ? "
                "AND state = 'started'",
                (
                    state, error_type, status_code, time.time(),
                    attempt_id, fence.session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"model attempt is not active: {attempt_id}")

    def acquire_lease(
        self, *, session_id: str, owner_id: str, turn_id: str,
        ttl_seconds: float,
    ) -> LeaseHandle | None:
        now = time.time()
        expires_at = now + ttl_seconds
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_leases WHERE session_id = ?", (session_id,)
            ).fetchone()
            if (
                row is not None
                and row["owner_id"] is not None
                and float(row["expires_at"] or 0) > now
                and row["owner_id"] != owner_id
            ):
                return None
            next_token = int(row["fencing_token"]) + 1 if row is not None else 1
            connection.execute(
                "INSERT INTO session_leases("
                "session_id, owner_id, turn_id, fencing_token, expires_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET owner_id = excluded.owner_id, "
                "turn_id = excluded.turn_id, fencing_token = excluded.fencing_token, "
                "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
                (session_id, owner_id, turn_id, next_token, expires_at, now),
            )
        return LeaseHandle(
            session_id=session_id, owner_id=owner_id, turn_id=turn_id,
            fencing_token=next_token, expires_at=expires_at,
        )

    def renew_lease(self, lease: LeaseHandle, *, ttl_seconds: float) -> LeaseHandle:
        now = time.time()
        expires_at = now + ttl_seconds
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE session_leases SET expires_at = ?, updated_at = ? "
                "WHERE session_id = ? AND owner_id = ? AND turn_id = ? "
                "AND fencing_token = ? AND expires_at > ?",
                (
                    expires_at, now, lease.session_id, lease.owner_id,
                    lease.turn_id, lease.fencing_token, now,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"lease is no longer current: {lease.session_id}")
        return lease.model_copy(update={"expires_at": expires_at})

    def assert_lease(self, lease: LeaseHandle) -> None:
        connection = self._connection()
        try:
            self._assert_fence(connection, lease.fence, turn_id=lease.turn_id)
        finally:
            connection.close()

    def release_lease(self, lease: LeaseHandle) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE session_leases SET owner_id = NULL, turn_id = NULL, "
                "expires_at = NULL, updated_at = ? "
                "WHERE session_id = ? AND owner_id = ? AND turn_id = ? "
                "AND fencing_token = ?",
                (
                    time.time(), lease.session_id, lease.owner_id,
                    lease.turn_id, lease.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"stale lease cannot be released: {lease.session_id}")

    @staticmethod
    def _assert_fence(
        connection: sqlite3.Connection, fence: LeaseFence, *,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        if session_id is not None and session_id != fence.session_id:
            raise LeaseLostError(
                "fence session does not match the write target"
            )
        row = connection.execute(
            "SELECT owner_id, turn_id, fencing_token, expires_at "
            "FROM session_leases WHERE session_id = ?",
            (fence.session_id,),
        ).fetchone()
        now = time.time()
        if (
            row is None
            or row["owner_id"] != fence.owner_id
            or int(row["fencing_token"]) != fence.fencing_token
            or float(row["expires_at"] or 0) <= now
            or (turn_id is not None and row["turn_id"] != turn_id)
        ):
            raise LeaseLostError(f"fenced write rejected for session {fence.session_id}")

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
                "SELECT tool_call_id, tool_name, side_effects, state, "
                "effect_disposition, error_type, may_still_be_running "
                "FROM tool_executions WHERE turn_id = ? ORDER BY rowid", (turn_id,),
            ).fetchall()
            return [
                ToolOutcome(
                    tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
                    side_effects=row["side_effects"], state=row["state"],
                    effect_disposition=row["effect_disposition"] or "unknown",
                    error_type=row["error_type"],
                    may_still_be_running=bool(row["may_still_be_running"]),
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
        self._validate_message_relationships(
            connection, session_id=session_id, turn_id=turn_id, message=message
        )
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
    def _validate_message_relationships(
        connection: sqlite3.Connection, *, session_id: str,
        turn_id: str, message: Message,
    ) -> None:
        turn = connection.execute(
            "SELECT session_id FROM turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if turn is None:
            raise StorageError(f"unknown turn: {turn_id}")
        if turn["session_id"] != session_id:
            raise StorageError("turn belongs to another session")

        assistant_rows = connection.execute(
            "SELECT tool_calls_json FROM messages "
            "WHERE turn_id = ? AND role = 'assistant' AND tool_calls_json IS NOT NULL "
            "ORDER BY seq",
            (turn_id,),
        ).fetchall()
        existing_calls: dict[str, str] = {}
        for row in assistant_rows:
            try:
                raw_calls = json.loads(row["tool_calls_json"])
                for raw_call in raw_calls:
                    call_id = str(raw_call["id"])
                    call_name = str(raw_call["name"])
                    if call_id in existing_calls:
                        raise StorageError(
                            f"duplicate tool call id in turn history: {call_id}"
                        )
                    existing_calls[call_id] = call_name
            except StorageError:
                raise
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise StorageError("invalid assistant tool call history") from exc

        if message.role == "assistant" and message.tool_calls:
            duplicate = next(
                (call.id for call in message.tool_calls if call.id in existing_calls),
                None,
            )
            if duplicate is not None:
                raise StorageError(f"tool call id already exists in turn: {duplicate}")
            return

        if message.role != "tool":
            return
        expected_name = existing_calls.get(message.tool_call_id or "")
        if expected_name is None:
            raise StorageError(
                f"tool result references an unknown assistant call: {message.tool_call_id}"
            )
        if expected_name != message.tool_name:
            raise StorageError("tool result name does not match the assistant call")
        duplicate_result = connection.execute(
            "SELECT 1 FROM messages "
            "WHERE turn_id = ? AND role = 'tool' AND tool_call_id = ? LIMIT 1",
            (turn_id, message.tool_call_id),
        ).fetchone()
        if duplicate_result is not None:
            raise StorageError(f"tool call already has a result: {message.tool_call_id}")

    @staticmethod
    def _row_to_session_context(row: sqlite3.Row) -> PersistedSessionContext:
        prompt_bytes = bytes(row["system_prompt_bytes"])
        tool_bytes = bytes(row["tool_schema_bytes"])
        prompt_fingerprint = str(row["system_prompt_fingerprint"])
        toolset_fingerprint = str(row["toolset_fingerprint"])
        if hashlib.sha256(prompt_bytes).hexdigest() != prompt_fingerprint:
            raise StorageError("persisted system prompt fingerprint mismatch")
        if hashlib.sha256(tool_bytes).hexdigest() != toolset_fingerprint:
            raise StorageError("persisted toolset fingerprint mismatch")
        binding_state = str(row["binding_state"])
        if binding_state not in {"verified", "legacy_unverified"}:
            raise StorageError(f"invalid session binding state: {binding_state}")
        return PersistedSessionContext(
            session_id=str(row["session_id"]),
            version=int(row["version"]),
            build_request=json.loads(row["build_request_json"] or "{}"),
            system_prompt_bytes=prompt_bytes,
            system_prompt_fingerprint=prompt_fingerprint,
            tool_schema_bytes=tool_bytes,
            toolset_fingerprint=toolset_fingerprint,
            tool_bindings=tuple(json.loads(row["tool_bindings_json"] or "[]")),
            source_versions=tuple(
                tuple(item) for item in json.loads(row["source_versions_json"] or "[]")
            ),
            binding_state=binding_state,
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        return Message(
            role=row["role"], content=row["content"], api_content=row["api_content"],
            tool_calls=json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else None,
            tool_call_id=row["tool_call_id"], tool_name=row["tool_name"],
            finish_reason=row["finish_reason"], effect_disposition=row["effect_disposition"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
