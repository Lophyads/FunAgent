"""Atomic main records on one owned, thread-safe SQLite connection."""
from contextlib import contextmanager
from dataclasses import replace
from threading import RLock
from uuid import uuid4
import json
import base64
import sqlite3
import time
import hashlib
import re
import unicodedata

from ..frame.contracts import (DomainError, Message, ModelReplay, Session, SessionSnapshot,
                         Task, TurnRequest, ToolCall, ToolResult, RunResult, RecordScope, Page, thaw)




def _message_json(message):
    return json.dumps({"role": message.role, "content": message.content,
                       "calls": [{"call_id": c.call_id, "name": c.name, "args": thaw(c.args),
                                  "argument_error": c.argument_error}
                                 for c in message.calls], "call_id": message.call_id,
                       "status": message.status, "effect": message.effect,
                       **({"replay": {"version": message.replay.version, "format": message.replay.format,
                                      "scope": message.replay.scope, "items": thaw(message.replay.items)}}
                          if message.replay is not None else {})}, ensure_ascii=False, allow_nan=False)


def _message(data):
    data = json.loads(data)
    data["calls"] = tuple(ToolCall(**c) for c in data["calls"])
    if data.get("replay") is not None:
        data["replay"] = ModelReplay(**data["replay"])
    return Message(**data)


def _result_json(result):
    return json.dumps({name: thaw(getattr(result, name)) for name in (
        "status", "final", "reason", "error_code", "usage", "iterations", "attempts", "profile_id")}
        | {"tool_results": [{"call_id": r.call_id, "status": r.status, "content": r.content, "effect": r.effect}
                            for r in result.tool_results]}, ensure_ascii=False)


def _run_result(data):
    data = json.loads(data)
    data["tool_results"] = tuple(ToolResult(**r) for r in data["tool_results"])
    return RunResult(**data)


def _cursor(scope, after, through):
    return base64.urlsafe_b64encode(json.dumps([scope, after, through]).encode()).decode()


def _query(query, scope, maximum):
    if type(query.limit) is not int or not 1 <= query.limit <= 1000:
        raise DomainError("INVALID_REQUEST", "Page limit must be between 1 and 1000")
    if query.cursor is None:
        return 0, maximum
    try:
        stored_scope, after, through = json.loads(base64.b64decode(query.cursor, altchars=b"-_", validate=True))
        if stored_scope != scope or type(after) is not int or type(through) is not int or min(after, through) < 0 or through > maximum:
            raise ValueError
        return after, through
    except (ValueError, TypeError, UnicodeError):
        raise DomainError("INVALID_REQUEST", "Invalid page cursor") from None

