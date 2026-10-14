"""Atomic SQLite snapshots, separate from conversation and memory storage."""
import json
from pathlib import Path
import sqlite3
from threading import RLock
import time

from ..frame.contracts import DomainError


def Encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class EvalRepository:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS eval_runs (
                run_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                summary TEXT NOT NULL, snapshot TEXT NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS eval_created ON eval_runs(created_at DESC, run_id DESC)")
            self._db.commit()
            self.Recover()
        except BaseException:
            self._db.close()
            raise

    def _save(self, run):
        summary = {key: run[key] for key in ("run_id", "question", "source_ids", "top_k", "status",
                                            "created_at", "finished_at", "total_ms", "judge_model")}
        self._db.execute("INSERT OR REPLACE INTO eval_runs VALUES (?, ?, ?, ?)",
                         (run["run_id"], run["created_at"], Encode(summary), Encode(run)))

    def Create(self, run):
        with self._lock, self._db:
            self._save(run)

    def Get(self, run_id):
        with self._lock:
            row = self._db.execute("SELECT snapshot FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise DomainError("NOT_FOUND", "Evaluation not found")
            return json.loads(row["snapshot"])

    def Update(self, run_id, change):
        with self._lock, self._db:
            run = self.Get(run_id)
            change(run)
            self._save(run)

    def List(self, allowed, limit=50):
        with self._lock:
            items = []
            # Summaries avoid loading large evidence snapshots for the history list.
            for row in self._db.execute("SELECT summary FROM eval_runs ORDER BY created_at DESC, run_id DESC"):
                value = json.loads(row["summary"])
                if set(value["source_ids"]) <= allowed:
                    items.append(value)
                    if len(items) == limit:
                        break
            return items

    def Recover(self):
        with self._lock, self._db:
            rows = self._db.execute("SELECT summary FROM eval_runs").fetchall()
            for row in rows:
                summary = json.loads(row["summary"])
                if summary["status"] not in {"queued", "running"}:
                    continue
                run = self.Get(summary["run_id"])
                run.update(status="interrupted", finished_at=time.time(), error_code="INTERRUPTED")
                for result in run["source_results"]:
                    if result["retrieval_status"] in {"queued", "running"}:
                        result.update(retrieval_status="interrupted", error_code="INTERRUPTED")
                    if result["judge_status"] in {"queued", "running"}:
                        result.update(judge_status="interrupted", judge_error_code="INTERRUPTED")
                self._save(run)

    def Close(self):
        with self._lock:
            self._db.close()
