"""Small business coordinator; Gateway remains the owner of execution semantics."""
from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import CancelledError
from dataclasses import asdict

from agentcore.workers import TurnQueueFullError
from file_memory.store import MemoryError, MemoryOperation, MemoryVersionConflict
from gateway.gateway import GatewayRuntime
from tools.runtime import get_tool_registry, get_tool_services

from .events import WebEventHub, WebInteractionPort, WebLifecycleSink
from .read_model import ReadModel, WebError


def required_text(body, key, *, max_length=65536):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise WebError(422, "invalid_input", f"{key} must be nonblank text (max {max_length})")
    return value


class WebHost:
    def __init__(self, config, *, memory=None, memory_settings=None, **agent_options):
        self.config, self.memory = config, memory
        self.events = WebEventHub()
        self._lock = threading.RLock()
        self._submit_lock = threading.Lock()
        self._submissions = {}
        self._requests = {}
        self._recent = deque()
        self.accepting = True
        self.gateway = GatewayRuntime(
            config, interaction_mode="push", interaction_port=WebInteractionPort(self.events),
            recovery_policy="attached", event_sinks=(WebLifecycleSink(self.events),),
            on_output=self.events.output, on_submission=self.register_turn, **agent_options,
        )
        self.read = ReadModel(config, get_tool_services(), get_tool_registry(), memory_settings)

    def start(self):
        self.gateway.start_blocking()

    def register_turn(self, submission):
        sid, tid = submission.session_id, submission.turn_id
        if not sid or not tid:
            raise RuntimeError("Gateway submission must carry IDs")
        with self._lock:
            if tid in self._submissions:
                return
            self._submissions[tid] = submission
        self.events.register(sid, tid)
        submission.future.add_done_callback(lambda future: self._completed(submission, future))

    def _completed(self, submission, future):
        try:
            result = future.result().model_dump(mode="json")
        except BaseException as exc:
            cancelled = isinstance(exc, CancelledError) or submission.cancel_event.is_set()
            result = {"session_id": submission.session_id, "turn_id": submission.turn_id,
                      "status": "interrupted" if cancelled else "failed", "final_text": None,
                      "error": {"code": "cancelled" if cancelled else "execution_failed",
                                "message": "turn cancelled" if cancelled else "turn execution failed"}}
        self.events.publish("turn.finished", submission.session_id, submission.turn_id, result)
        with self._lock:
            self._recent.append(submission.turn_id)
            while len(self._recent) > 200:
                retired = self._recent.popleft()
                self._submissions.pop(retired, None)
                for request_id, (_, dto) in tuple(self._requests.items()):
                    if dto["turn_id"] == retired:
                        self._requests.pop(request_id, None)

    def submit(self, body):
        sid = required_text(body, "session_id", max_length=256)
        text = required_text(body, "text")
        request_id = required_text(body, "client_request_id", max_length=256)
        fingerprint = (sid, text)
        # Serialize only HTTP submissions, never hold the registry lock when entering Gateway.
        with self._submit_lock:
            if not self.accepting:
                raise WebError(503, "stopping", "service is stopping")
            with self._lock:
                existing = self._requests.get(request_id)
            if existing:
                if existing[0] != fingerprint:
                    raise WebError(409, "request_conflict", "request ID already used for different content")
                return existing[1]
            try:
                submission = self.gateway.submit_turn(text, sid)
            except TurnQueueFullError:
                raise WebError(503, "queue_full", "turn queue is full") from None
            dto = {"session_id": sid, "turn_id": submission.turn_id, "client_request_id": request_id}
            self.events.publish("turn.submitted", sid, submission.turn_id, {**dto, "text": text})
            with self._lock:
                self._requests[request_id] = (fingerprint, dto)
            return dto

    def turn(self, tid):
        value = self.events.turn(tid)
        if value is None:
            raise WebError(404, "not_found", "turn not retained in this process")
        return value

    def cancel(self, tid):
        with self._lock:
            submission = self._submissions.get(tid)
        if submission is None:
            raise WebError(404, "not_found", "turn not found")
        if not submission.future.done():
            submission.cancel()
            self.events.publish("turn.cancel_requested", submission.session_id, tid, {})
        return self.turn(tid)

    def interactions(self, sid=None):
        requests = self.gateway.pending_interactions(sid)
        self.events.reconcile_interactions(sid, requests)
        return {"interactions": [r.to_dict() for r in requests]}

    def resolve(self, request_id, body):
        sid = required_text(body, "session_id", max_length=256)
        pending = self.gateway.pending_interactions(sid)
        request = next((r for r in pending if r.request_id == request_id), None)
        if request is None or not self.gateway.resolve_interaction(sid, request_id, body.get("response")):
            raise WebError(409, "interaction_conflict", "interaction expired or response invalid")
        self.events.publish("interaction.resolved", sid, request.turn_id, {"request_id": request_id})
        return self.interactions(sid)

    def memory_document(self, target):
        if target not in {"memory", "user"}:
            raise WebError(404, "not_found", "unknown memory document")
        if self.memory is None:
            raise WebError(409, "memory_disabled", "enable [file_memory] and restart")
        try:
            return {**asdict(self.memory.read(target)), "limit": self.memory.limits[target]}
        except MemoryError as exc:
            raise WebError(422, "memory_invalid", str(exc)) from None

    def edit_memory(self, target, body):
        self.memory_document(target)
        version = required_text(body, "expected_version", max_length=64)
        action = body.get("action")
        if action not in {"add", "replace", "remove"}:
            raise WebError(422, "invalid_input", "invalid memory action")
        for key in ("content", "old_text"):
            if not isinstance(body.get(key, ""), str):
                raise WebError(422, "invalid_input", f"{key} must be text")
        try:
            self.memory.apply(MemoryOperation(action, target, body.get("content", ""), body.get("old_text", "")),
                              expected_version=version, exact_match=True)
        except MemoryVersionConflict as exc:
            raise WebError(409, "memory_conflict", str(exc)) from None
        except MemoryError as exc:
            raise WebError(422, "memory_invalid", str(exc)) from None
        return self.memory_document(target)

    def status(self):
        return {"version": "0.1.0", "epoch": self.events.epoch, "accepting": self.accepting,
                "capabilities": {"memory": self.memory is not None, "streaming": True,
                                 "data_readonly": True, "config_readonly": True}}

    def close(self, timeout=5):
        self.accepting = False
        return self.gateway.close_blocking(timeout=timeout)
