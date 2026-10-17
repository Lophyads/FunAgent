"""Management uses frozen permissions and one-use challenges, never tool grants."""
from dataclasses import replace
from functools import wraps
from threading import RLock
from uuid import uuid4
import hashlib
import hmac
import json
import time
import re

from ..frame.contracts import DomainError, RunContext
from .common import text, validate_content, validate_metadata
from .contracts import MemoryListQuery, TopicSelection, UNSET
from .protocols import TopicCatalog
from .topic_projection import TopicData, TopicPath, RecordTopic
from .topic_pagination import ReadDirectory
from .policy import Outcome
from .prompt import RecordData
from .failure import IssueData
from ..observability.observation_mgr import ObservationMgr


def _management(method):
    @wraps(method)
    def call(*args, **kwargs):
        with ObservationMgr.GetInstance().Suspend():
            return method(*args, **kwargs)
    return call


def ManagementRecord(record):
    return RecordData(record) | {"evidence": list(record.evidence)}


class MemoryOperations:
    def __init__(self, memory, conf, *, clock=time.monotonic, inspection=None, topics: TopicCatalog | None = None):
        self._memory = memory
        self._inspection = inspection
        self._topics = topics
        self._sources = conf.Memory().Sources()
        self._facts = tuple(conf.Memory().Policy('facts').get('sources', ()))
        self._access = conf.Tool().Access("management")
        self._timeout = conf.Serv().Options().get("interaction_timeout", 120)
        self._clock = clock
        self._lock = RLock()
        self._pending = {}
        self._closed = False

    def _context(self, identity=None):
        if self._closed:
            raise DomainError("NOT_READY", "Memory operations closed")
        identity = identity or uuid4().hex
        return RunContext(run_id=identity, purpose="management", access=self._access,
                          deadline=time.monotonic() + self._timeout,
                          memory_evidence=("management:" + identity,))

    def Sources(self):
        self._context()
        return [{"source_id": name, "kind": source["kind"], "store": source.get("store", "unknown"),
                 "source_description": source.get("source_description", ""),
                 "capabilities": list(source.get("capabilities", ("search",))),
                 "writable": self._token(name, True) in self._access,
                 **({'inspection': list(self._inspection.capabilities)} if self._inspection and self._inspection.Supports(name) else {})}
                for name, source in self._sources.items() if self._token(name) in self._access]

    def DefaultSource(self):
        self._context()
        return next((name for name in self._facts if self._sources[name]['kind'] == 'episodic'
                     and self._token(name) in self._access), None)

    @_management
    def Check(self, source_id=None):
        context = self._context()
        if source_id is not None and (not text(source_id) or len(source_id) > 128):
            raise DomainError("INVALID_REQUEST", "Invalid memory source")
        names = ((source_id,) if source_id is not None else
                 tuple(name for name in self._sources if self._token(name) in self._access))
        return self._memory.Check(names, context)

    @_management
    def Inspect(self, operation, query):
        context = self._context()
        if self._inspection is None:
            raise DomainError('UNSUPPORTED', 'Inspection unavailable')
        return self._inspection.Read(operation, query, context)

    def _invalidate(self, source):
        if self._inspection is not None:
            self._inspection.Invalidate(source)

    def _token(self, name, write=False):
        return self._sources[name].get("write_access" if write else "read_access",
                                      f"memory.{name}.{'write' if write else 'read'}")

    def _catalog(self, source_id, context, *, write=False):
        context.CheckCancelled()
        if not text(source_id) or len(source_id) > 128:
            raise DomainError("INVALID_REQUEST", "Invalid memory source")
        if source_id not in self._sources:
            raise DomainError("NOT_FOUND", "Memory source not found")
        if self._token(source_id) not in context.access or (write and self._token(source_id, True) not in context.access):
            raise DomainError("DENIED", "Memory topic management access denied")
        if self._topics is None:
            raise DomainError("NOT_READY", "Topic catalog is unavailable")
        return self._topics

    @_management
    def Topics(self, source_id):
        snapshot = self._catalog(source_id, self._context()).Snapshot(source_id)
        return {"source_id": source_id, "revision": snapshot.revision,
                "topics": [TopicData(topic) for topic in snapshot.topics.values()]}

    @_management
    def CreateTopic(self, source_id, parent_id, name, description):
        change = self._catalog(source_id, self._context(), write=True).CreateTopic(source_id, parent_id, name, description)
        return {"topic": TopicData(change.topic), "revision": change.revision}

    @_management
    def UpdateTopic(self, source_id, topic_id, expected_revision, **changes):
        change = self._catalog(source_id, self._context(), write=True).UpdateTopic(source_id, topic_id, expected_revision, **changes)
        return {"topic": TopicData(change.topic), "revision": change.revision}

    @_management
    def DeleteTopic(self, source_id, topic_id, expected_revision):
        revision = self._catalog(source_id, self._context(), write=True).DeleteTopic(source_id, topic_id, expected_revision)
        return {"deleted": True, "revision": revision}

    def _visible_record(self, ref, context):
        cursor, visited = None, set()
        while True:
            context.CheckCancelled()
            page = self._memory.List(MemoryListQuery(ref.source_id, limit=100, cursor=cursor), context)
            record = next((r for r in page.items if r.ref == ref and not r.deleted), None)
            if record is not None:
                return record
            cursor = page.next_cursor
            if cursor is None:
                raise DomainError("NOT_FOUND", "Memory record not found")
            if cursor in visited:
                raise DomainError("MEMORY_STORAGE", "Memory store returned a repeated list cursor")
            visited.add(cursor)

    @_management
    def AssignTopic(self, ref, topic_id, expected_revision):
        context = self._context()
        catalog = self._catalog(ref.source_id, context, write=True)
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError("INVALID_REQUEST", "Expected revision must be a nonnegative integer")
        if not text(ref.record_id) or len(ref.record_id) > 128 or not text(topic_id) or len(topic_id) > 128:
            raise DomainError("INVALID_REQUEST", "Invalid record or topic reference")
        self._visible_record(ref, context)
        snapshot = catalog.Snapshot(ref.source_id)
        if topic_id not in snapshot.topics:
            raise DomainError("NOT_FOUND", "Topic does not exist in this source")
        path = TopicPath(snapshot, topic_id)
        revision = catalog.Assign(ref.source_id, ref.record_id, topic_id, expected_revision)
        return {"source_id": ref.source_id, "record_id": ref.record_id, "topic_id": topic_id,
                "topic_path": path, "revision": revision}

    @_management
    def Search(self, query):
        context = self._context()
        if len(query.source_ids) != 1:
            raise DomainError("INVALID_REQUEST", "Management search requires one source")
        source_id = query.source_ids[0]
        snapshot = self._catalog(source_id, context).Snapshot(source_id)
        result = self._memory.Search(query, context)
        return {"records": [ManagementRecord(record) | RecordTopic(snapshot, record.ref.record_id) for record in result.records],
                "source_status": dict(result.source_status), "warnings": list(result.warnings),
                "issues": [IssueData(i) for i in result.issues], "revision": snapshot.revision}

    @_management
    def List(self, query, *, topic_id=None, unclassified=False):
        if type(unclassified) is not bool or (topic_id is not None and unclassified):
            raise DomainError("INVALID_REQUEST", "Topic and unclassified filters are mutually exclusive")
        if type(query.limit) is not int or not 1 <= query.limit <= 100:
            raise DomainError("INVALID_REQUEST", "Memory limit must be between 1 and 100")
        context = self._context()
        snapshot = self._catalog(query.source_id, context).Snapshot(query.source_id)
        if topic_id is not None and topic_id not in snapshot.topics:
            raise DomainError("NOT_FOUND", "Topic does not exist in this source")
        if "list" not in self._sources[query.source_id].get("capabilities", ()):
            raise DomainError("UNSUPPORTED", "This memory source supports search only")
        records, cursor = ReadDirectory(self._memory, snapshot, query, context, topic_id=topic_id, unclassified=unclassified)
        return {"records": [ManagementRecord(record) | RecordTopic(snapshot, record.ref.record_id) for record in records],
                "next_cursor": cursor, "source_status": {query.source_id: "ok"}, "revision": snapshot.revision}

    @_management
    def Write(self, draft, request_key=None, *, topic_id=None):
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_key)):
            raise DomainError('INVALID_REQUEST', 'Invalid memory request key')
        identity = hashlib.sha256((draft.source_id + ':' + request_key).encode()).hexdigest() if request_key else None
        context = self._context(identity)
        self._catalog(draft.source_id, context, write=True)
        selection = TopicSelection(topic_id=topic_id)
        draft = replace(draft, write_key="management:" + context.run_id, evidence=context.memory_evidence)
        try:
            receipt = self._memory.Write(draft, context, topic=selection)
        finally:
            self._invalidate(draft.source_id)
        record, snapshot = receipt.record, receipt.topic_snapshot
        return ManagementRecord(record) | RecordTopic(snapshot, record.ref.record_id) | {"revision": snapshot.revision}

    @_management
    def Request(self, action, ref, expected_version, patch=None):
        context = self._context()
        if not isinstance(action, str) or action not in {"replace", "remove"} or not text(expected_version) or len(expected_version) > 128:
            raise DomainError("INVALID_REQUEST", "Invalid management change")
        if not text(ref.source_id) or not text(ref.record_id) or len(ref.record_id) > 128:
            raise DomainError("INVALID_REQUEST", "Invalid memory reference")
        source = self._sources.get(ref.source_id)
        if source is None:
            raise DomainError("NOT_FOUND", "Memory source not found")
        operation = "update" if action == "replace" else "delete"
        if Outcome(action, context.purpose) != "approval" or self._token(ref.source_id, True) not in context.access:
            raise DomainError("DENIED", "Memory management access denied")
        if operation not in source.get("capabilities", ()):
            raise DomainError("UNSUPPORTED", "Memory source does not support this operation")
        if action == "replace":
            if patch is None:
                raise DomainError("INVALID_REQUEST", "Replacement requires content")
            validate_content(patch.content, context.memory_evidence, patch.type or "note")
            validate_metadata(None if patch.subject == UNSET else patch.subject,
                              None if patch.event_at == UNSET else patch.event_at,
                              None if patch.origin == UNSET else patch.origin)
            patch = replace(patch, evidence=context.memory_evidence)
        elif patch is not None:
            raise DomainError("INVALID_REQUEST", "Delete does not accept a patch")
        # Require a visible current record as well as write authority; the writer
        # repeats source/purpose checks and CAS at confirmation time.
        record = self._visible_record(ref, context)
        if record.version != expected_version:
            raise DomainError("CONFLICT", "Memory version is stale")
        identifier = uuid4().hex
        parameters = {"action": action, "source_id": ref.source_id, "record_id": ref.record_id,
                      "expected_version": expected_version}
        if patch is not None:
            parameters["patch"] = {name: getattr(patch, name) for name in
                ("content", "type", "subject", "event_at", "origin") if getattr(patch, name) != UNSET}
        digest = hashlib.sha256(json.dumps(parameters, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        challenge = parameters | {"challenge_id": identifier, "arguments_digest": digest,
                                   "expires_at": time.time() + self._timeout}
        with self._lock:
            self._context()
            now = self._clock()
            self._pending = {key: value for key, value in self._pending.items() if value[0] > now}
            if len(self._pending) >= 128:
                raise DomainError("CAPACITY", "Too many pending management changes")
            self._pending[identifier] = (now + self._timeout, digest, action, ref, expected_version, patch)
        return challenge

    @_management
    def Confirm(self, challenge_id, arguments_digest, approve):
        if not isinstance(challenge_id, str) or not isinstance(arguments_digest, str) or type(approve) is not bool:
            raise DomainError("INVALID_REQUEST", "Invalid management confirmation")
        with self._lock:
            context = self._context(challenge_id)
            pending = self._pending.get(challenge_id)
            if pending is None:
                raise DomainError("MANAGEMENT_STALE", "Management challenge is no longer pending")
            expires, digest, action, ref, version, patch = pending
            if expires <= self._clock():
                del self._pending[challenge_id]
                raise DomainError("MANAGEMENT_EXPIRED", "Management challenge expired")
            if (len(arguments_digest) != 64 or any(char not in "0123456789abcdef" for char in arguments_digest)
                    or not hmac.compare_digest(digest, arguments_digest)):
                raise DomainError("CONFLICT", "Management parameters changed")
            del self._pending[challenge_id]
            if not approve:
                return {"declined": True}
            if Outcome(action, context.purpose) != "approval":
                raise DomainError("DENIED", "Memory action no longer allowed")
            if action == "replace":
                snapshot = self._catalog(ref.source_id, context, write=True).Snapshot(ref.source_id)
                record = self._memory.Update(ref, replace(patch, evidence=context.memory_evidence), version, context)
                self._invalidate(ref.source_id)
                return ManagementRecord(record) | RecordTopic(snapshot, ref.record_id) | {"revision": snapshot.revision}
            self._catalog(ref.source_id, context, write=True)
            try:
                self._memory.Delete(ref, version, context)
            finally:
                self._invalidate(ref.source_id)
            return {"deleted": True}

    def Close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
