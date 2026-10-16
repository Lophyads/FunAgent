"""A bounded presentation projection. Callers serialize access with EventMgr's lock."""
from collections import OrderedDict, deque
from copy import deepcopy
from uuid import uuid4
import json
import math

TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
RESULT_CODES = frozenset({"INVALID_REQUEST", "NOT_FOUND", "METHOD_NOT_ALLOWED", "CAPACITY", "STOPPING", "NOT_READY",
    "DENIED", "UNSUPPORTED", "MANAGEMENT_STALE", "MANAGEMENT_EXPIRED", "MEMORY_STORAGE", "TOPIC_SYNC_FAILED", "CONFLICT",
    "INTERACTION_STALE", "INTERACTION_EXPIRED", "FORBIDDEN", "PAYLOAD_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE",
    "STORAGE", "SUBMIT_FAILED", "INTERNAL_ERROR", "CANCELLED", "DEADLINE", "INTERRUPTED", "MAX_ITERATIONS",
    "PROFILES_EXHAUSTED", "TIMEOUT", "TRANSPORT", "PROTOCOL", "TRUNCATED", "PROVIDER_FATAL", "AUTH",
    "MODEL_NOT_FOUND", "EMPTY_REPLY", "RATE_LIMIT", "SERVER", "CONFIG_REFERENCE", "INVALID_STRATEGY"})
KINDS = {"task_queued": "turn.submitted", "task_started": "turn.admitted",
         "cancel_requested": "turn.cancel_requested", "phase_started": "turn.phase_started",
         "phase_finished": "turn.phase_finished", "phase_failed": "turn.phase_failed",
         "attempt_started": "model.started", "text_delta": "model.delta",
         "attempt_accepted": "model.accepted", "attempt_reset": "model.discarded",
         "tool_started": "tool.started", "tool_finished": "tool.finished",
         "interaction_pending": "interaction.pending", "interaction_resolved": "interaction.resolved",
         "interaction_cleared": "interaction.cleared", "task_committed": "turn.finished"}


def ResultView(result, status):
    return {"status": status, "usage": {k: v for k, v in result.usage.items()
            if k in {"total_tokens", "prompt_tokens", "completion_tokens"} and type(v) in {int, float} and math.isfinite(v)},
            "iterations": None if result.reason == 'INTERRUPTED' else result.iterations,
            "attempts": None if result.reason == 'INTERRUPTED' else result.attempts, "route": result.profile_id,
            "tool_calls": None if result.reason == 'INTERRUPTED' else len(result.tool_results),
            "error": {"code": (result.error_code or result.reason) if (result.error_code or result.reason) in RESULT_CODES else 'INTERNAL_ERROR'} if status != "completed" else None,
            "tool_outcomes": [{"tool_call_id": r.call_id, "state": ToolState(r.status), "effect": r.effect}
                              for r in result.tool_results[-128:]]}


def TaskView(task):
    return {"turn_id": task.task_id, "session_id": task.request.session_id, "input": task.request.text,
            "status": task.status, "created_at": task.created_at, "finished_at": task.finished_at,
            "result": None if task.result is None else ResultView(task.result, task.status)}


def ToolState(status):
    return {"ok": "succeeded", "error": "failed", "cancelled": "not_executed"}.get(status, status)


