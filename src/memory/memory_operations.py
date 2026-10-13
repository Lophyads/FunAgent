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
from .contracts import MemoryListQuery, UNSET
from .policy import Outcome
from .prompt import RecordData
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
    def __init__(self, memory, conf, *, clock=time.monotonic):
        self._memory = memory
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
        return [{"source_id": name, "kind": source["kind"], "capabilities": list(source.get("capabilities", ("search",))),
                 "writable": self._token(name, True) in self._access}
                for name, source in self._sources.items() if self._token(name) in self._access]

    def DefaultSource(self):
        self._context()
        return next((name for name in self._facts if self._sources[name]['kind'] == 'episodic'
                     and self._token(name) in self._access), None)

    def _token(self, name, write=False):
        return self._sources[name].get("write_access" if write else "read_access",
                                      f"memory.{name}.{'write' if write else 'read'}")

    @_management
    def Search(self, query):
        result = self._memory.Search(query, self._context())
        return {"records": [ManagementRecord(record) for record in result.records],
                "source_status": dict(result.source_status), "warnings": list(result.warnings)}

    @_management
    def List(self, query):
        page = self._memory.List(query, self._context())
        return {"records": [ManagementRecord(record) for record in page.items], "next_cursor": page.next_cursor,
                "source_status": {query.source_id: "ok"}}

    @_management
    def Write(self, draft, request_key=None):
        if request_key is not None and (not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_key)):
            raise DomainError('INVALID_REQUEST', 'Invalid memory request key')
        identity = hashlib.sha256((draft.source_id + ':' + request_key).encode()).hexdigest() if request_key else None
        context = self._context(identity)
        draft = replace(draft, write_key="management:" + context.run_id, evidence=context.memory_evidence)
        return ManagementRecord(self._memory.Write(draft, context))

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
        cursor = None
        while True:
            page = self._memory.List(MemoryListQuery(ref.source_id, limit=100, cursor=cursor), context)
            record = next((r for r in page.items if r.ref == ref), None)
            if record is not None:
                if record.version != expected_version:
                    raise DomainError("CONFLICT", "Memory version is stale")
                break
            cursor = page.next_cursor
            if cursor is None:
                raise DomainError("NOT_FOUND", "Memory record not found")
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
                return ManagementRecord(self._memory.Update(ref, replace(patch, evidence=context.memory_evidence), version, context))
            self._memory.Delete(ref, version, context)
            return {"deleted": True}

    def Close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
