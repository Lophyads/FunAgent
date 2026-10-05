"""SQLite records, transactional FTS5 indexing and local review checkpoints."""
from __future__ import annotations

import base64
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .store import (
    KINDS, UNSET, MemoryBackendError, MemoryDraft, MemoryEntry, MemoryError, MemoryPage,
    MemoryPatch, MemoryQuery, MemorySearch, MemoryVersionConflict, MemoryWriteResult,
    memory_key as _key, normalized as _normalized, validate_text as _text, validate_draft, validate_query, validate_version,
)

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE entries (
    row_id INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('user','note','episode')),
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source_json TEXT NOT NULL,
    event_at TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
);
CREATE INDEX entries_kind_recent ON entries(kind,updated_at DESC,row_id DESC);
CREATE VIRTUAL TABLE entries_fts USING fts5(subject,content,content='entries',content_rowid='row_id');
CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid,subject,content) VALUES(new.row_id,new.subject,new.content);
END;
CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts,rowid,subject,content) VALUES('delete',old.row_id,old.subject,old.content);
END;
CREATE TRIGGER entries_au AFTER UPDATE OF subject,content ON entries BEGIN
    INSERT INTO entries_fts(entries_fts,rowid,subject,content) VALUES('delete',old.row_id,old.subject,old.content);
    INSERT INTO entries_fts(rowid,subject,content) VALUES(new.row_id,new.subject,new.content);
END;
CREATE TABLE review_state (
    session_id TEXT PRIMARY KEY,
    completed_count INTEGER NOT NULL DEFAULT 0,
    seen_position INTEGER NOT NULL DEFAULT 0,
    seen_turn_id TEXT,
    reviewed_position INTEGER NOT NULL DEFAULT 0,
    reviewed_fingerprint TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True, slots=True)
class ReviewState:
    completed_count: int = 0
    seen_position: int = 0
    reviewed_position: int = 0
    reviewed_fingerprint: str = ""