class WebProjection:
    def __init__(self, *, capacity=4096, max_bytes=8 * 1024 * 1024, retained=200, max_text=16384):
        self.epoch = uuid4().hex
        self.seq = 0
        self.events, self.views = deque(), OrderedDict()
        self.bytes = 0
        self.capacity, self.max_bytes, self.retained, self.max_text = capacity, max_bytes, retained, max_text

    def Publish(self, event):
        if not event.session_id or event.kind not in KINDS:
            return False
        previous = self.views.get(event.task_id)
        if previous and previous['status'] in TERMINAL:
            return False
        if previous is None and event.kind not in {'task_queued', 'task_started'}:
            return False  # Evicted terminal tasks cannot be resurrected by a late delta.
        view = self.views.setdefault(event.task_id, {"turn_id": event.task_id, "session_id": event.session_id,
            "status": "queued", "created_at": event.occurred_at, "attempts": {}, "tools": {},
            "phases": {}, "timeline": [], "result": None})
        kind, p = KINDS[event.kind], {}
        for key in ("text", "profile_id", "call_id", "tool_name", "status", "effect", "phase",
                    "interaction_id", "kind", "error_code", "request_key"):
            value = event.payload.get(key)
            if isinstance(value, str):
                p[key] = value[-self.max_text:] if key == 'text' else value[:256]
                if key == 'text' and len(value) > self.max_text:
                    p['truncated'] = True
        p.update(iteration=event.iteration, attempt_id=event.attempt_id)
        if event.iteration:
            view['iteration'] = event.iteration
        if "profile_id" in p:
            p["route"] = p.pop("profile_id")
        if "call_id" in p:
            p["tool_call_id"] = p.pop("call_id")
        if kind == 'tool.finished':
            kind = 'tool.' + ToolState(p.get('status', 'unknown'))
        if kind == 'turn.submitted':
            view['input'] = p.get('text', '')
            view['client_request_id'] = p.get('request_key')
        elif kind == 'turn.finished':
            view['status'] = p.get('status', 'interrupted')
            view['finished_at'] = event.occurred_at
        elif kind == 'turn.cancel_requested':
            view['status'] = 'cancelling'
        elif kind == 'interaction.pending':
            if view['status'] != 'cancelling':
                view['status'] = 'waiting'
        elif kind in {'interaction.resolved', 'interaction.cleared'}:
            if view['status'] == 'waiting':
                view['status'] = 'running'
        elif kind != 'turn.submitted' and view['status'] == 'queued':
            view['status'] = 'running'
            view['started_at'] = event.occurred_at
        if kind.startswith('model.'):
            key = str(event.attempt_id)
            attempt = view['attempts'].setdefault(key, {"attempt_id": event.attempt_id, "iteration": event.iteration,
                "route": p.get('route'), "text": '', "status": 'started'})
            attempt['status'] = kind.split('.')[1]
            if kind == 'model.delta':
                combined = attempt['text'] + p.get('text', '')
                attempt['truncated'] = attempt.get('truncated', False) or len(combined) > self.max_text
                attempt['text'] = combined[-self.max_text:]
            elif kind == 'model.accepted':
                attempt['text'] = p.get('text', '')
                attempt['truncated'] = attempt.get('truncated', False) or p.get('truncated', False)
            while len(view['attempts']) > 128:
                del view['attempts'][next(iter(view['attempts']))]
        if kind.startswith('tool.') and p.get('tool_call_id'):
            key = p['tool_call_id']
            view['tools'][key] = {**view['tools'].get(key, {}), **p, "status": kind.split('.')[1]}
            while len(view['tools']) > 128:
                del view['tools'][next(iter(view['tools']))]
        if kind.startswith('turn.phase_'):
            key = f"{event.iteration}:{p.get('phase')}"
            view['phases'][key] = {**p, "status": kind.removeprefix('turn.phase_')}
            view['phase'] = p.get('phase')
            while len(view['phases']) > 64:
                del view['phases'][next(iter(view['phases']))]
        self.seq += 1
        envelope = {"epoch": self.epoch, "seq": self.seq, "kind": kind, "session_id": event.session_id,
                    "turn_id": event.task_id, "run_id": event.run_id, "attempt_id": event.attempt_id,
                    "occurred_at": event.occurred_at, "payload": p}
        if kind != 'model.delta':
            view['timeline'].append({**envelope, "payload": {k: v for k, v in p.items() if k != 'text'}})
            del view['timeline'][:-300]
        size = len(json.dumps(envelope, ensure_ascii=False).encode('utf8'))
        self.events.append((envelope, size))
        self.bytes += size
        while self.events and (len(self.events) > self.capacity or self.bytes > self.max_bytes):
            self.bytes -= self.events.popleft()[1]
        self._prune()
        return True

    def _prune(self):
        done = sorted((v for v in self.views.values() if v['status'] in TERMINAL),
                      key=lambda v: v.get('finished_at') or 0, reverse=True)
        for view in done[self.retained:]:
            self.views.pop(view['turn_id'], None)

    def Reconcile(self, tasks):
        """Committed state overrides display, including missing terminal delivery."""
        for task in tasks:
            durable = TaskView(task)
            view = self.views.get(task.task_id)
            if view is None:
                self.views[task.task_id] = {**durable, "attempts": {}, "tools": {}, "phases": {}, "timeline": []}
            elif task.status in TERMINAL:
                view.update(durable)
            elif view['status'] not in TERMINAL:
                for key in ('input', 'created_at'):
                    view[key] = durable[key]
                if view['status'] not in {'waiting', 'cancelling'}:
                    view['status'] = task.status
        self._prune()

    def Snapshot(self, sid, kind='snapshot'):
        return {"epoch": self.epoch, "seq": self.seq, "kind": kind, "session_id": sid,
                "payload": {"turns": deepcopy([v for v in self.views.values() if v['session_id'] == sid])}}

    def Valid(self, seq):
        floor = self.events[0][0]['seq'] - 1 if self.events else self.seq
        return floor <= seq <= self.seq

    def Read(self, sid, after):
        return [deepcopy(e) for e, _ in self.events if e['session_id'] == sid and e['seq'] > after]
