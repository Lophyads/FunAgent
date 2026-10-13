"""Application binding, fixed tree ownership and execution-local scope restoration."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock, RLock
import time
from uuid import uuid4

from ..frame.contracts import DomainError, ControlError, freeze
from .contracts import NodeRef, ObservationRecord, TraceId
from .protocols import NullObservation, NullRecorder
from .scope_state import CURRENT, Cursor, Frame, Owner


_LABELS = {'session_id', 'run_id', 'purpose', 'iteration', 'attempt_id', 'call_id', 'interaction_id'}


def _labels(fields):
    return {key: fields[key] for key in _LABELS if key in fields}


@dataclass
class _Node:
    ref: NodeRef
    parent: str | None
    started: float
    owner: tuple
    ended: float | None = None
    borrowers: int = 0
    depth: int = 0
    started_tick: float = 0
    labels: object = field(default_factory=dict)


@dataclass
class _Task:
    root: NodeRef
    nodes: dict = field(default_factory=dict)
    users: int = 0
    ended: bool = False


class ObservationMgr:
    _instance = None
    _binding_lock = Lock()
    _null = NullObservation()

    def __init__(self, recorder=None, *, clock=time.time, timer=None, max_nodes=4096, max_tasks=1024):
        self._recorder = recorder or NullRecorder()
        self._clock, self._max_nodes, self._max_tasks = clock, max_nodes, max_tasks
        self._timer = timer or (time.monotonic if clock is time.time else clock)
        self._lock = RLock()
        self._tasks, self._bindings = {}, {}
        self._closed = False

    @classmethod
    def Initialize(cls, instance):
        with cls._binding_lock:
            if cls._instance is not None:
                raise DomainError("ALREADY_INITIALIZED", "Observation already initialized")
            cls._instance = instance

    @classmethod
    def GetInstance(cls):
        return cls._instance if cls._instance is not None else cls._null

    @classmethod
    def Close(cls):
        with cls._binding_lock:
            if cls._instance is not None:
                cls._instance.CloseResource()
                cls._instance = None

    def Diagnose(self, code, task_id=None):
        try:
            self._recorder.Diagnose(code, task_id)
        except Exception:
            pass

    def _prepare(self, fields):
        try:
            return self._recorder.Prepare(fields)
        except Exception:
            self.Diagnose("snapshot_failed", self._cursor().ref.task_id if self._cursor().ref else None)
            return {"payload_missing": True}

    def _publish(self, ref, operation, fields, at=None):
        try:
            self._recorder.Publish(ObservationRecord(ref, operation, self._clock() if at is None else at, fields))
        except Exception:
            self.Diagnose("record_failed", ref.task_id)

    def _cursor(self):
        cursor = CURRENT.get()
        return cursor if cursor.manager is self and not self._closed else Cursor(manager=self)

    def _node(self, ref):
        task = self._tasks.get(ref.task_id) if ref else None
        return task.nodes.get(ref.span_id) if task else None

    def PrepareInput(self, projection):
        if self._closed or self._cursor().suppressed:
            return None
        return self._prepare({"input": projection}).get("input")

    def StartTask(self, task_id, *, session_id=None, input=None, started_at=None, **fields):
        if self._closed or self._cursor().suppressed:
            return None
        prepared = self._prepare(dict(fields, input=input, session_id=session_id))
        with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id].root
            if len(self._tasks) >= self._max_tasks:
                self.Diagnose("task_capacity", task_id)
                return None
            ref = NodeRef(task_id, TraceId(task_id), uuid4().hex[:16])
            at = self._clock() if started_at is None else started_at
            node = _Node(ref, None, at, Owner(), started_tick=self._timer() - max(0, self._clock() - at),
                         labels=freeze(_labels(prepared)))
            self._tasks[task_id] = _Task(ref, {ref.span_id: node})
            self._publish(ref, "start_task", dict(prepared, name="task.turn", kind="agent", parent_span_id=None), at)
            return ref

    def _start(self, name, fields, binding=None, *, entered=False):
        cursor = self._cursor()
        if cursor.suppressed or cursor.ref is None:
            return None
        with self._lock:
            parent = self._node(cursor.ref)
            if parent is None or parent.ended is not None:
                return None
            task = self._tasks[cursor.ref.task_id]
            if len(task.nodes) >= self._max_nodes:
                self.Diagnose('node_capacity', cursor.ref.task_id)
                return None
            if parent.depth >= 128:
                self.Diagnose('node_depth_limit', cursor.ref.task_id)
                return None
        prepared = self._prepare(fields)
        with self._lock:
            parent = self._node(cursor.ref)
            if parent is None or parent.ended is not None:
                return None
            task = self._tasks[cursor.ref.task_id]
            if task.ended:
                return None
            if len(task.nodes) >= self._max_nodes:
                self.Diagnose("node_capacity", cursor.ref.task_id)
                return None
            ref = NodeRef(cursor.ref.task_id, cursor.ref.trace_id, uuid4().hex[:16])
            prepared = dict(parent.labels, **prepared)
            at = prepared.get('started_at', self._clock())
            task.nodes[ref.span_id] = _Node(ref, cursor.ref.span_id, at, Owner(), depth=parent.depth + 1,
                                          started_tick=self._timer() - max(0, self._clock() - at),
                                          labels=freeze(_labels(prepared)))
            if entered:
                task.users += 1
            self._publish(ref, "start", dict(prepared, name=name, parent_span_id=cursor.ref.span_id), at)
            if binding is not None:
                self._bindings[binding] = ref
            return ref

    def Start(self, name, *, binding=None, **fields):
        return self._start(name, fields, binding)

    def Enter(self, name, **fields):
        previous = self._cursor()
        ref = self._start(name, fields, entered=True)
        CURRENT.set(Cursor(self, ref, previous.stack + (Frame(previous, ref, Owner()),),
                           suppressed=ref is None))
        return ref

    def Exit(self, **fields):
        cursor = self._cursor()
        if not cursor.stack or cursor.stack[-1].owner != Owner():
            self.Diagnose("scope_mismatch", cursor.ref.task_id if cursor.ref else None)
            return
        frame = cursor.stack[-1]
        try:
            self.Complete(**fields)
        finally:
            CURRENT.set(frame.previous)
            if frame.ref is not None:
                with self._lock:
                    task = self._tasks.get(frame.ref.task_id)
                    if task:
                        task.users -= 1
                        self._seal(task)

    @contextmanager
    def Scope(self, name, **fields):
        previous = CURRENT.get()
        depth = len(self._cursor().stack)
        ref = self.Enter(name, **fields)
        try:
            yield
        except BaseException as error:
            status = ('cancelled' if error.code == 'CANCELLED' else 'timeout') if isinstance(error, ControlError) else 'failed'
            self.Complete(status=status, error_code=getattr(error, "code", type(error).__name__))
            raise
        finally:
            cursor = self._cursor()
            if cursor.ref != ref:
                self.Diagnose("scope_mismatch", ref.task_id if ref else None)
                while len(cursor.stack) > depth + 1 and cursor.ref != ref and cursor.stack[-1].owner == Owner():
                    self.Exit(status="abandoned")
                    cursor = self._cursor()
            if cursor.ref == ref and len(cursor.stack) > depth:
                self.Exit()
            CURRENT.set(previous)

    def Update(self, **fields):
        cursor = self._cursor()
        if cursor.suppressed or cursor.ref is None:
            return
        with self._lock:
            node = self._node(cursor.ref)
            active = node is not None and node.ended is None
        if active:
            prepared = self._prepare(fields)
            with self._lock:
                node = self._node(cursor.ref)
                if node is not None and node.ended is None:
                    node.labels = freeze(dict(node.labels, **_labels(prepared)))
                    self._publish(cursor.ref, 'update', prepared)

    def Complete(self, *, status="completed", **fields):
        cursor = self._cursor()
        if cursor.suppressed or cursor.ref is None:
            return
        with self._lock:
            node = self._node(cursor.ref)
            if node is None or node.ended is not None:
                return
        prepared = self._prepare(fields)
        with self._lock:
            node = self._node(cursor.ref)
            if node is None or node.ended is not None:
                return
            # Long-lived bindings (e.g. queue_wait) transfer ending rights;
            # borrowed interaction scopes can only record facts, not complete.
            if cursor.borrowed or (node.parent is not None and node.owner != Owner()):
                self.Diagnose("scope_mismatch", cursor.ref.task_id)
                return
            node.ended = self._clock()
            if any(n.parent == node.ref.span_id and n.ended is None for n in self._tasks[node.ref.task_id].nodes.values()):
                self.Diagnose("scope_mismatch", cursor.ref.task_id)
            self._publish(cursor.ref, "complete", dict(prepared, status=status, ended_at=node.ended,
                                                       duration_ms=max(0, (self._timer() - node.started_tick) * 1000)), node.ended)
            self._release_bindings(node)

    def Event(self, name, *, occurred_at=None, **fields):
        cursor = self._cursor()
        if cursor.suppressed or cursor.ref is None:
            return
        at = self._clock() if occurred_at is None else occurred_at
        with self._lock:
            node = self._node(cursor.ref)
            if node is None or (node.ended is not None and not (cursor.borrowed and at <= node.ended)):
                return
        prepared = dict(self._prepare(fields), name=name)
        with self._lock:
            node = self._node(cursor.ref)
            if node is not None and (node.ended is None or (cursor.borrowed and at <= node.ended)):
                self._publish(cursor.ref, 'event', prepared, at)

    def Bind(self, key):
        cursor = self._cursor()
        if not cursor.suppressed and cursor.ref is not None:
            with self._lock:
                self._bindings[key] = cursor.ref

    @contextmanager
    def _use(self, ref, *, borrowed=False, transfer=False):
        task = None
        with self._lock:
            if self._cursor().suppressed or self._closed:
                ref = None
            if ref is not None:
                task = self._tasks.get(ref.task_id)
                node = task.nodes.get(ref.span_id) if task else None
                if borrowed and (node is None or node.ended is not None):
                    self.Diagnose('scope_missing', ref.task_id)
                    task = None
            if task:
                task.users += 1
                if borrowed:
                    node.borrowers += 1
                if transfer:
                    task.nodes[ref.span_id].owner = Owner()
            token = CURRENT.set(Cursor(self, ref if task else None, suppressed=not bool(task), borrowed=borrowed))
        try:
            yield
        finally:
            CURRENT.reset(token)
            with self._lock:
                if task:
                    task.users -= 1
                    if borrowed:
                        node.borrowers -= 1
                        self._release_bindings(node)
                    self._seal(task)

    def _release_bindings(self, node):
        if node.ended is not None and node.borrowers == 0:
            self._bindings = {key: ref for key, ref in self._bindings.items() if ref != node.ref}

    def UseTask(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            ref = task.root if task else None
        if ref is None:
            self.Diagnose("scope_missing", task_id)
        return self._use(ref)

    def UseBinding(self, key):
        with self._lock:
            ref = self._bindings.get(key)
        if ref is None:
            self.Diagnose("scope_missing")
        transfer = isinstance(key, tuple) and bool(key) and key[0] == 'queue_wait'
        return self._use(ref, borrowed=not transfer, transfer=transfer)

    @contextmanager
    def Suspend(self):
        token = CURRENT.set(Cursor(self, suppressed=True))
        try:
            yield
        finally:
            CURRENT.reset(token)

    def EndTask(self, *, status, **fields):
        cursor = self._cursor()
        if cursor.suppressed or cursor.ref is None:
            return
        if cursor.borrowed:
            self.Diagnose('scope_mismatch', cursor.ref.task_id)
            return
        with self._lock:
            task = self._tasks.get(cursor.ref.task_id)
            if task is None or task.ended:
                return
            previous = CURRENT.set(Cursor(self, task.root))
            try:
                self.Complete(status=status, **fields)
                task.ended = True
                self._publish(task.root, "end_task", {"task_status": status})
                self._seal(task)
            finally:
                CURRENT.reset(previous)

    def _seal(self, task):
        if task.ended and task.users == 0:
            for node in task.nodes.values():
                if node.ended is None:
                    self._publish(node.ref, "complete", {"status": "abandoned", "ended_at": self._clock()})
                    self.Diagnose("scope_mismatch", task.root.task_id)
            self._publish(task.root, "seal", {})
            self._tasks.pop(task.root.task_id, None)
            self._bindings = {k: ref for k, ref in self._bindings.items() if ref.task_id != task.root.task_id}

    def Recover(self, task_lookup=None):
        self._recorder.Recover(task_lookup)

    def Health(self):
        return self._recorder.Health()

    def Reader(self, task_lookup=None):
        from .trace_reader import TraceReader
        return TraceReader(self._recorder.store.directory, task_lookup)

    def State(self):
        with self._lock:
            return {"active_tasks": len(self._tasks), "bindings": len(self._bindings)}

    def CloseResource(self):
        # Failure keeps the application binding and recorder available for retry.
        self._recorder.Close()
        with self._lock:
            self._closed = True
            self._tasks.clear()
            self._bindings.clear()
            if CURRENT.get().manager is self:
                CURRENT.set(Cursor())
