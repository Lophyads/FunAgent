"""Bounded, process-local task display; persistence remains the terminal truth."""
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from threading import Condition
from typing import Protocol
import json

from .contracts import DomainError, thaw
from ..model.contracts import RunEvent
from .protocols import TaskEventReader, RunEventWriter
from .web_projection import WebProjection


class EventAccess(TaskEventReader, RunEventWriter, Protocol):
    """Public display ports; lifecycle ownership stays with EventMgr."""


@dataclass
class _Display:
    events: deque = field(default_factory=deque)
    bytes: int = 0
    current: RunEvent | None = None
    text: str = ''
    truncated: bool = False


class TaskEvents:
    def __init__(self, *, max_events=128, max_tasks=128, max_bytes=262144, max_text=16384, heartbeat=0.5):
        if any(type(v) is not int or v < 1 for v in (max_events, max_tasks, max_bytes, max_text)) or heartbeat <= 0:
            raise DomainError('INVALID_REQUEST', 'Invalid event buffer bounds')
        self._limit, self._tasks, self._bytes, self._text = max_events, max_tasks, max_bytes, max_text
        self._heartbeat = heartbeat
        self._condition = Condition()
        self._entries, self._seq, self._stopped, self._subscribers = OrderedDict(), 0, False, 0
        self._web = WebProjection(max_text=max_text)

    def Publish(self, event):
        if event.purpose != 'main' or not event.task_id:
            return
        with self._condition:
            if self._stopped:
                return
            if self._web.Publish(event):
                self._condition.notify_all()
        keys = {'attempt_started': ('profile_id',), 'text_delta': ('text',), 'attempt_reset': (),
                'tool_started': ('call_id', 'tool_name'), 'tool_finished': ('call_id', 'status', 'effect'),
                'interaction_pending': ('interaction_id',), 'task_committed': ()}
        if event.purpose != 'main' or not event.task_id or event.kind not in keys:
            return
        original_text = event.payload.get('text', '')
        payload = {key: value[-self._text:] if key == 'text' else value[:256]
                   for key in keys[event.kind] if isinstance(value := event.payload.get(key), str)}
        with self._condition:
            if self._stopped:
                return
            self._seq += 1
            event = replace(event, payload=payload, seq=self._seq)
            state = self._entries.setdefault(event.task_id, _Display())
            self._entries.move_to_end(event.task_id)
            if event.kind == 'attempt_started':
                state.text, state.truncated = '', False
            elif event.kind == 'text_delta':
                combined = state.text + payload.get('text', '')
                state.truncated |= len(combined) > self._text or len(original_text) > self._text
                state.text = combined[-self._text:]
            elif event.kind == 'attempt_reset':
                state.text, state.truncated = '', False
            state.current = event
            size = len(json.dumps(thaw(payload), ensure_ascii=False).encode('utf8')) + 512
            state.events.append((event, size))
            state.bytes += size
            while state.events and (len(state.events) > self._limit or state.bytes > self._bytes):
                _, discarded = state.events.popleft()
                state.bytes -= discarded
            while len(self._entries) > self._tasks:
                self._entries.popitem(last=False)
            self._condition.notify_all()

    def _reset(self, task_id, state):
        current = state.current if state else None
        return RunEvent(task_id, current.run_id if current else '', iteration=current.iteration if current else 0,
                        attempt_id=current.attempt_id if current else 0, kind='stream_reset',
                        payload={'display': {'text': state.text if state else '',
                                            'truncated': state.truncated if state else False}},
                        seq=current.seq if current else self._seq)

    def Subscribe(self, task_id, after_seq=None):
        """No per-subscriber queue: a slow reader falls behind the bounded tail."""
        cursor, initial = after_seq, True
        with self._condition:
            self._subscribers += 1
            self._condition.notify_all()
        try:
            while True:
                with self._condition:
                    if self._stopped:
                        return
                    state = self._entries.get(task_id)
                    latest = state.current.seq if state else self._seq
                    oldest = state.events[0][0].seq if state and state.events else latest + 1
                    if cursor is None or cursor > latest or (cursor < oldest - 1 and cursor != latest):
                        batch = [self._reset(task_id, state)]
                    else:
                        batch = [e for e, _ in state.events if e.seq > cursor] if state else []
                    if not batch:
                        if not initial:
                            self._condition.wait(self._heartbeat)
                            if self._stopped:
                                return
                            state = self._entries.get(task_id)
                            latest = state.current.seq if state else self._seq
                            if latest != cursor:
                                continue
                        batch = [RunEvent(task_id, kind='heartbeat')]
                    initial = False
                for event in batch:
                    if event.seq:
                        cursor = event.seq
                    elif cursor is None:
                        cursor = latest
                    yield event
        finally:
            with self._condition:
                self._subscribers -= 1
                self._condition.notify_all()

    def RequestStop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    @property
    def epoch(self):
        return self._web.epoch

    def Reconcile(self, tasks):
        with self._condition:
            self._web.Reconcile(tasks)

    def Snapshot(self, session_id):
        with self._condition:
            return self._web.Snapshot(session_id)

    def SubscribeSession(self, session_id, cursor=None):
        after, initial = None, True
        if cursor:
            try:
                sid, epoch, sequence = cursor.split(':')
                if sid != session_id:
                    raise DomainError('INVALID_REQUEST', 'Cursor belongs to another session')
                if epoch == self._web.epoch:
                    after = int(sequence)
            except (ValueError, TypeError):
                after = None
        with self._condition:
            self._subscribers += 1
        try:
            while True:
                with self._condition:
                    if self._stopped:
                        return
                    if after is None or not self._web.Valid(after):
                        batch = [self._web.Snapshot(session_id, 'reset' if cursor or not initial else 'snapshot')]
                    else:
                        batch = self._web.Read(session_id, after)
                    watermark = self._web.seq
                    if not batch:
                        if not initial:
                            self._condition.wait(self._heartbeat)
                            if self._stopped:
                                return
                            if self._web.seq != watermark:
                                continue
                        batch = [{"kind": "heartbeat", "session_id": session_id}]
                    after, initial = watermark, False
                yield from batch
        finally:
            with self._condition:
                self._subscribers -= 1
                self._condition.notify_all()

    def Close(self):
        self.RequestStop()
        with self._condition:
            self._entries.clear()
            self._web.views.clear()
            self._web.events.clear()


class EventMgr:
    _instance = None

    @classmethod
    def Initialize(cls, service):
        if cls._instance is not None:
            raise DomainError('ALREADY_INITIALIZED', 'Events already initialized')
        cls._instance = service

    @classmethod
    def GetInstance(cls) -> EventAccess:
        if cls._instance is None:
            raise DomainError('NOT_READY', 'Events not initialized')
        return cls._instance

    @classmethod
    def Close(cls):
        if cls._instance is not None:
            cls._instance.Close()
            cls._instance = None


def Emit(context, kind, payload=None):
    """Observation failure never changes task admission or a business outcome."""
    if context.purpose != 'main' or not context.task_id:
        return
    try:
        EventMgr.GetInstance().Publish(RunEvent(context.task_id, context.run_id, context.purpose,
                                              kind=kind, payload=payload or {}, session_id=context.session_id))
    except Exception:
        pass
