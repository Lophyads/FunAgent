"""Source authorization and physical memory dispatch."""
from dataclasses import replace
from types import MappingProxyType
import time

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import ControlError, DomainError
from .common import text, validate_query, NormalizeTopicName
from .contracts import (MemoryQuery, MemoryRecord, MemorySearchResult, MemoryWriteReceipt,
                        TopicConflict, TopicSelection, TOPIC_SYNC_MESSAGE, UNSET)
from .protocols import TopicCatalog
from .policy import Outcome
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Memory, Query
from .prompt import RecordData


def _scope(context, name, **fields):
    mgr = ObservationMgr.GetInstance()
    return mgr.Suspend() if context.purpose in {'review', 'management'} else mgr.Scope(
        name, run_id=context.run_id, purpose=context.purpose, **fields)


def _receipt(result):
    if isinstance(result, MemoryRecord):
        return RecordData(result)
    if hasattr(result, 'items'):
        return dict(records=[RecordData(r) for r in result.items], next_cursor=result.next_cursor)
    return result


class MemoryManager:
    def __init__(self, stores, *, topics: TopicCatalog | None = None, owns_stores=True):
        sources = ConfMgr.GetInstance().Memory().Sources()
        if set(stores) != set(sources):
            raise DomainError("CONFIG_REFERENCE", "Invalid memory source bindings")
        for name, store in stores.items():
            if not set(sources[name].get("capabilities", ("search",))) <= store.capabilities:
                raise DomainError("UNSUPPORTED", "Memory source requests unsupported capabilities")
        self._sources, self._stores = sources, MappingProxyType(dict(stores))
        self._owns_stores = owns_stores
        self._topics = topics

    def ReadTopics(self, source_ids, context):
        """Read only after source/purpose authorization; never expose the JSON path."""
        for name in source_ids:
            self._authorize(name, "search", context)
        if self._topics is None:
            raise DomainError("NOT_READY", "Topic catalog is unavailable")
        return tuple(self._topics.Snapshot(name) for name in dict.fromkeys(source_ids))

    def _select_topic(self, source_id, selection):
        if self._topics is None:
            raise DomainError("NOT_READY", "Topic catalog is unavailable")
        if not isinstance(selection, TopicSelection):
            raise DomainError("INVALID_REQUEST", "Memory creation requires a topic")
        if selection.topic_id is not None:
            if selection.topic_id not in self._topics.Snapshot(source_id).topics:
                raise DomainError("NOT_FOUND", "Topic does not exist in this source")
            return selection.topic_id
        try:
            return self._topics.CreateTopic(source_id, selection.parent_id, selection.name, selection.description).topic.topic_id
        except DomainError as error:
            if error.code != "CONFLICT":
                raise
            # Concurrent review proposals reuse the winner without changing its description.
            for topic in self._topics.Snapshot(source_id).topics.values():
                if topic.parent_id == selection.parent_id and NormalizeTopicName(topic.name) == NormalizeTopicName(selection.name):
                    return topic.topic_id
            raise

    def _sync_topic(self, source_id, record_id, topic_id=None, *, require_same=False):
        if self._topics is None:
            raise DomainError("NOT_READY", "Topic catalog is unavailable")
        try:
            if topic_id is None:
                return self._topics.Unassign(source_id, record_id)
            return self._assign_record(source_id, record_id, topic_id, require_same)
        except TopicConflict:
            if require_same:
                raise
            raise DomainError("TOPIC_SYNC_FAILED", TOPIC_SYNC_MESSAGE) from None
        except Exception:
            raise DomainError("TOPIC_SYNC_FAILED", TOPIC_SYNC_MESSAGE) from None

    def _assign_record(self, source_id, record_id, topic_id, require_same):
        # CAS prevents a concurrent human classification from being overwritten.
        # Build the receipt from the CAS input, never from a later snapshot read.
        for _ in range(8):
            snapshot = self._topics.Snapshot(source_id)
            current = snapshot.assignments.get(record_id)
            if current is not None:
                if require_same and current != topic_id:
                    raise TopicConflict("This request already has a different topic; use the classification endpoint")
                return snapshot
            candidate = replace(snapshot, revision=snapshot.revision + 1,
                                assignments=dict(snapshot.assignments) | {record_id: topic_id})
            try:
                revision = self._topics.Assign(source_id, record_id, topic_id, snapshot.revision)
            except DomainError as error:
                if error.code == "CONFLICT":
                    continue
                raise
            return replace(candidate, revision=revision)
        raise DomainError("MEMORY_STORAGE", "Topic catalog is busy; classification was not committed")

    def Close(self):
        if not self._owns_stores:
            return
        error = None
        seen = set()
        for store in self._stores.values():
            if id(store) in seen:
                continue
            seen.add(id(store))
            try:
                store.Close()
            except Exception:
                error = DomainError("CLOSE_FAILED", "Memory store close failed")
        if error is not None:
            raise error


    def _token(self, source_id, write=False):
        option = "write_access" if write else "read_access"
        return self._sources[source_id].get(option, f"memory.{source_id}.{'write' if write else 'read'}")

    def _authorize(self, source_id, operation, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.authorization', input={'source_id': source_id, 'operation': operation}):
            result = self._authorize_source(source_id, operation, context)
            mgr.Complete(status='allowed', output={'authorized': True})
            return result

    def _authorize_source(self, source_id, operation, context):
        context.CheckCancelled()
        if source_id not in self._sources:
            raise DomainError("NOT_FOUND", "Memory source not found")
        source, store = self._sources[source_id], self._stores[source_id]
        action = {"write": "add", "update": "replace", "delete": "remove"}.get(operation, operation)
        if Outcome(action, context.purpose) == "deny":
            raise DomainError("DENIED", "Memory action is forbidden for this purpose")
        if operation not in source.get("capabilities", ("search",)) or operation not in store.capabilities:
            raise DomainError("UNSUPPORTED", "Memory source does not support this operation")
        write = operation in {"write", "update", "delete"}
        if self._token(source_id, write) not in context.access or (write and source["kind"] != "episodic"
                                                                 and context.purpose in {"main", "review"}):
            raise DomainError("DENIED", "Memory access denied")
        return store

    def Search(self, query, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.search', kind='retriever', input=lambda: Query(query)):
            result = self._search(query, context)
            status = 'ok' if all(s == 'ok' for s in result.source_status.values()) else 'partial' if result.records else 'failed'
            mgr.Complete(status=status, output=lambda: Memory(result))
            return result

    def _search(self, query, context):
        mgr = ObservationMgr.GetInstance()
        context.CheckCancelled()
        validate_query(query)
        names = query.source_ids or tuple(name for name in self._sources if self._token(name) in context.access)
        if query.include_deleted and (context.purpose != "review" or any(self._sources.get(name, {}).get("kind") != "episodic" for name in names)):
            raise DomainError("DENIED", "Deleted receipts are available only to episodic review")
        selected = []
        for name in names:
            if name not in self._sources:
                raise DomainError("NOT_FOUND", "Memory source not found")
            if query.kind is None or self._sources[name]["kind"] == query.kind:
                self._authorize(name, "search", context)
                if name not in selected:
                    selected.append(name)
        groups, statuses, warnings = [], {}, []

        def finish():
            # Preserve source-local ordering; scores are never compared.
            records = tuple(group[index] for index in range(max((len(g) for g in groups), default=0))
                            for group in groups if index < len(group))[:query.limit]
            result = MemorySearchResult(records, statuses, tuple(warnings))
            return result

        for name in selected:
            child = context
            timeout = self._sources[name].get("timeout")
            if timeout is not None:
                child = replace(context, deadline=min(context.deadline or float("inf"), time.monotonic() + timeout))
            with mgr.Scope('memory.source.search', kind='retriever', source_id=name,
                           input=lambda: Query(replace(query, source_ids=(name,))), deadline=child.deadline):
                try:
                    context.CheckCancelled()
                    result = self._stores[name].Search(replace(query, source_ids=(name,)), child)
                    mgr.Update(output=lambda: Memory(result), response_received=True)
                    child.CheckCancelled()
                    seen = set()
                    for record in result.records:
                        if (not isinstance(record, MemoryRecord) or record.ref.source_id != name or
                                record.kind != self._sources[name]["kind"] or record.ref in seen or
                                any(not text(v) for v in (record.ref.record_id, record.content, record.type, record.citation))):
                            raise DomainError("MEMORY_PROTOCOL", "Invalid memory evidence")
                        seen.add(record.ref)
                    groups.append(result.records)
                    statuses[name] = "ok"
                    # Warnings are generated here; driver strings cannot leak credentials.
                    if f"{name}:FTS_UNAVAILABLE" in result.warnings:
                        warnings.append(f"{name}:FTS_UNAVAILABLE")
                    mgr.Complete(status='ok')
                except ControlError:
                    statuses[name] = "timeout"
                    warnings.append(f"{name}:DEADLINE")
                    try:
                        context.CheckCancelled()
                    except ControlError as error:
                        mgr.Complete(status='cancelled' if error.code == 'CANCELLED' else 'timeout', error_code=error.code)
                        raise
                    mgr.Complete(status='timeout', error_code='DEADLINE')
                except DomainError as error:
                    context.CheckCancelled()
                    if error.code == "MEMORY_TIMEOUT":
                        mgr.Complete(status='timeout', error_code=error.code)
                        statuses[name] = "timeout"
                        warnings.append(f"{name}:DEADLINE")
                        continue
                    statuses[name] = "failed"
                    code = error.code if error.code in {"MEMORY_PROTOCOL", "MEMORY_STORAGE", "MEMORY_SOURCE", "NOT_READY"} else "MEMORY_SOURCE"
                    warnings.append(f"{name}:{code}")
                    mgr.Complete(status='failed', error_code=code)
                except Exception:
                    context.CheckCancelled()
                    statuses[name] = "failed"
                    warnings.append(f"{name}:MEMORY_SOURCE")
                    mgr.Complete(status='failed', error_code='MEMORY_SOURCE')
        return finish()

    def List(self, query, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.operation', input=lambda: dict(operation='list', source_id=query.source_id,
                    type=query.type, limit=query.limit, cursor=query.cursor, subject=query.subject,
                    event_from=query.event_from, event_to=query.event_to, origin=query.origin)):
            result = self._authorize(query.source_id, 'list', context).List(query, context)
            mgr.Complete(status='ok', output=lambda: _receipt(result))
            return result

    def Write(self, draft, context, *, topic=None):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.operation', input=lambda: dict(operation='write', source_id=draft.source_id,
                    content=draft.content, evidence=draft.evidence, type=draft.type, write_key=draft.write_key,
                    subject=draft.subject, event_at=draft.event_at, origin=draft.origin)):
            store = self._authorize(draft.source_id, 'write', context)
            self._check_evidence(draft.evidence, context)
            topic_id = self._select_topic(draft.source_id, topic)
            result = store.Write(draft, context)
            mgr.Update(effect='known', response_received=True, output=lambda: _receipt(result))
            if (not isinstance(result, MemoryRecord) or result.ref.source_id != draft.source_id
                    or not text(result.ref.record_id) or len(result.ref.record_id) > 128):
                raise DomainError("MEMORY_PROTOCOL", "Memory write returned an invalid receipt; verify the source before retrying")
            snapshot = self._sync_topic(draft.source_id, result.ref.record_id, topic_id, require_same=context.purpose == "management")
            mgr.Complete(status='ok', output=lambda: _receipt(result), effect='known')
            return MemoryWriteReceipt(result, snapshot)

    def Update(self, ref, patch, expected_version, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.operation', input=lambda: dict(operation='update', source_id=ref.source_id,
                    record_id=ref.record_id, expected_version=expected_version, content=patch.content,
                    evidence=patch.evidence, type=patch.type,
                    **{key: getattr(patch, key) for key in ('subject', 'event_at', 'origin') if getattr(patch, key) != UNSET})):
            store = self._authorize(ref.source_id, 'update', context)
            self._check_evidence(patch.evidence, context)
            result = store.Update(ref, patch, expected_version, context)
            mgr.Complete(status='ok', output=lambda: _receipt(result), effect='known')
            return result

    @staticmethod
    def _check_evidence(evidence, context):
        if context.purpose in {"main", "review"}:
            trusted = context.review_evidence if context.purpose == "review" else context.memory_evidence
            if not evidence or not trusted or not set(evidence) <= set(trusted):
                raise DomainError("DENIED", "Memory writes require authorized evidence")

    def Delete(self, ref, expected_version, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'memory.operation', input=dict(operation='delete', source_id=ref.source_id,
                                                           record_id=ref.record_id, expected_version=expected_version)):
            store = self._authorize(ref.source_id, 'delete', context)
            self._check_evidence(context.memory_evidence, context)
            if self._topics is None:
                raise DomainError("NOT_READY", "Topic catalog is unavailable")
            result = store.Delete(ref, expected_version, context)
            mgr.Update(effect='known', response_received=True)
            self._sync_topic(ref.source_id, ref.record_id)
            mgr.Complete(status='ok', output=lambda: _receipt(result), effect='known')
            return result

    def GetProfile(self, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'profile.retrieve'):
            result = self._get_profile(context)
            mgr.Complete(status='ready' if result else 'empty', output=lambda: [RecordData(r) for r in result])
            return result

    def _get_profile(self, context):
        policy = ConfMgr.GetInstance().Memory().Policy("profile")
        names = tuple(name for name in policy.get("sources", ()) if policy.get("enabled", False))
        if not names:
            return ()
        result = self.Search(MemoryQuery(source_ids=names, kind="episodic", types=("profile",), limit=policy.get("max_records", 10)), context)
        if any(result.source_status.get(name) != "ok" for name in names):
            raise DomainError("MEMORY_SOURCE", "Profile source failed")
        from .prompt import FitBranch
        return FitBranch("profile", result.records, {name: "ready" if any(r.ref.source_id == name for r in result.records) else "empty" for name in names},
                         result.warnings, (), policy.get("max_records", 10), policy.get("max_chars", 16000)).records
