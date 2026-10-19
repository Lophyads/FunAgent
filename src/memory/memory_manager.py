"""Source authorization and physical memory dispatch."""
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import contextmanager
from types import MappingProxyType
import time

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import ControlError, DomainError
from .common import text, validate_query, NormalizeTopicName
from .contracts import (MemoryQuery, MemoryRecord, MemorySearchResult, MemoryWriteReceipt,
                        MemoryFailure, TopicConflict, TopicSelection, TOPIC_SYNC_MESSAGE, UNSET)
from .failure import BindIssue, ErrorIssue, FailureData, IssueData, IssueWarnings
from .protocols import TopicCatalog
from .policy import Outcome
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Memory, Query
from .prompt import RecordData


@contextmanager
def _scope(context, name, **fields):
    mgr = ObservationMgr.GetInstance()
    scope = mgr.Suspend() if context.purpose in {'review', 'management'} else mgr.Scope(
        name, run_id=context.run_id, purpose=context.purpose, **fields)
    with scope:
        try:
            yield
        except MemoryFailure as error:
            mgr.Update(effect=error.issue.effect, output=lambda: FailureData(error))
            raise


def _receipt(result):
    if isinstance(result, MemoryRecord):
        return RecordData(result)
    if hasattr(result, 'items'):
        return dict(records=[RecordData(r) for r in result.items], next_cursor=result.next_cursor)
    return result


