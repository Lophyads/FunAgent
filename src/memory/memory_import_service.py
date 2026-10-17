"""One application-wide worker; snapshots never hold locks during storage IO."""
from copy import deepcopy
import hashlib
from threading import Event, RLock, Thread
from uuid import uuid4

from ..frame.contracts import DomainError
from .memory_import import Parse, Run, CheckSource
from .protocols import MemoryOperationsAccess


class MemoryImportService:
    def __init__(self, operations: MemoryOperationsAccess):
        self.operations = operations
        self._lock, self._stop = RLock(), Event()
        self._latest, self._thread, self._active = {}, None, None

    def IsBusy(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def RebindMemory(self, operations):
        with self._lock:
            self.operations = operations
            self._latest.clear()

    def Submit(self, source_id, raw):
        document = Parse(raw)
        CheckSource(self.operations, source_id)
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            if self._stop.is_set():
                raise DomainError('STOPPING', 'Application is stopping')
            if self._active:
                active = self._latest[self._active]
                if active['source_id'] == source_id and active['input_sha256'] == digest:
                    return deepcopy(active)
                raise DomainError('CAPACITY', 'An import is running')
            identifier = uuid4().hex
            snapshot = dict(import_id=identifier, source_id=source_id, input_sha256=digest,
                            dataset_id=document.dataset_id, status='running', topics=[], memories=[], error=None,
                            progress=dict(topics_done=0, topics_total=len(document.topics), memories_done=0,
                                          memories_total=len(document.memories)))
            self._latest[source_id] = snapshot
            self._active = source_id
            self._thread = Thread(target=self._run, args=(document, source_id),
                                  name='memory-import', daemon=True)
            self._thread.start()
            return deepcopy(snapshot)

    def _run(self, document, source_id):
        def update(value):
            with self._lock:
                snapshot = self._latest[source_id]
                snapshot['topics'].extend(value['topics'])
                snapshot['memories'].extend(value['memories'])
                snapshot.update({key: item for key, item in value.items() if key not in {'topics', 'memories'}})
        try:
            Run(document, source_id, self.operations, on_progress=update, should_stop=self._stop.is_set)
        finally:
            with self._lock:
                self._active = None

    def Latest(self, source_id):
        CheckSource(self.operations, source_id, write=False)
        self.operations.Topics(source_id)
        with self._lock:
            return deepcopy(self._latest.get(source_id))

    def Get(self, import_id):
        with self._lock:
            snapshot = next((deepcopy(s) for s in self._latest.values() if s['import_id'] == import_id), None)
        if snapshot is None:
            raise DomainError('NOT_FOUND', 'Import not found')
        CheckSource(self.operations, snapshot['source_id'], write=False)
        self.operations.Topics(snapshot['source_id'])
        return snapshot

    def RequestStop(self):
        self._stop.set()

    def Stop(self):
        self.RequestStop()
        with self._lock:
            thread = self._thread
        if thread:
            thread.join()

    def IsStopped(self):
        return self._stop.is_set() and (self._thread is None or not self._thread.is_alive())

    Close = Stop
