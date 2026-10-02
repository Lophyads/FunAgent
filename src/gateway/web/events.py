"""Bounded runtime projection and replay, with network I/O outside the lock."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping

from agentcore.interfaces import ModelOutputEvent

TERMINAL = frozenset({"completed", "failed", "interrupted"})


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class WebEventHub:
    def __init__(self, *, capacity=4096, max_bytes=8 * 1024 * 1024, retained=200):
        self.epoch = uuid.uuid4().hex
        self._condition = threading.Condition(threading.RLock())
        self._events = deque()
        self._bytes = 0
        self._seq = 0
        self._capacity, self._max_bytes, self._retained = capacity, max_bytes, retained
        self._turns: dict[str, dict] = {}
        self._terminal = deque()
        self._early = deque(maxlen=256)
        self._closed = False

    def _ensure(self, sid, tid):
        return self._turns.setdefault(tid, {
            "session_id": sid, "turn_id": tid, "status": "queued", "created_at": time.time(),
            "attempts": {}, "tools": {}, "phases": {}, "timeline": [], "result": None,
        })

    def register(self, sid, tid):
        with self._condition:
            self._ensure(sid, tid)
            self.publish("turn.queued", sid, tid, {})
            early = [event for event in self._early if event.trace_id == tid]
            self._early = deque((event for event in self._early if event.trace_id != tid), maxlen=256)
            for event in early:
                self._diagnostic(event)

    def diagnostic(self, event):
        with self._condition:
            if event.trace_id not in self._turns:
                self._early.append(event)
                return
            self._diagnostic(event)

    def _diagnostic(self, event):
        turn = self._turns[event.trace_id]
        self.publish(event.name, turn["session_id"], event.trace_id,
                     {**plain(event.payload), "phase": event.phase}, occurred_at=event.occurred_at)

    def output(self, event: ModelOutputEvent):
        with self._condition:
            # Output may arrive before Gateway._track; raw IDs allow immediate binding.
            self._ensure(event.session_id, event.turn_id)
            self.publish(event.kind, event.session_id, event.turn_id, {
                "iteration": event.iteration, "attempt_id": event.attempt_id,
                "route": event.route, "text": event.text,
            })

    def publish(self, kind, sid, tid, payload, *, occurred_at=None):
        with self._condition:
            if self._closed:
                return
            turn = self._ensure(sid, tid) if tid else None
            if turn and turn["status"] in TERMINAL and kind != "turn.finished":
                return  # Late diagnostics and output cannot regress a terminal turn.
            payload = plain(payload)
            now = occurred_at or time.time()
            if turn:
                if kind == "turn.submitted":
                    turn["input"] = payload.get("text", "")
                    turn["client_request_id"] = payload.get("client_request_id")
                if kind == "turn.finished":
                    if turn["status"] in TERMINAL:
                        return
                    turn["status"] = payload["status"]
                    turn["result"] = payload
                    turn["finished_at"] = now
                    self._terminal.append(tid)
                    while len(self._terminal) > self._retained:
                        self._turns.pop(self._terminal.popleft(), None)
                elif kind == "turn.cancel_requested":
                    turn["status"] = "cancelling"
                elif kind == "interaction.pending":
                    if turn["status"] != "cancelling":
                        turn["status"] = "waiting"
                elif kind in {"interaction.resolved", "interaction.cleared"}:
                    if turn["status"] == "waiting":
                        turn["status"] = "running"
                elif kind not in {"turn.queued", "turn.submitted"} and turn["status"] == "queued":
                    turn["status"] = "running"
                    turn["started_at"] = now
                if kind.startswith("model."):
                    attempt = turn["attempts"].setdefault(payload["attempt_id"], {
                        "attempt_id": payload["attempt_id"], "iteration": payload["iteration"],
                        "route": payload["route"], "text": "", "status": "started",
                    })
                    attempt["status"] = kind.split(".")[1]
                    if kind == "model.delta":
                        attempt["text"] += payload["text"]
                    elif kind == "model.accepted":
                        attempt["text"] = payload["text"]
                if kind.startswith("tool.") and payload.get("tool_call_id"):
                    key = payload["tool_call_id"]
                    turn["tools"][key] = {**turn["tools"].get(key, {}), **payload,
                                           "status": kind.split(".")[1]}
                if kind.startswith("turn.phase_"):
                    key = f'{payload.get("iteration", 0)}:{payload.get("phase")}'
                    turn["phases"][key] = {**payload, "status": kind.removeprefix("turn.phase_")}
                    turn["phase"] = payload.get("phase")
                    turn["iteration"] = payload.get("iteration", 0)
                if kind != "model.delta":
                    turn["timeline"].append({"kind": kind, "seq": self._seq + 1, "occurred_at": now,
                                               "payload": {k: v for k, v in payload.items() if k != "text"}})
                    del turn["timeline"][:-300]
            self._seq += 1
            envelope = {"epoch": self.epoch, "seq": self._seq, "kind": kind,
                        "session_id": sid, "turn_id": tid, "occurred_at": now, "payload": payload}
            size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
            self._events.append((envelope, size))
            self._bytes += size
            while self._events and (len(self._events) > self._capacity or self._bytes > self._max_bytes):
                _, old_size = self._events.popleft()
                self._bytes -= old_size
            self._condition.notify_all()

    def turn(self, tid):
        with self._condition:
            return copy.deepcopy(self._turns.get(tid))

    def reconcile_interactions(self, sid, requests):
        active = {request.turn_id for request in requests}
        with self._condition:
            for tid, turn in tuple(self._turns.items()):
                if (sid is None or turn["session_id"] == sid) and turn["status"] == "waiting" and tid not in active:
                    self.publish("interaction.cleared", turn["session_id"], tid, {})

    def _snapshot(self, sid):
        return {"epoch": self.epoch, "seq": self._seq, "kind": "snapshot", "session_id": sid,
                "payload": {"turns": copy.deepcopy([t for t in self._turns.values()
                                                      if t["session_id"] == sid])}}

    def connect(self, sid, last_id=None):
        """Capture snapshot/watermark under one lock, or replay a valid cursor."""
        with self._condition:
            cursor = None
            if last_id:
                try:
                    epoch, seq = last_id.split(":")
                    seq = int(seq)
                    if epoch == self.epoch and self._valid(seq):
                        cursor = seq
                except (ValueError, TypeError):
                    pass
            if cursor is not None:
                return self._read(sid, cursor), self._seq
            snapshot = self._snapshot(sid)
            if last_id:
                snapshot["kind"] = "reset"
            return [snapshot], self._seq

    def _valid(self, cursor):
        floor = self._events[0][0]["seq"] - 1 if self._events else self._seq
        return floor <= cursor <= self._seq

    def _read(self, sid, cursor):
        return [copy.deepcopy(e) for e, _ in self._events
                if e["seq"] > cursor and e["session_id"] == sid]

    def wait(self, sid, cursor, timeout=15):
        with self._condition:
            self._condition.wait_for(lambda: self._closed or self._seq > cursor, timeout)
            if not self._valid(cursor):
                snapshot = self._snapshot(sid)
                snapshot["kind"] = "reset"
                return [snapshot], self._seq
            return self._read(sid, cursor), self._seq

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class WebLifecycleSink:
    def __init__(self, events):
        self.events = events

    def emit(self, envelope):
        self.events.diagnostic(envelope)

    def close(self):
        pass  # WebHost owns the hub and keeps completion callbacks available during shutdown.


class WebInteractionPort:
    def __init__(self, events):
        self.events = events

    def present(self, request):
        self.events.publish("interaction.pending", request.session_id, request.turn_id, request.to_dict())