class MemoryManager:
    def __init__(self, stores, *, topics: TopicCatalog | None = None, owns_stores=True, conf=None):
        self._conf = conf if conf is not None else ConfMgr.GetInstance().Memory()
        sources = self._conf.Sources()
        if set(stores) != set(sources):
            raise DomainError("CONFIG_REFERENCE", "Invalid memory source bindings")
        for name, store in stores.items():
            if not set(sources[name].get("capabilities", ("search",))) <= store.capabilities:
                raise DomainError("UNSUPPORTED", "Memory source requests unsupported capabilities")
        self._sources, self._stores = sources, MappingProxyType(dict(stores))
        self._owns_stores = owns_stores
        self._topics = topics

    def SupportedTypes(self, source_ids, context):
        stores = {name: self._authorize(name, "search", context) for name in source_ids}
        return {name: None if store.supported_types is None else tuple(sorted(store.supported_types))
                for name, store in stores.items()}

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
        except ControlError:
            raise
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
            from .common import SearchStatus
            status = SearchStatus(result.source_status)
            mgr.Complete(status=status, output=lambda: Memory(result))
            return result

    def Check(self, source_ids, context):
        """Explicit authorized probe. Evidence stays inside Search and is never returned."""
        context.CheckCancelled()
        if context.purpose != "management":
            raise DomainError("DENIED", "Memory checks require management access")
        names = tuple(dict.fromkeys(source_ids))
        # Authorize the whole selection before any IO, including later sources.
        for name in names:
            self._authorize(name, "search", context)
        sources = []
        for name in names:
            context.CheckCancelled()
            result = self.Search(MemoryQuery("FunAgent memory connectivity check", (name,), limit=1), context)
            status = result.source_status.get(name, "failed")
            issues = result.issues
            if status == "timeout" and not issues:
                issues = (ErrorIssue(DomainError("MEMORY_TIMEOUT", ""), source_id=name, stage="search"),)
            observed = status in {"ok", "partial"}
            store = self._sources[name].get("store")
            sources.append({"source_id": name, "status": status,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "stages": {"search": "verified" if observed else "not_verified",
                    "embedding": ("not_applicable" if store == "sqlite" else
                                  "verified" if store == "supabase" and observed else "not_verified"),
                    "model_identity": "not_applicable" if store == "sqlite" else "not_verified",
                    "profile_coverage": ("verified" if store == "supabase" and observed else
                                         "not_applicable" if store == "sqlite" else "not_verified"),
                    "index": "not_verified",
                    "write": "not_verified"},
                "issues": [IssueData(issue) for issue in issues]})
        return {"sources": sources}

    def _search(self, query, context):
        mgr = ObservationMgr.GetInstance()
        context.CheckCancelled()
        validate_query(query)
        names = query.source_ids or tuple(name for name in self._sources if self._token(name) in context.access)
        selected = []
        for name in names:
            if name not in self._sources:
                raise DomainError("NOT_FOUND", "Memory source not found")
            if query.kind is None or self._sources[name]["kind"] == query.kind:
                self._authorize(name, "search", context)
                if name not in selected:
                    selected.append(name)
        groups, statuses, warnings, issues = [], {}, [], []

        def finish():
            # Preserve source-local ordering; scores are never compared.
            records = tuple(group[index] for index in range(max((len(g) for g in groups), default=0))
                            for group in groups if index < len(group))[:query.limit]
            result = MemorySearchResult(records, statuses, tuple(dict.fromkeys((*warnings, *IssueWarnings(issues)))),
                                        issues=tuple(dict.fromkeys(issues)))
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
                    status = result.source_status.get(name, "ok")
                    if (set(result.source_status) - {name} or status not in {"ok", "partial"} or len(result.issues) > 16):
                        raise DomainError("MEMORY_PROTOCOL", "Invalid memory source result")
                    bound = tuple(BindIssue(issue, name, read=True) for issue in result.issues)
                    groups.append(result.records)
                    statuses[name] = "partial" if bound or status == "partial" else "ok"
                    issues.extend(bound)
                    # Warnings are generated here; driver strings cannot leak credentials.
                    if f"{name}:FTS_UNAVAILABLE" in result.warnings:
                        warnings.append(f"{name}:FTS_UNAVAILABLE")
                    mgr.Complete(status=statuses[name])
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
                    issue = ErrorIssue(error, source_id=name, stage="search")
                    issues.append(issue)
                    if isinstance(error, MemoryFailure):
                        issues.extend(BindIssue(i, name, read=True) for i in error.related)
                    if error.code == "MEMORY_TIMEOUT":
                        mgr.Complete(status='timeout', error_code=error.code)
                        statuses[name] = "timeout"
                        warnings.append(f"{name}:MEMORY_TIMEOUT")
                        continue
                    statuses[name] = "failed"
                    code = issue.code
                    warnings.append(f"{name}:{code}")
                    mgr.Complete(status='failed', error_code=code)
                except Exception:
                    context.CheckCancelled()
                    statuses[name] = "failed"
                    warnings.append(f"{name}:MEMORY_SOURCE")
                    issues.append(ErrorIssue(None, source_id=name, stage="search"))
                    mgr.Complete(status='failed', error_code='MEMORY_SOURCE')
        return finish()

    @staticmethod
    def _change(store, operation, source_id, context, *args):
        """Preserve structured failures; legacy stores cannot prove post-dispatch failure is harmless."""
        try:
            return getattr(store, operation)(*args, context)
        except ControlError:
            raise
        except MemoryFailure as error:
            if error.ref is not None and error.ref.source_id != source_id:
                raise MemoryFailure(ErrorIssue(DomainError('MEMORY_PROTOCOL', ''), source_id=source_id,
                                               stage='verify', effect='unknown')) from None
            raise MemoryFailure(BindIssue(error.issue, source_id), ref=error.ref, version=error.version,
                                related=tuple(BindIssue(i, source_id) for i in error.related)) from None
        except DomainError as error:
            harmless = error.code in {'INVALID_REQUEST', 'DENIED', 'UNSUPPORTED', 'NOT_FOUND', 'CONFLICT',
                'CONFIG_CREDENTIAL', 'NOT_READY'} or error.code.startswith('EMBEDDING_')
            raise MemoryFailure(ErrorIssue(error, source_id=source_id,
                effect='none' if harmless else 'unknown')) from None
        except Exception:
            context.CheckCancelled()
            raise MemoryFailure(ErrorIssue(None, source_id=source_id, effect='unknown')) from None

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
            try:
                result = self._change(store, 'Write', draft.source_id, context, draft)
            except MemoryFailure as error:
                mgr.Update(effect=error.issue.effect, output=lambda: FailureData(error))
                if (error.code in {'INDEX_NOT_READY', 'INDEX_STATE_UNKNOWN'} and
                        error.issue.effect == 'known' and error.ref is not None and error.version is not None):
                    context.CheckCancelled()
                    try:
                        self._sync_topic(draft.source_id, error.ref.record_id, topic_id,
                                         require_same=context.purpose == 'management')
                    except ControlError:
                        raise
                    except DomainError:
                        raise MemoryFailure(error.issue, ref=error.ref, version=error.version,
                            related=(*error.related[:15], ErrorIssue(DomainError('TOPIC_SYNC_FAILED', ''),
                                source_id=draft.source_id, stage='topic', effect='known'))) from None
                raise
            mgr.Update(effect='known', response_received=True, output=lambda: _receipt(result))
            if (not isinstance(result, MemoryRecord) or result.ref.source_id != draft.source_id
                    or not text(result.ref.record_id) or len(result.ref.record_id) > 128):
                raise MemoryFailure(ErrorIssue(DomainError('MEMORY_PROTOCOL', ''), source_id=draft.source_id, effect='unknown'))
            try:
                context.CheckCancelled()
                snapshot = self._sync_topic(draft.source_id, result.ref.record_id, topic_id, require_same=context.purpose == "management")
            except ControlError:
                raise
            except DomainError as error:
                raise MemoryFailure(ErrorIssue(error, source_id=draft.source_id, stage='topic', effect='known'),
                                    ref=result.ref, version=result.version) from None
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
            result = self._change(store, 'Update', ref.source_id, context, ref, patch, expected_version)
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
            result = self._change(store, 'Delete', ref.source_id, context, ref, expected_version)
            mgr.Update(effect='known', response_received=True)
            try:
                self._sync_topic(ref.source_id, ref.record_id)
            except ControlError:
                raise
            except DomainError as error:
                raise MemoryFailure(ErrorIssue(error, source_id=ref.source_id, stage='topic', effect='known')) from None
            mgr.Complete(status='ok', output=lambda: _receipt(result), effect='known')
            return result

    def GetProfile(self, context):
        mgr = ObservationMgr.GetInstance()
        with _scope(context, 'profile.retrieve'):
            result = self._get_profile(context)
            mgr.Complete(status='ready' if result else 'empty', output=lambda: [RecordData(r) for r in result])
            return result

    def _get_profile(self, context):
        policy = self._conf.Policy("profile")
        names = tuple(name for name in policy.get("sources", ()) if policy.get("enabled", False))
        if not names:
            return ()
        result = self.Search(MemoryQuery(source_ids=names, kind="episodic", types=("profile",), limit=policy.get("max_records", 10)), context)
        if any(result.source_status.get(name) != "ok" for name in names):
            if result.issues:
                raise MemoryFailure(result.issues[0], related=result.issues[1:17])
            raise DomainError("MEMORY_SOURCE", "Profile source failed")
        from .prompt import FitBranch
        return FitBranch("profile", result.records, {name: "ready" if any(r.ref.source_id == name for r in result.records) else "empty" for name in names},
                         result.warnings, (), policy.get("max_records", 10), policy.get("max_chars", 16000)).records