#Todo 优化: 添加本地缓存数据，读优先读缓存数据
class SQLiteStore:
    def __init__(self, database):
        self._lock = RLock()
        self._closed = False
        try:
            self._db = sqlite3.connect(database, check_same_thread=False, timeout=5)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL, workspace TEXT NOT NULL, prompt TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id), text TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, finished_at REAL);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tool_calls (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id), call_id TEXT NOT NULL,
                    batch_message_id INTEGER NOT NULL REFERENCES messages(id),
                    result_message_id INTEGER REFERENCES messages(id), PRIMARY KEY(task_id,call_id));
                CREATE TABLE IF NOT EXISTS review_watermarks (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id), through_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS review_observed_turns (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id));
                CREATE TABLE IF NOT EXISTS review_triggers (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
                    completed_turns INTEGER NOT NULL, through_id INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS task_session ON tasks(session_id, seq);
                CREATE INDEX IF NOT EXISTS message_task ON messages(task_id, id);
            """)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(tasks)")}
            for name in ("request_key", "input_fingerprint"):
                if name not in columns:
                    self._db.execute("ALTER TABLE tasks ADD COLUMN " + name + " TEXT")
            self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS task_request ON tasks(session_id,request_key) WHERE request_key IS NOT NULL")
            self._db.commit()
        except sqlite3.Error:
            if hasattr(self, "_db"):
                self._db.close()
            raise DomainError("STORAGE", "Database initialization failed") from None

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed:
                raise DomainError("NOT_READY", "Database closed")
            try:
                with self._db:
                    yield self._db
            except sqlite3.Error:
                raise DomainError("STORAGE", "Database operation failed") from None

    def _session_row(self, db, session_id):
        row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Session not found")
        return row

    def _task_row(self, db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "Task not found")
        return row

    def _session(self, row):
        return Session(row["session_id"], row["title"], row["workspace"], row["prompt"], row["created_at"])

    def _task(self, row):
        return Task(row["task_id"], TurnRequest(row["session_id"], row["text"], row["request_key"]), row["status"],
                    result=_run_result(row["result"]) if row["result"] else None,
                    created_at=row["created_at"], finished_at=row["finished_at"])

    def _snapshot(self, db, session_id):
        session = self._session(self._session_row(db, session_id))
        rows = db.execute("SELECT m.id,m.task_id,m.payload FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                          "WHERE t.session_id=? ORDER BY m.id", (session_id,)).fetchall()
        return SessionSnapshot(session, tuple(replace(_message(r["payload"]), message_id=r["id"], task_id=r["task_id"])
                                               for r in rows), rows[-1]["id"] if rows else 0)

    def CreateSession(self, request):
        if not isinstance(request.title, str) or not request.title.strip() or not isinstance(request.workspace, str) or not request.workspace.strip():
            raise DomainError("INVALID_REQUEST", "Session requires title and workspace")
        with self._transaction() as db:
            session_id = uuid4().hex
            db.execute("INSERT INTO sessions(session_id,title,workspace,created_at) VALUES(?,?,?,?)",
                       (session_id, request.title, request.workspace, time.time()))
            return self._session(self._session_row(db, session_id))

    def LoadSession(self, session_id):
        with self._transaction() as db:
            return self._snapshot(db, session_id)

    def FreezePrompt(self, session_id, text):
        if not isinstance(text, str) or not text.strip():
            raise DomainError("INVALID_REQUEST", "Prompt must be nonempty")
        with self._transaction() as db:
            row = self._session_row(db, session_id)
            if row["prompt"] is not None and row["prompt"] != text:
                raise DomainError("CONFLICT", "Session prompt is frozen")
            if row["prompt"] is None:
                db.execute("UPDATE sessions SET prompt=? WHERE session_id=?", (text, session_id))
            return text

    @staticmethod
    def _fingerprint(request):
        if not isinstance(request.text, str) or not request.text.strip() or len(request.text) > 16000:
            raise DomainError("INVALID_REQUEST", "Invalid turn text")
        if request.request_key is not None and (not isinstance(request.request_key, str) or
                re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request.request_key) is None):
            raise DomainError("INVALID_REQUEST", "Invalid request key")
        canonical = unicodedata.normalize("NFC", request.text.replace("\r\n", "\n").replace("\r", "\n"))
        return hashlib.sha256(canonical.encode("utf8")).hexdigest()

    def _find_turn(self, db, request, fingerprint):
        if request.request_key is None:
            return None
        row = db.execute("SELECT * FROM tasks WHERE session_id=? AND request_key=?",
                         (request.session_id, request.request_key)).fetchone()
        if row is None:
            return None
        if row["input_fingerprint"] != fingerprint:
            raise DomainError("CONFLICT", "Request key already has different content")
        return self._task(row)

    def FindTurn(self, request):
        fingerprint = self._fingerprint(request)
        with self._transaction() as db:
            return self._find_turn(db, request, fingerprint)

    def RegisterTurn(self, request):
        fingerprint = self._fingerprint(request)
        with self._transaction() as db:
            # Independent connections serialize the lookup and unique insertion.
            db.execute("BEGIN IMMEDIATE")
            self._session_row(db, request.session_id)
            existing = self._find_turn(db, request, fingerprint)
            if existing is not None:
                return existing
            task_id = uuid4().hex
            db.execute("INSERT INTO tasks(task_id,session_id,text,status,created_at,request_key,input_fingerprint) "
                       "VALUES(?,?,?,'queued',?,?,?)",
                       (task_id, request.session_id, request.text, time.time(), request.request_key, fingerprint))
            return self._task(self._task_row(db, task_id))

    def BeginTurn(self, task_id):
        with self._transaction() as db:
            task = self._task_row(db, task_id)
            if task["status"] != "queued":
                raise DomainError("CONFLICT", "Task cannot begin")
            db.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task_id,))
            db.execute("INSERT INTO messages(task_id,payload) VALUES(?,?)",
                       (task_id, _message_json(Message("user", task["text"]))))
            return self._snapshot(db, task["session_id"])

    def GetTask(self, task_id):
        with self._transaction() as db:
            return self._task(self._task_row(db, task_id))

    def _running(self, db, task_id):
        task = self._task_row(db, task_id)
        if task["status"] != "running":
            raise DomainError("CONFLICT", "Task is not running")
        return task

    def _scope(self, scope):
        if not isinstance(scope, RecordScope) or scope.kind != "main_history" or not scope.turn_id:
            raise DomainError("INVALID_REQUEST", "Main history requires a Turn")
        return scope.turn_id

    def AppendToolCalls(self, scope, message):
        task_id = self._scope(scope)
        if message.role != "assistant" or not message.calls:
            raise DomainError("INVALID_REQUEST", "Expected complete tool batch")
        ids = [c.call_id for c in message.calls]
        if len(set(ids)) != len(ids) or any(not isinstance(key, str) or not key for key in ids):
            raise DomainError("INVALID_REQUEST", "Invalid tool call IDs")
        with self._transaction() as db:
            self._running(db, task_id)
            existing = {r[0] for r in db.execute("SELECT call_id FROM tool_calls WHERE task_id=?", (task_id,))}
            if existing.intersection(ids):
                raise DomainError("CONFLICT", "Tool call already recorded")
            if db.execute("SELECT 1 FROM tool_calls WHERE task_id=? AND result_message_id IS NULL", (task_id,)).fetchone():
                raise DomainError("CONFLICT", "Previous tool batch is incomplete")
            batch_id = db.execute("INSERT INTO messages(task_id,payload) VALUES(?,?)",
                                  (task_id, _message_json(message))).lastrowid
            db.executemany("INSERT INTO tool_calls(task_id,call_id,batch_message_id) VALUES(?,?,?)",
                           [(task_id, key, batch_id) for key in ids])

    def _append_result(self, db, task_id, result):
        row = db.execute("SELECT * FROM tool_calls WHERE task_id=? AND call_id=?", (task_id, result.call_id)).fetchone()
        if row is None or row["result_message_id"] is not None:
            raise DomainError("CONFLICT", "Tool result cannot be paired")
        message = Message("tool", result.content, call_id=result.call_id, status=result.status, effect=result.effect)
        result_id = db.execute("INSERT INTO messages(task_id,payload) VALUES(?,?)",
                               (task_id, _message_json(message))).lastrowid
        db.execute("UPDATE tool_calls SET result_message_id=? WHERE task_id=? AND call_id=?",
                   (result_id, task_id, result.call_id))

    def AppendToolResult(self, scope, result):
        task_id = self._scope(scope)
        with self._transaction() as db:
            self._running(db, task_id)
            self._append_result(db, task_id, result)

    def _finish(self, db, task_id, result, status):
        task = self._task_row(db, task_id)
        if task["status"] not in {"queued", "running"}:
            raise DomainError("CONFLICT", "Task already ended")
        if status == "completed" and (task["status"] != "running" or not isinstance(result.final, str) or not result.final.strip()):
            raise DomainError("INVALID_REQUEST", "Completed Turn requires a final reply")
        known = {r.call_id: r for r in result.tool_results}
        pending = db.execute("SELECT call_id FROM tool_calls WHERE task_id=? AND result_message_id IS NULL "
                             "ORDER BY batch_message_id,rowid", (task_id,)).fetchall()
        if status == "completed" and any(r["call_id"] not in known for r in pending):
            raise DomainError("CONFLICT", "Cannot complete with unresolved tool calls")
        for row in pending:
            actual = known.get(row["call_id"], ToolResult(row["call_id"], "unknown", "Tool outcome unknown", "unknown"))
            self._append_result(db, task_id, actual)
        if status == "completed":
            db.execute("INSERT INTO messages(task_id,payload) VALUES(?,?)",
                       (task_id, _message_json(Message("assistant", result.final, replay=result.replay))))
        db.execute("UPDATE tasks SET status=?,result=?,finished_at=? WHERE task_id=?",
                   (status, _result_json(result), time.time(), task_id))
        return self._task(self._task_row(db, task_id))

    def FinishTurn(self, task_id, result):
        if result.status not in {"completed", "failed", "limit", "cancelled"}:
            raise DomainError("INVALID_REQUEST", "Invalid Turn result")
        with self._transaction() as db:
            return self._finish(db, task_id, result, "failed" if result.status == "limit" else result.status)

    def RepairInterrupted(self):
        with self._transaction() as db:
            tasks = db.execute("SELECT task_id FROM tasks WHERE status IN ('queued','running') ORDER BY seq").fetchall()
            for task in tasks:
                self._finish(db, task["task_id"], RunResult("failed", reason="INTERRUPTED", error_code="INTERRUPTED"), "interrupted")
            return len(tasks)

    def ReadHistory(self, session_id, query):
        # A complete Turn is the paging unit. limit is a soft message budget,
        # so even a large tool batch cannot be separated from its results.
        with self._transaction() as db:
            self._session_row(db, session_id)
            maximum = db.execute("SELECT COALESCE(MAX(m.id),0) FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                                 "WHERE t.session_id=? AND t.status NOT IN ('queued','running')", (session_id,)).fetchone()[0]
            if query.direction not in {"forward", "backward"}:
                raise DomainError("INVALID_REQUEST", "Invalid history direction")
            if query.direction == "backward":
                return self._history_backwards(db, session_id, query, maximum)
            after, through = _query(query, "history:" + session_id, maximum)
            turns = db.execute("SELECT t.seq,t.task_id FROM tasks t JOIN messages m ON m.task_id=t.task_id "
                               "WHERE t.session_id=? AND t.seq>? AND t.status NOT IN ('queued','running') "
                               "GROUP BY t.task_id HAVING MAX(m.id)<=? ORDER BY t.seq", (session_id, after, through)).fetchall()
            items = []
            cursor = None
            for index, turn in enumerate(turns):
                items.extend(replace(_message(r["payload"]), message_id=r["id"], task_id=turn["task_id"])
                             for r in db.execute("SELECT id,payload FROM messages WHERE task_id=? ORDER BY id", (turn["task_id"],)))
                if len(items) >= query.limit:
                    if index + 1 < len(turns):
                        cursor = _cursor("history:" + session_id, turn["seq"], through)
                    break
            return Page(tuple(items), cursor)

    def _history_backwards(self, db, session_id, query, maximum):
        before, through = _query(query, "history-back:" + session_id, maximum)
        turns = db.execute("SELECT t.seq,t.task_id FROM tasks t JOIN messages m ON m.task_id=t.task_id "
                           "WHERE t.session_id=? AND (?=0 OR t.seq<?) AND t.status NOT IN ('queued','running') "
                           "GROUP BY t.task_id HAVING MAX(m.id)<=? ORDER BY t.seq DESC",
                           (session_id, before, before, through)).fetchall()
        groups, count, cursor = [], 0, None
        for index, turn in enumerate(turns):
            group = tuple(replace(_message(r["payload"]), message_id=r["id"], task_id=turn["task_id"])
                          for r in db.execute("SELECT id,payload FROM messages WHERE task_id=? ORDER BY id", (turn["task_id"],)))
            groups.append(group)
            count += len(group)
            if count >= query.limit:
                if index + 1 < len(turns):
                    cursor = _cursor("history-back:" + session_id, turn["seq"], through)
                break
        return Page(tuple(m for group in reversed(groups) for m in group), cursor)

    def ListTasks(self, session_id, query):
        with self._transaction() as db:
            self._session_row(db, session_id)
            maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM tasks WHERE session_id=?", (session_id,)).fetchone()[0]
            before, through = _query(query, "tasks:" + session_id, maximum)
            rows = db.execute("SELECT * FROM tasks WHERE session_id=? AND seq<=? AND (?=0 OR seq<?) "
                              "ORDER BY seq DESC LIMIT ?", (session_id, through, before, before, query.limit + 1)).fetchall()
            page = rows[:query.limit]
            cursor = _cursor("tasks:" + session_id, page[-1]["seq"], through) if len(rows) > query.limit else None
            return Page(tuple(self._task(row) for row in page), cursor)

    def DataSchema(self):
        from .data import Schema
        with self._transaction() as db:
            return Schema(db)

    def ReadTable(self, table, query, *, detail=False):
        from .data import Read
        with self._transaction() as db:
            return Read(db, table, query, detail=detail)

    def ListSessions(self, query):
        with self._transaction() as db:
            maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM sessions").fetchone()[0]
            after, through = _query(query, "sessions", maximum)
            rows = db.execute("SELECT * FROM sessions WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",
                              (after, through, query.limit + 1)).fetchall()
            page = rows[:query.limit]
            cursor = _cursor("sessions", page[-1]["seq"], through) if len(rows) > query.limit else None
            return Page(tuple(self._session(row) for row in page), cursor)

    def ObserveReviewTurn(self, task_id, every_turns=10):
        """Observe a committed user Turn and return an outstanding trigger."""
        if type(every_turns) is not int or every_turns < 0:
            raise DomainError("INVALID_REQUEST", "Review frequency must be a nonnegative integer")
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task_row(db, task_id)
            if every_turns == 0:
                return None
            session_id = task["session_id"]
            through = self._completed_review_boundary(db, task)
            if through and db.execute("INSERT OR IGNORE INTO review_observed_turns(task_id) VALUES(?)", (task_id,)).rowcount:
                db.execute("INSERT INTO review_triggers(session_id,completed_turns,through_id) VALUES(?,1,0) "
                           "ON CONFLICT(session_id) DO UPDATE SET completed_turns=completed_turns+1", (session_id,))
                count = db.execute("SELECT completed_turns FROM review_triggers WHERE session_id=?", (session_id,)).fetchone()[0]
                if count % every_turns == 0:
                    db.execute("UPDATE review_triggers SET through_id=MAX(through_id,?) WHERE session_id=?", (through, session_id))
            return self._review_trigger(db, session_id)["trigger_pending_through"]

    def _completed_review_boundary(self, db, task):
        # Read only the Turn endpoints, independent of the size of its tool batches.
        if task["status"] != "completed":
            return 0
        first = db.execute("SELECT payload FROM messages WHERE task_id=? ORDER BY id LIMIT 1", (task["task_id"],)).fetchone()
        last = db.execute("SELECT id,payload FROM messages WHERE task_id=? ORDER BY id DESC LIMIT 1", (task["task_id"],)).fetchone()
        if not first or _message(first["payload"]).role != "user" or _message(last["payload"]).role != "assistant":
            return 0
        if db.execute("SELECT 1 FROM tool_calls WHERE task_id=? AND result_message_id IS NULL", (task["task_id"],)).fetchone():
            return 0
        return last["id"]

    def _review_trigger(self, db, session_id):
        self._session_row(db, session_id)
        row = db.execute("SELECT completed_turns,through_id FROM review_triggers WHERE session_id=?", (session_id,)).fetchone()
        watermark = db.execute("SELECT through_id FROM review_watermarks WHERE session_id=?", (session_id,)).fetchone()
        count, through = (row[0], row[1]) if row else (0, 0)
        reviewed = watermark[0] if watermark else 0
        return {"completed_turns": count, "trigger_through": through,
                "trigger_pending_through": through if through > reviewed else None}

    def ReviewTrigger(self, session_id):
        with self._transaction() as db:
            return self._review_trigger(db, session_id)

    def ReviewThrough(self, task_id):
        with self._transaction() as db:
            task = self._task_row(db, task_id)
            if task["status"] in {"queued", "running"}:
                raise DomainError("INVALID_REQUEST", "Review requires a committed Turn")
            return db.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE task_id=?", (task_id,)).fetchone()[0]

    def _review_boundary(self, db, session_id, identifier):
        if type(identifier) is not int or identifier < 0:
            raise DomainError("INVALID_REQUEST", "Invalid review boundary")
        if identifier == 0:
            return
        row = db.execute("SELECT t.status,MAX(m.id) AS last FROM tasks t JOIN messages m ON m.task_id=t.task_id "
                         "WHERE t.session_id=? AND t.task_id=(SELECT task_id FROM messages WHERE id=?) "
                         "GROUP BY t.task_id", (session_id, identifier)).fetchone()
        if row is None or row["status"] in {"queued", "running"} or row["last"] != identifier:
            raise DomainError("INVALID_REQUEST", "Review requires a complete Turn boundary")

    def ReviewRange(self, session_id, after_id, through_id):
        with self._transaction() as db:
            self._session_row(db, session_id)
            self._review_boundary(db, session_id, after_id)
            self._review_boundary(db, session_id, through_id)
            if through_id < after_id:
                raise DomainError("INVALID_REQUEST", "Review range is reversed")
            rows = db.execute("SELECT m.id,m.payload,t.status FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                              "WHERE t.session_id=? AND m.id>? AND m.id<=? ORDER BY m.id",
                              (session_id, after_id, through_id)).fetchall()
            if any(row["status"] in {"queued", "running"} for row in rows):
                raise DomainError("INVALID_REQUEST", "Review range contains an unfinished Turn")
            return tuple(replace(_message(row["payload"]), message_id=row["id"]) for row in rows)

    def ReviewWatermark(self, session_id):
        with self._transaction() as db:
            self._session_row(db, session_id)
            row = db.execute("SELECT through_id FROM review_watermarks WHERE session_id=?", (session_id,)).fetchone()
            return row[0] if row else 0

    def ReviewAnchors(self, session_id, through_id):
        """The last two already-reviewed conversation texts, oldest first."""
        with self._transaction() as db:
            self._session_row(db, session_id)
            self._review_boundary(db, session_id, through_id)
            row = db.execute("SELECT through_id FROM review_watermarks WHERE session_id=?", (session_id,)).fetchone()
            if through_id > (row[0] if row else 0):
                raise DomainError("INVALID_REQUEST", "Anchors must already be reviewed")
            rows = db.execute("SELECT m.id,m.payload FROM messages m JOIN tasks t ON t.task_id=m.task_id "
                              "WHERE t.session_id=? AND m.id<=? ORDER BY m.id DESC", (session_id, through_id))
            anchors = []
            for row in rows:
                message = _message(row["payload"])
                if message.role in {"user", "assistant"} and message.content.strip():
                    anchors.append(Message(message.role, message.content, message_id=row["id"]))
                    if len(anchors) == 2:
                        break
            return tuple(reversed(anchors))

    def AdvanceReview(self, session_id, expected_id, through_id):
        with self._transaction() as db:
            db.execute("BEGIN IMMEDIATE")
            self._session_row(db, session_id)
            self._review_boundary(db, session_id, expected_id)
            self._review_boundary(db, session_id, through_id)
            if through_id <= expected_id:
                raise DomainError("INVALID_REQUEST", "Review watermark must increase")
            row = db.execute("SELECT through_id FROM review_watermarks WHERE session_id=?", (session_id,)).fetchone()
            if (row[0] if row else 0) != expected_id:
                raise DomainError("CONFLICT", "Review watermark changed")
            db.execute("INSERT INTO review_watermarks(session_id,through_id) VALUES(?,?) "
                       "ON CONFLICT(session_id) DO UPDATE SET through_id=excluded.through_id", (session_id, through_id))

    def Close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
