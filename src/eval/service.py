"""Bounded background evaluation; source IO never belongs to HTTP requests."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from threading import Condition, Event, Thread
import re
import time
from uuid import uuid4

from ..frame.contracts import ControlError, DomainError, RunContext
from ..memory.contracts import MemoryQuery
from ..memory.prompt import RecordData
from .judge import JUDGE_PROMPT, PROMPT_VERSION


ERROR_CODES = frozenset({"CANCELLED", "DEADLINE", "INTERRUPTED", "DENIED", "UNSUPPORTED", "NOT_FOUND",
    "MEMORY_PROTOCOL", "MEMORY_STORAGE", "MEMORY_SOURCE", "MEMORY_TIMEOUT", "NOT_READY",
    "EVAL_JUDGE_PROTOCOL", "EVAL_JUDGE_ERROR", "AUTH", "MODEL_NOT_FOUND", "EMPTY_REPLY", "PROTOCOL",
    "TRANSPORT", "TIMEOUT", "RATE_LIMIT", "SERVER", "PROVIDER_FATAL", "TRUNCATED", "MAX_ITERATIONS"})


def ErrorCode(error):
    return error.code if isinstance(error, DomainError) and error.code in ERROR_CODES else "EVAL_ERROR"


class EvalService:
    def __init__(self, memory, operations, access, repository, judge, *, source_timeout=30,
                 judge_timeout=60, capacity=4, start_worker=True):
        self._memory, self._operations = memory, operations
        self._access = frozenset(access)
        self._repository, self._judge = repository, judge
        self._source_timeout, self._judge_timeout = source_timeout, judge_timeout
        self._capacity = capacity
        self._condition, self._cancel = Condition(), Event()
        self._pending, self._admitted = deque(), set()
        self._worker = Thread(target=self._work, name="funagent-eval")
        if start_worker:
            self.Start()

    def Start(self):
        self._worker.start()

    def IsBusy(self):
        with self._condition:
            return bool(self._admitted)

    def RebindMemory(self, memory, operations, judge):
        with self._condition:
            self._memory, self._operations, self._judge = memory, operations, judge

    def Sources(self):
        return [source for source in self._operations.Sources() if "search" in source["capabilities"]]

    def Options(self):
        return {"sources": self.Sources(), "judge_model": self._judge.model,
                "max_sources": 8, "max_top_k": 20, "prompt_version": PROMPT_VERSION}

    def _allowed(self):
        return {s["source_id"] for s in self.Sources()}

    def Submit(self, question, source_ids, top_k, request_key=None):
        if (not isinstance(question, str) or not question.strip() or len(question) > 8000
                or not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8
                or any(not isinstance(s, str) or not s or len(s) > 128 for s in source_ids)
                or len(set(source_ids)) != len(source_ids) or type(top_k) is not int or not 1 <= top_k <= 20):
            raise DomainError("INVALID_REQUEST", "Invalid evaluation input")
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r"[a-f0-9]{32}", request_key)):
            raise DomainError("INVALID_REQUEST", "Invalid evaluation request key")
        if not set(source_ids) <= self._allowed():
            raise DomainError("DENIED", "Evaluation source access denied")
        question = question.strip()
        run_id = request_key or uuid4().hex
        with self._condition:
            if self._cancel.is_set():
                raise DomainError("STOPPING", "Evaluation is stopping")
            if request_key:
                try:
                    previous = self._repository.Get(run_id)
                except DomainError as error:
                    if error.code != "NOT_FOUND":
                        raise
                else:
                    if (previous["question"], previous["source_ids"], previous["top_k"]) != (question, source_ids, top_k):
                        raise DomainError("CONFLICT", "Evaluation request key was already used")
                    return run_id
            if len(self._admitted) >= self._capacity:
                raise DomainError("CAPACITY", "Evaluation capacity reached")
            sources = {s["source_id"]: s for s in self.Sources()}
            run = {"schema_version": 1, "run_id": run_id, "question": question, "source_ids": source_ids,
                   "top_k": top_k, "status": "queued", "created_at": time.time(), "started_at": None,
                   "finished_at": None, "total_ms": None, "error_code": None,
                   "judge_model": dict(self._judge.model), "prompt_version": PROMPT_VERSION,
                   "judge_prompt": JUDGE_PROMPT, "source_results": []}
            for name in source_ids:
                run["source_results"].append({"source_id": name, "store": sources[name]["store"],
                    "retrieval_status": "queued", "judge_status": "queued", "records": [], "warnings": [],
                    "retrieval_ms": None, "judge_ms": None, "error_code": None, "judge_error_code": None,
                    "judge_input": None, "judgment": None, "judge_usage": None})
            self._repository.Create(run)
            self._admitted.add(run_id)
            self._pending.append(run_id)
            self._condition.notify()
            return run_id

    def Get(self, run_id):
        run = self._repository.Get(run_id)
        if not set(run["source_ids"]) <= self._allowed():
            raise DomainError("DENIED", "Evaluation source access denied")
        return run

    def List(self):
        return {"items": self._repository.List(self._allowed())}

    def _update_source(self, run_id, source_id, **values):
        def change(run):
            next(r for r in run["source_results"] if r["source_id"] == source_id).update(values)
        self._repository.Update(run_id, change)

    def _context(self, run_id, timeout):
        # Reuse management authorization; no tools or write operations are exposed by Eval.
        return RunContext(run_id=run_id, purpose="management", access=self._access,
                          cancel=self._cancel, deadline=time.monotonic() + timeout)

    def _retrieve(self, run):
        def search(source_id):
            started = time.monotonic()
            self._update_source(run["run_id"], source_id, retrieval_status="running")
            try:
                context = self._context(run["run_id"], self._source_timeout)
                context.CheckCancelled()
                result = self._memory.Search(MemoryQuery(text=run["question"], source_ids=(source_id,),
                                                        limit=run["top_k"]), context)
                context.CheckCancelled()
                status = result.source_status.get(source_id, "failed")
                if status == "ok":
                    self._update_source(run["run_id"], source_id,
                        retrieval_status="completed" if result.records else "empty",
                        records=[RecordData(r) for r in result.records], warnings=list(result.warnings))
                else:
                    self._update_source(run["run_id"], source_id, retrieval_status=status,
                        judge_status="skipped", error_code="DEADLINE" if status == "timeout" else "MEMORY_SOURCE",
                        warnings=list(result.warnings))
            except Exception as error:
                self._update_source(run["run_id"], source_id,
                    retrieval_status="interrupted" if self._cancel.is_set() else "timeout" if
                    isinstance(error, ControlError) and error.code == "DEADLINE" else "failed",
                    judge_status="skipped", error_code=ErrorCode(error))
            finally:
                self._update_source(run["run_id"], source_id, retrieval_ms=(time.monotonic() - started) * 1000)

        with ThreadPoolExecutor(max_workers=min(4, len(run["source_ids"])),
                                thread_name_prefix="funagent-eval-retrieval") as pool:
            for future in as_completed([pool.submit(search, name) for name in run["source_ids"]]):
                future.result()

    def _execute(self, run_id):
        started = time.monotonic()
        self._repository.Update(run_id, lambda run: run.update(status="running", started_at=time.time()))
        try:
            self._context(run_id, self._source_timeout).CheckCancelled()
            self._retrieve(self._repository.Get(run_id))
            for source in self._repository.Get(run_id)["source_results"]:
                self._context(run_id, self._judge_timeout).CheckCancelled()
                name = source["source_id"]
                if source["retrieval_status"] == "empty":
                    self._update_source(run_id, name, judge_status="skipped")
                    continue
                if source["retrieval_status"] != "completed":
                    continue
                judge_started = time.monotonic()
                try:
                    payload = self._judge.Input(self._repository.Get(run_id)["question"], source["records"])
                    self._update_source(run_id, name, judge_status="running", judge_input=payload)
                    judgment, usage = self._judge.Grade(payload, self._context(run_id, self._judge_timeout))
                    self._cancel_check()
                    self._update_source(run_id, name, judge_status="completed", judgment=judgment, judge_usage=usage)
                except Exception as error:
                    self._update_source(run_id, name, judge_status="interrupted" if self._cancel.is_set() else "failed",
                                        judge_error_code=ErrorCode(error))
                finally:
                    self._update_source(run_id, name, judge_ms=(time.monotonic() - judge_started) * 1000)
            results = self._repository.Get(run_id)["source_results"]
            successful = sum(r["retrieval_status"] == "empty" or
                             r["retrieval_status"] == "completed" and r["judge_status"] == "completed" for r in results)
            self._cancel_check()
            status = "completed" if successful == len(results) else "partial" if successful else "failed"
            self._repository.Update(run_id, lambda run: run.update(status=status, finished_at=time.time(),
                                                                   total_ms=(time.monotonic() - started) * 1000))
        except Exception as error:
            def fail(run):
                run.update(status="interrupted" if self._cancel.is_set() else "failed", error_code=ErrorCode(error),
                           finished_at=time.time(), total_ms=(time.monotonic() - started) * 1000)
                for result in run["source_results"]:
                    if result["retrieval_status"] in {"queued", "running"}:
                        result.update(retrieval_status="interrupted" if self._cancel.is_set() else "failed", error_code=ErrorCode(error))
                    if result["judge_status"] in {"queued", "running"}:
                        result.update(judge_status="interrupted" if self._cancel.is_set() else "failed", judge_error_code=ErrorCode(error))
            self._repository.Update(run_id, fail)

    def _cancel_check(self):
        if self._cancel.is_set():
            raise ControlError("CANCELLED")

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._cancel.is_set())
                if not self._pending:
                    return
                run_id = self._pending.popleft()
            try:
                self._execute(run_id)
            except Exception:
                # Persistence failures leave the durable snapshot recoverable on restart.
                pass
            finally:
                with self._condition:
                    self._admitted.discard(run_id)

    def RequestStop(self):
        with self._condition:
            self._cancel.set()
            self._condition.notify_all()

    def Stop(self):
        self.RequestStop()
        if self._worker.ident is not None:
            self._worker.join()

    def IsStopped(self):
        return not self._worker.is_alive()