class SqliteMemoryStore:
    """Connections belong to individual calls, never to shared worker threads."""

    def __init__(self, path: Path | str, *, review_scope: tuple[str, ...] = ()) -> None:
        self.path = Path(path).expanduser().absolute()
        self._review_scope = review_scope
        self._closed = False
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise MemoryError("memory database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(write=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version == 0 and tables:
                raise MemoryError("path contains a different database; choose a dedicated memory database")
            if version not in (0, SCHEMA_VERSION):
                raise MemoryError(f"unsupported memory schema version {version}")
            if version == 0:
                statement = ""
                for line in SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ""
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif not {"entries", "entries_fts", "review_state"}.issubset({r[0] for r in tables}):
                raise MemoryError("invalid memory database schema")
            triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if not {"entries_ai", "entries_au", "entries_ad"}.issubset(triggers):
                raise MemoryError("memory database is missing indexing triggers")
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def _connection(self, *, write: bool = False):
        if self._closed:
            raise MemoryBackendError("memory store is closed")
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except sqlite3.Error as exc:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise MemoryBackendError(f"SQLite memory operation failed: {exc}") from exc
        except BaseException:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _entry(row) -> MemoryEntry:
        return MemoryEntry(row["id"], row["kind"], row["subject"], row["content"], row["version"],
                           json.loads(row["source_json"]), row["event_at"], row["created_at"], row["updated_at"])

    @staticmethod
    def _draft(draft: MemoryDraft) -> MemoryDraft:
        return validate_draft(draft)

    @staticmethod
    def _filter(kind, limit):
        validate_query(kind, limit)

    def add(self, draft: MemoryDraft) -> MemoryWriteResult:
        draft = self._draft(draft)
        key = _key(draft)
        with self._connection(write=True) as conn:
            old = conn.execute("SELECT * FROM entries WHERE dedupe_key=?", (key,)).fetchone()
            if old:
                return MemoryWriteResult(False, self._entry(old), "duplicate")
            now, entry_id = time.time(), str(uuid.uuid4())
            conn.execute("INSERT INTO entries(id,kind,subject,content,source_json,event_at,created_at,updated_at,dedupe_key) VALUES(?,?,?,?,?,?,?,?,?)",
                         (entry_id, draft.kind, draft.subject, draft.content, json.dumps(draft.source, ensure_ascii=False), draft.event_at, now, now, key))
            return MemoryWriteResult(True, self._entry(conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()))

    def list(self, query: MemoryQuery = MemoryQuery()) -> MemoryPage:
        self._filter(query.kind, query.limit)
        where, args = ("kind=?", [query.kind]) if query.kind else ("1", [])
        cursor_where, cursor_args = "", []
        if query.cursor:
            try:
                if len(query.cursor) > 1000:
                    raise ValueError()
                stamp, row_id, kind = json.loads(base64.urlsafe_b64decode(query.cursor))
                if kind != query.kind or type(stamp) not in (int, float) or not math.isfinite(stamp) or type(row_id) is not int:
                    raise ValueError()
                cursor_where, cursor_args = " AND (updated_at<? OR (updated_at=? AND row_id<?))", [stamp, stamp, row_id]
            except (ValueError, TypeError, UnicodeError):
                raise MemoryError("invalid memory cursor") from None
        with self._connection() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM entries WHERE " + where, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM entries WHERE " + where + cursor_where + " ORDER BY updated_at DESC,row_id DESC LIMIT ?",
                                [*args, *cursor_args, query.limit + 1]).fetchall()
        selected = rows[:query.limit]
        cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            cursor = base64.urlsafe_b64encode(json.dumps([last["updated_at"], last["row_id"], query.kind]).encode()).decode()
        return MemoryPage(tuple(map(self._entry, selected)), total, cursor)

    def search(self, query: MemorySearch) -> list[MemoryEntry]:
        self._filter(query.kind, query.limit)
        words = list(dict.fromkeys(re.findall(r"[^\W_]+", _text(query.query, blank=True, limit=1000).lower())))[:20]
        if not words:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        kinds, kind_args = (" AND e.kind=?", [query.kind]) if query.kind else ("", [])
        candidate_limit = min(800, query.limit * 4)
        with self._connection() as conn:
            conn.execute("BEGIN")
            fts_rows = conn.execute("SELECT e.*,bm25(entries_fts,2.0,1.0) AS score FROM entries_fts JOIN entries e ON e.row_id=entries_fts.rowid WHERE entries_fts MATCH ?" + kinds + " ORDER BY score,e.updated_at DESC LIMIT ?",
                                    [expression, *kind_args, candidate_limit]).fetchall()
            rows = {r["id"]: r for r in fts_rows}
            scores = {r["id"]: r["score"] for r in fts_rows}
            cjk = [w for w in words if CJK.search(w)]
            if cjk:
                conditions, patterns = [], []
                for word in cjk:
                    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    conditions.append("(e.subject LIKE ? ESCAPE '\\' OR e.content LIKE ? ESCAPE '\\')")
                    patterns.extend(["%" + escaped + "%"] * 2)
                extra = conn.execute("SELECT e.* FROM entries e WHERE (" + " OR ".join(conditions) + ")" + kinds + " ORDER BY updated_at DESC LIMIT ?",
                                     [*patterns, *kind_args, candidate_limit]).fetchall()
                rows.update({r["id"]: r for r in extra})
        def ranking(row):
            text = _normalized(row["subject"] + " " + row["content"])
            tokens = set(re.findall(r"[^\W_]+", text))
            coverage = sum(_normalized(word) in text if CJK.search(word) else _normalized(word) in tokens for word in words)
            stamp = row["updated_at"]
            if row["kind"] == "episode" and row["event_at"]:
                date = datetime.fromisoformat(row["event_at"])
                stamp = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
            return (-coverage, scores.get(row["id"], 0), -stamp, row["id"])
        return [self._entry(row) for row in sorted(rows.values(), key=ranking)[:query.limit]]

    @staticmethod
    def _version(version):
        validate_version(version)

    @staticmethod
    def _current(conn, entry_id, expected_version):
        if not isinstance(entry_id, str) or not entry_id:
            raise MemoryError("id must be a nonblank string")
        row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row and row["version"] != expected_version:
            raise MemoryVersionConflict("memory changed; reload the entry before saving")
        return row

    def update(self, entry_id: str, patch: MemoryPatch, *, expected_version: int) -> MemoryWriteResult:
        self._version(expected_version)
        with self._connection(write=True) as conn:
            row = self._current(conn, entry_id, expected_version)
            if row is None:
                return MemoryWriteResult(False, reason="missing")
            old = self._entry(row)
            draft = self._draft(MemoryDraft(old.kind, patch.content, old.subject if patch.subject is None else patch.subject,
                                           old.source if patch.source is None else patch.source, old.event_at if patch.event_at is UNSET else patch.event_at))
            key = _key(draft) if old.kind != "episode" else row["dedupe_key"]
            duplicate = conn.execute("SELECT id FROM entries WHERE dedupe_key=? AND id<>?", (key, entry_id)).fetchone()
            if duplicate:
                raise MemoryError("replacement duplicates another entry")
            conn.execute("UPDATE entries SET subject=?,content=?,source_json=?,event_at=?,dedupe_key=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                         (draft.subject, draft.content, json.dumps(draft.source, ensure_ascii=False), draft.event_at, key, time.time(), entry_id, expected_version))
            return MemoryWriteResult(True, self._entry(conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()))

    def delete(self, entry_id: str, *, expected_version: int) -> MemoryWriteResult:
        self._version(expected_version)
        with self._connection(write=True) as conn:
            row = self._current(conn, entry_id, expected_version)
            if row is None:
                return MemoryWriteResult(False, reason="missing")
            conn.execute("DELETE FROM entries WHERE id=? AND version=?", (entry_id, expected_version))
            return MemoryWriteResult(True, self._entry(row))

    def observe_turn(self, session_id: str, turn_id: str, position: int) -> int | None:
        session_id = self._review_id(session_id)
        with self._connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO review_state(session_id,updated_at) VALUES(?,?)", (session_id, time.time()))
            row = conn.execute("SELECT * FROM review_state WHERE session_id=?", (session_id,)).fetchone()
            if position <= row["seen_position"] or turn_id == row["seen_turn_id"]:
                return None
            count = row["completed_count"] + 1
            conn.execute("UPDATE review_state SET completed_count=?,seen_position=?,seen_turn_id=?,updated_at=? WHERE session_id=?",
                         (count, position, turn_id, time.time(), session_id))
            return count

    def review_state(self, session_id: str) -> ReviewState:
        session_id = self._review_id(session_id)
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM review_state WHERE session_id=?", (session_id,)).fetchone()
        return ReviewState(row["completed_count"], row["seen_position"], row["reviewed_position"], row["reviewed_fingerprint"]) if row else ReviewState()

    def mark_reviewed(self, session_id: str, position: int, fingerprint: str) -> None:
        session_id = self._review_id(session_id)
        with self._connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO review_state(session_id,updated_at) VALUES(?,?)", (session_id, time.time()))
            conn.execute("UPDATE review_state SET reviewed_position=?,reviewed_fingerprint=?,updated_at=? WHERE session_id=? AND reviewed_position<=?",
                         (position, fingerprint, time.time(), session_id, position))

    def _review_id(self, session_id: str) -> str:
        return json.dumps([*self._review_scope, session_id], ensure_ascii=False) if self._review_scope else session_id

    def close(self) -> None:
        self._closed = True
