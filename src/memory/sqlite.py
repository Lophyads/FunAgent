"""Local episodic records: Unicode search, stable pagination and atomic CAS."""
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4
import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from ..frame.contracts import DomainError, Page
from .contracts import MemoryRecord, MemoryRef, MemorySearchResult, UNSET
from .common import controlled_lock, normalize, text, validate_content, validate_query, validate_metadata, filter_clauses
from .search import Terms, Rank


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
            # Upgrade only this application's memory table; no legacy schema import.
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(memory_records)")}
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                for name in ("subject", "event_at", "created_at", "updated_at", "origin"):
                    if name not in columns:
                        self._db.execute("ALTER TABLE memory_records ADD COLUMN " + name + " TEXT")
                now = datetime.now(timezone.utc).isoformat()
                self._db.execute("UPDATE memory_records SET created_at=COALESCE(created_at,?),updated_at=COALESCE(updated_at,?)", (now, now))
                self._db.execute("CREATE INDEX IF NOT EXISTS memory_event ON memory_records(source_id,deleted,type,event_at)")
            self._fts = self._initialize_fts()
        except (OSError, sqlite3.Error):
            if self._db is not None:
                self._db.close()
            raise DomainError("MEMORY_STORAGE", "Memory database could not be opened") from None

    def _initialize_fts(self):
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(search_text, content='memory_records', content_rowid='seq', tokenize='unicode61')")
        except sqlite3.OperationalError as error:
            if "no such module: fts5" not in str(error).lower():
                raise
            return False
        self._db.executescript("""
            BEGIN IMMEDIATE;
            DROP TRIGGER IF EXISTS memory_fts_insert;
            DROP TRIGGER IF EXISTS memory_fts_delete;
            DROP TRIGGER IF EXISTS memory_fts_update;
            CREATE TRIGGER memory_fts_insert AFTER INSERT ON memory_records WHEN new.deleted=0 BEGIN
                INSERT INTO memory_fts(rowid,search_text) VALUES(new.seq,new.search_text);
            END;
            CREATE TRIGGER memory_fts_delete AFTER DELETE ON memory_records WHEN old.deleted=0 BEGIN
                INSERT INTO memory_fts(memory_fts,rowid,search_text) VALUES('delete',old.seq,old.search_text);
            END;
            CREATE TRIGGER memory_fts_update AFTER UPDATE OF search_text,deleted ON memory_records BEGIN
                INSERT INTO memory_fts(memory_fts,rowid,search_text) SELECT 'delete',old.seq,old.search_text WHERE old.deleted=0;
                INSERT INTO memory_fts(rowid,search_text) SELECT new.seq,new.search_text WHERE new.deleted=0;
            END;
            INSERT INTO memory_fts(memory_fts) VALUES('delete-all');
            INSERT INTO memory_fts(rowid,search_text) SELECT seq,search_text FROM memory_records WHERE deleted=0;
            COMMIT;
        """)
        return True

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
                            row["version"].removeprefix("deleted:"), tuple(json.loads(row["evidence"])), bool(row["deleted"]),
                            row["subject"], row["event_at"], row["created_at"], row["updated_at"], row["origin"])

    def Search(self, query, context):
        context.CheckCancelled()
        validate_query(query)
        if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
            return MemorySearchResult()
        terms = Terms(query.text)
        clauses, parameters = ["source_id=?"], [self.source_id]
        if terms:
            clauses.append("(" + " OR ".join("instr(search_text,?)>0" for _ in terms) + ")")
            parameters.extend(terms)
        if not query.include_deleted:
            clauses.append("deleted=0")
        if query.types:
            clauses.append("type IN (" + ",".join("?" for _ in query.types) + ")")
            parameters.extend(query.types)
        filters, values = filter_clauses(query)
        clauses.extend(filters)
        parameters.extend(values)
        with self._connection(context) as db:
            scores = {}
            if self._fts and terms:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                scores = dict(db.execute("SELECT rowid,bm25(memory_fts) FROM memory_fts WHERE memory_fts MATCH ?", (match,)))
                # FTS token normalization and substring/CJK fallback are independent candidates.
                clauses[1] = "(" + clauses[1] + " OR seq IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?))"
                parameters.insert(1 + len(terms), match)
            rows = db.execute("SELECT * FROM memory_records WHERE " + " AND ".join(clauses), parameters).fetchall()
            rows = sorted(rows, key=lambda row: Rank(row, terms, scores.get(row["seq"], 0)))[:query.limit]
            context.CheckCancelled()
            return MemorySearchResult(tuple(self._record(row) for row in rows), {self.source_id: "ok"},
                                      () if self._fts else (f"{self.source_id}:FTS_UNAVAILABLE",))

    def List(self, query, context):
        context.CheckCancelled()
        if (query.source_id != self.source_id or type(query.limit) is not int or not 1 <= query.limit <= 100 or
                query.type is not None and not text(query.type) or query.cursor is not None and not isinstance(query.cursor, str)):
            raise DomainError("INVALID_REQUEST", "Invalid memory list query")
        after = 0
        filters, values = filter_clauses(query)
        identity = [self.source_id, query.type, query.subject, query.event_from, query.event_to, query.origin]
        if query.cursor is not None:
            try:
                saved_filter, after = json.loads(base64.urlsafe_b64decode(query.cursor.encode("ascii")))
                if saved_filter != identity or type(after) is not int or after <= 0:
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise DomainError("INVALID_REQUEST", "Invalid memory cursor") from None
        clauses, parameters = ["source_id=?", "deleted=0", "seq>?"], [self.source_id, after]
        if query.type is not None:
            clauses.append("type=?")
            parameters.append(query.type)
        clauses.extend(filters)
        parameters.extend(values)
        with self._connection(context) as db:
            rows = db.execute("SELECT * FROM memory_records WHERE " + " AND ".join(clauses) +
                              " ORDER BY seq LIMIT ?", (*parameters, query.limit + 1)).fetchall()
            context.CheckCancelled()
            cursor = None
            if len(rows) > query.limit:
                rows = rows[:query.limit]
                cursor = base64.urlsafe_b64encode(json.dumps([identity, rows[-1]["seq"]]).encode()).decode()
            return Page(tuple(self._record(row) for row in rows), cursor)

    def Write(self, draft, context):
        context.CheckCancelled()
        validate_content(draft.content, draft.evidence, draft.type)
        subject, event_at, origin = validate_metadata(draft.subject, draft.event_at, draft.origin)
        if draft.source_id != self.source_id or not text(draft.write_key):
            raise DomainError("INVALID_REQUEST", "Invalid memory write identity")
        identity = [draft.content, draft.type, draft.evidence]
        if any(value is not None for value in (subject, event_at, origin)):
            identity.extend((subject, event_at, origin))
        original = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        with self._connection(context, write=True) as db:
            existing = db.execute("SELECT * FROM memory_records WHERE source_id=? AND write_key=?", (self.source_id, draft.write_key)).fetchone()
            if existing:
                if existing["original_hash"] != original or existing["deleted"]:
                    raise DomainError("CONFLICT", "Memory write identity already used")
                return self._record(existing)
            identifier = uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            db.execute("""INSERT INTO memory_records(source_id,record_id,type,content,search_text,version,evidence,write_key,original_hash,subject,event_at,created_at,updated_at,origin)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (self.source_id, identifier, draft.type, draft.content, normalize(draft.content), uuid4().hex,
                        json.dumps(draft.evidence, ensure_ascii=False), draft.write_key, original, subject, event_at, now, now, origin))
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
            subject, event_at, origin = validate_metadata(*(old[field] if getattr(patch, field) is UNSET else getattr(patch, field)
                                                            for field in ("subject", "event_at", "origin")))
            db.execute("UPDATE memory_records SET content=?,search_text=?,type=?,evidence=?,version=?,subject=?,event_at=?,origin=?,updated_at=? WHERE source_id=? AND record_id=?",
                       (patch.content, normalize(patch.content), category, json.dumps(patch.evidence, ensure_ascii=False), uuid4().hex,
                        subject, event_at, origin, datetime.now(timezone.utc).isoformat(),
                        self.source_id, ref.record_id))
            context.CheckCancelled()
            return self._record(db.execute("SELECT * FROM memory_records WHERE source_id=? AND record_id=?", (self.source_id, ref.record_id)).fetchone())

    def Delete(self, ref, expected_version, context):
        context.CheckCancelled()
        if ref.source_id != self.source_id or not text(expected_version):
            raise DomainError("INVALID_REQUEST", "Invalid memory reference or version")
        with self._connection(context, write=True) as db:
            receipt = context.purpose == "review" and bool(context.review_evidence)
            row = db.execute("SELECT * FROM memory_records WHERE source_id=? AND record_id=?", (ref.source_id, ref.record_id)).fetchone()
            if (receipt and row is not None and row["deleted"] and row["version"] == "deleted:" + expected_version
                    and tuple(json.loads(row["evidence"])) == context.review_evidence):
                return
            old = self._existing(db, ref, expected_version)
            # Existing tombstone fields retain a review deletion receipt. This
            # confirms only the same version and trusted evidence on a retry.
            db.execute("UPDATE memory_records SET deleted=1,version=?,evidence=?,updated_at=? WHERE source_id=? AND record_id=?",
                       ("deleted:" + expected_version if receipt else uuid4().hex,
                        json.dumps(context.review_evidence) if receipt else
                        json.dumps(context.memory_evidence) if context.purpose == "main" and context.memory_evidence else old["evidence"],
                        datetime.now(timezone.utc).isoformat(), self.source_id, ref.record_id))
            context.CheckCancelled()

    def Close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
