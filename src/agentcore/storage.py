from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Message, TurnError
from .tools import encode_tool_error


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    current_context_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_contexts (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    system_prompt TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session_id, version)
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    context_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'interrupted')),
    started_at REAL NOT NULL,
    completed_at REAL,
    final_message_id INTEGER,
    error_json TEXT,
    route_name TEXT,
    iterations INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS turns_session_status_idx ON turns(session_id, status);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT,
    api_content TEXT,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    finish_reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS messages_session_seq_idx ON messages(session_id, seq);
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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _context_fingerprint(system_prompt: str, tools: list[dict[str, Any]]) -> tuple[str, str]:
    tools_json = _canonical_json(tools)
    payload = _canonical_json({"system_prompt": system_prompt, "tools": json.loads(tools_json)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), tools_json


class SQLiteStore:
    def __init__(self, path: str | Path, *, tool_result_max_chars: int = 32_768) -> None:
        self.path = Path(path)
        self.tool_result_max_chars = tool_result_max_chars
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._connection is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(str(self.path), isolation_level=None)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            await connection.execute("PRAGMA journal_mode = WAL")
        await connection.executescript(_SCHEMA)
        await connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '1')"
        )
        self._connection = connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise StorageError("SQLiteStore is not open")
        return self._connection

    async def ensure_session_context(
        self,
        session_id: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
    ) -> SessionContextState:
        fingerprint, tools_json = _context_fingerprint(system_prompt, tools)
        now = time.time()
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone(
                    "SELECT current_context_version FROM sessions WHERE id = ?", (session_id,)
                )
                if row is None:
                    version = 1
                    await conn.execute(
                        "INSERT INTO sessions(id, current_context_version, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (session_id, version, now, now),
                    )
                    await conn.execute(
                        "INSERT INTO session_contexts(session_id, version, system_prompt, tools_json, "
                        "fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (session_id, version, system_prompt, tools_json, fingerprint, now),
                    )
                    await conn.commit()
                    return SessionContextState(version=version, changed=False)

                current = int(row["current_context_version"])
                context = await self._fetchone(
                    "SELECT fingerprint FROM session_contexts WHERE session_id = ? AND version = ?",
                    (session_id, current),
                )
                if context is not None and context["fingerprint"] == fingerprint:
                    await conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
                    await conn.commit()
                    return SessionContextState(version=current, changed=False)

                version = current + 1
                await conn.execute(
                    "INSERT INTO session_contexts(session_id, version, system_prompt, tools_json, "
                    "fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, version, system_prompt, tools_json, fingerprint, now),
                )
                await conn.execute(
                    "UPDATE sessions SET current_context_version = ?, updated_at = ? WHERE id = ?",
                    (version, now, session_id),
                )
                await conn.commit()
                return SessionContextState(version=version, changed=True)
            except Exception:
                await conn.rollback()
                raise

    async def recover_incomplete_turns(self, session_id: str) -> RecoveryState:
        warnings: list[str] = []
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "SELECT id FROM turns WHERE session_id = ? AND status = 'running' ORDER BY started_at",
                    (session_id,),
                )
                running = await cursor.fetchall()
                await cursor.close()
                for turn_row in running:
                    turn_id = str(turn_row["id"])
                    cursor = await conn.execute(
                        "SELECT * FROM messages WHERE turn_id = ? ORDER BY seq", (turn_id,)
                    )
                    rows = await cursor.fetchall()
                    await cursor.close()
                    messages = [self._row_to_message(row) for row in rows]
                    answered = {
                        message.tool_call_id
                        for message in messages
                        if message.role == "tool" and message.tool_call_id
                    }
                    missing = []
                    for message in messages:
                        for call in message.tool_calls or []:
                            if call.id not in answered:
                                missing.append(call)

                    for call in missing:
                        content = encode_tool_error(
                            "interrupted",
                            "The previous process exited before this tool call completed.",
                            self.tool_result_max_chars,
                        )
                        await self._insert_message_unlocked(
                            session_id,
                            turn_id,
                            Message(
                                role="tool",
                                content=content,
                                tool_call_id=call.id,
                                tool_name=call.name,
                            ),
                        )

                    last_row = rows[-1] if rows else None
                    last_message = messages[-1] if messages else None
                    recover_as_completed = bool(
                        not missing
                        and last_message
                        and last_message.role == "assistant"
                        and not last_message.tool_calls
                        and last_message.content
                        and last_message.content.strip()
                    )
                    status = "completed" if recover_as_completed else "failed"
                    error = None if recover_as_completed else _canonical_json(
                        {
                            "code": "recovered_incomplete_turn",
                            "message": "Recovered an unfinished turn after process interruption.",
                            "retryable": True,
                        }
                    )
                    await conn.execute(
                        "UPDATE turns SET status = ?, completed_at = ?, final_message_id = ?, error_json = ? "
                        "WHERE id = ? AND status = 'running'",
                        (
                            status,
                            time.time(),
                            int(last_row["id"]) if recover_as_completed and last_row else None,
                            error,
                            turn_id,
                        ),
                    )
                    warnings.append(f"recovered unfinished turn {turn_id} as {status}")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return RecoveryState(warnings=tuple(warnings))

    async def load_messages(self, session_id: str) -> list[Message]:
        async with self._lock:
            cursor = await self._conn().execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [self._row_to_message(row) for row in rows]

    async def begin_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        context_version: int,
        user_message: Message,
    ) -> int:
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    "INSERT INTO turns(id, session_id, context_version, status, started_at) "
                    "VALUES (?, ?, ?, 'running', ?)",
                    (turn_id, session_id, context_version, time.time()),
                )
                message_id = await self._insert_message_unlocked(
                    session_id, turn_id, user_message
                )
                await conn.commit()
                return message_id
            except Exception:
                await conn.rollback()
                raise

    async def append_message(self, session_id: str, turn_id: str, message: Message) -> int:
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                message_id = await self._insert_message_unlocked(session_id, turn_id, message)
                await conn.commit()
                return message_id
            except Exception:
                await conn.rollback()
                raise

    async def finish_turn_with_message(
        self,
        *,
        session_id: str,
        turn_id: str,
        message: Message,
        status: str,
        error: TurnError | None,
        route_name: str | None,
        iterations: int,
        usage: dict[str, int],
    ) -> bool:
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone("SELECT status FROM turns WHERE id = ?", (turn_id,))
                if row is None:
                    raise StorageError(f"unknown turn: {turn_id}")
                if row["status"] != "running":
                    await conn.commit()
                    return False
                message_id = await self._insert_message_unlocked(session_id, turn_id, message)
                await self._finish_turn_unlocked(
                    turn_id=turn_id,
                    session_id=session_id,
                    status=status,
                    error=error,
                    route_name=route_name,
                    iterations=iterations,
                    usage=usage,
                    final_message_id=message_id,
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def finish_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        status: str,
        error: TurnError | None,
        route_name: str | None,
        iterations: int,
        usage: dict[str, int],
    ) -> bool:
        async with self._lock:
            conn = self._conn()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone("SELECT status FROM turns WHERE id = ?", (turn_id,))
                if row is None:
                    raise StorageError(f"unknown turn: {turn_id}")
                if row["status"] != "running":
                    await conn.commit()
                    return False
                await self._finish_turn_unlocked(
                    turn_id=turn_id,
                    session_id=session_id,
                    status=status,
                    error=error,
                    route_name=route_name,
                    iterations=iterations,
                    usage=usage,
                    final_message_id=None,
                )
                await conn.commit()
                return True
            except Exception:
                await conn.rollback()
                raise

    async def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        async with self._lock:
            row = await self._fetchone("SELECT * FROM turns WHERE id = ?", (turn_id,))
        if row is None:
            return None
        result = dict(row)
        error_json = result.pop("error_json")
        result["error"] = json.loads(error_json) if error_json else None
        result["usage"] = json.loads(result.pop("usage_json"))
        return result

    async def get_contexts(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            cursor = await self._conn().execute(
                "SELECT * FROM session_contexts WHERE session_id = ? ORDER BY version", (session_id,)
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [dict(row) for row in rows]

    async def _finish_turn_unlocked(
        self,
        *,
        turn_id: str,
        session_id: str,
        status: str,
        error: TurnError | None,
        route_name: str | None,
        iterations: int,
        usage: dict[str, int],
        final_message_id: int | None,
    ) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(f"invalid terminal turn status: {status}")
        conn = self._conn()
        await conn.execute(
            "UPDATE turns SET status = ?, completed_at = ?, final_message_id = ?, error_json = ?, "
            "route_name = ?, iterations = ?, usage_json = ? WHERE id = ? AND status = 'running'",
            (
                status,
                time.time(),
                final_message_id,
                _canonical_json(error.model_dump()) if error else None,
                route_name,
                iterations,
                _canonical_json(usage),
                turn_id,
            ),
        )
        await conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id))

    async def _insert_message_unlocked(
        self, session_id: str, turn_id: str, message: Message
    ) -> int:
        conn = self._conn()
        row = await self._fetchone(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM messages WHERE session_id = ?",
            (session_id,),
        )
        next_seq = int(row["next_seq"])
        tool_calls_json = (
            _canonical_json([call.model_dump() for call in message.tool_calls])
            if message.tool_calls
            else None
        )
        cursor = await conn.execute(
            "INSERT INTO messages(session_id, turn_id, seq, role, content, api_content, "
            "tool_calls_json, tool_call_id, tool_name, finish_reason, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                turn_id,
                next_seq,
                message.role,
                message.content,
                message.api_content,
                tool_calls_json,
                message.tool_call_id,
                message.tool_name,
                message.finish_reason,
                _canonical_json(message.metadata),
                time.time(),
            ),
        )
        message_id = int(cursor.lastrowid)
        await cursor.close()
        return message_id

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> Message:
        return Message(
            role=row["role"],
            content=row["content"],
            api_content=row["api_content"],
            tool_calls=json.loads(row["tool_calls_json"]) if row["tool_calls_json"] else None,
            tool_call_id=row["tool_call_id"],
            tool_name=row["tool_name"],
            finish_reason=row["finish_reason"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    async def _fetchone(self, sql: str, params: tuple[Any, ...]) -> aiosqlite.Row | None:
        cursor = await self._conn().execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row
