"""Local episodic records: Unicode search, stable pagination and atomic CAS."""
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4
import base64
import hashlib
import json
import sqlite3

from ..contracts import DomainError, Page
from .contracts import MemoryRecord, MemoryRef, MemorySearchResult
from .common import controlled_lock, normalize, text, validate_content, validate_query


class SQLiteMemoryStore:
    capabilities = frozenset({"search", "list", "write", "update", "delete"})

    def __init__(self, path, source_id="facts", kind="episodic"):
        if not text(source_id) or kind != "episodic":
            raise DomainError("CONFIG_INVALID", "SQLite memory requires an episodic source")
        self.source_id, self.kind = source_id, kind
        self._lock, self._closed = Lock(), False
        self._db = None
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL, record_id TEXT NOT NULL,
                    type TEXT NOT NULL, content TEXT NOT NULL, search_text TEXT NOT NULL,
                    version TEXT NOT NULL, evidence TEXT NOT NULL,
                    write_key TEXT NOT NULL, original_hash TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_id, record_id), UNIQUE(source_id, write_key)
                );
                CREATE INDEX IF NOT EXISTS memory_type ON memory_records(source_id, deleted, type, seq);
            """)
        except (OSError, sqlite3.Error):
            if self._db is not None:
                self._db.close()
            raise DomainError("MEMORY_STORAGE", "Memory database could not be opened") from None

    @contextmanager
    def _connection(self, context, *, write=False):
        with controlled_lock(self._lock, context):
            if self._closed:
                raise DomainError("NOT_READY", "Memory store is closed")
            try:
                milliseconds = int(min(context.Remaining(), 5) * 1000)
                self._db.execute(f"PRAGMA busy_timeout={milliseconds}")
                with self._db:
                    if write:
                        self._db.execute("BEGIN IMMEDIATE")
                        context.CheckCancelled()
                    yield self._db
            except sqlite3.Error:
                context.CheckCancelled()
                raise DomainError("MEMORY_STORAGE", "Memory database operation failed") from None

    def _record(self, row):
        return MemoryRecord(MemoryRef(self.source_id, row["record_id"]), self.kind,
                            row["type"], row["content"], f"memory://{self.source_id}/{row['record_id']}",
                            row["version"], tuple(json.loads(row["evidence"])))

    def Search(self, query, context):
        context.CheckCancelled()
        validate_query(query)
        if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
            return MemorySearchResult()
        clauses, parameters = ["source_id=?", "deleted=0", "instr(search_text,?)>0"], [self.source_id, normalize(query.text)]
        if query.types:
            clauses.append("type IN (" + ",".join("?" for _ in query.types) + ")")
            parameters.extend(query.types)
        with self._connection(context) as db:
            rows = db.execute("SELECT * FROM memory_records WHERE " + " AND ".join(clauses) +
                              " ORDER BY seq LIMIT ?", (*parameters, query.limit)).fetchall()
            context.CheckCancelled()
            return MemorySearchResult(tuple(self._record(row) for row in rows), {self.source_id: "ok"})

    def List(self, query, context):
        context.CheckCancelled()
        if query.source_id != self.source_id or type(query.limit) is not int or not 1 <= query.limit <= 100 or (query.type is not None and not text(query.type)):
            raise DomainError("INVALID_REQUEST", "Invalid memory list query")
        after = 0
        if query.cursor is not None:
            try:
                source, category, after = json.loads(base64.urlsafe_b64decode(query.cursor.encode("ascii")))
                if source != self.source_id or category != query.type or type(after) is not int or after <= 0:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise DomainError("INVALID_REQUEST", "Invalid memory cursor") from None
        clauses, parameters = ["source_id=?", "deleted=0", "seq>?"], [self.source_id, after]
        if query.type is not None:
            clauses.append("type=?")
            parameters.append(query.type)
        with self._connection(context) as db:
            rows = db.execute("SELECT * FROM memory_records WHERE " + " AND ".join(clauses) +
                              " ORDER BY seq LIMIT ?", (*parameters, query.limit + 1)).fetchall()
            context.CheckCancelled()
            cursor = None
            if len(rows) > query.limit:
                rows = rows[:query.limit]
                cursor = base64.urlsafe_b64encode(json.dumps([self.source_id, query.type, rows[-1]["seq"]]).encode()).decode()
            return Page(tuple(self._record(row) for row in rows), cursor)

    def Write(self, draft, context):
        context.CheckCancelled()
        validate_content(draft.content, draft.evidence, draft.type)
        if draft.source_id != self.source_id or not text(draft.write_key):
            raise DomainError("INVALID_REQUEST", "Invalid memory write identity")
        original = hashlib.sha256(json.dumps([draft.content, draft.type, draft.evidence], ensure_ascii=False).encode()).hexdigest()
        with self._connection(context, write=True) as db:
            existing = db.execute("SELECT * FROM memory_records WHERE source_id=? AND write_key=?", (self.source_id, draft.write_key)).fetchone()
            if existing:
                if existing["original_hash"] != original or existing["deleted"]:
                    raise DomainError("CONFLICT", "Memory write identity already used")
                return self._record(existing)
            identifier = uuid4().hex
            db.execute("""INSERT INTO memory_records(source_id,record_id,type,content,search_text,version,evidence,write_key,original_hash)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (self.source_id, identifier, draft.type, draft.content, normalize(draft.content), uuid4().hex,
                        json.dumps(draft.evidence, ensure_ascii=False), draft.write_key, original))
            context.CheckCancelled()
            return self._record(db.execute("SELECT * FROM memory_records WHERE source_id=? AND record_id=?", (self.source_id, identifier)).fetchone())

    def _existing(self, db, ref, expected_version):
        if ref.source_id != self.source_id or not text(expected_version):
            raise DomainError("INVALID_REQUEST", "Invalid memory reference or version")
        row = db.execute("SELECT * FROM memory_records WHERE source_id=? AND record_id=? AND deleted=0", (self.source_id, ref.record_id)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Memory record not found")
        if row["version"] != expected_version:
            raise DomainError("CONFLICT", "Memory version changed")
        return row

    def Update(self, ref, patch, expected_version, context):
        context.CheckCancelled()
        with self._connection(context, write=True) as db:
            old = self._existing(db, ref, expected_version)
            category = old["type"] if patch.type is None else patch.type
            validate_content(patch.content, patch.evidence, category)
            db.execute("UPDATE memory_records SET content=?,search_text=?,type=?,evidence=?,version=? WHERE source_id=? AND record_id=?",
                       (patch.content, normalize(patch.content), category, json.dumps(patch.evidence, ensure_ascii=False), uuid4().hex,
                        self.source_id, ref.record_id))
            context.CheckCancelled()
            return self._record(db.execute("SELECT * FROM memory_records WHERE source_id=? AND record_id=?", (self.source_id, ref.record_id)).fetchone())

    def Delete(self, ref, expected_version, context):
        context.CheckCancelled()
        with self._connection(context, write=True) as db:
            self._existing(db, ref, expected_version)
            db.execute("UPDATE memory_records SET deleted=1,version=? WHERE source_id=? AND record_id=?", (uuid4().hex, self.source_id, ref.record_id))
            context.CheckCancelled()

    def Close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
