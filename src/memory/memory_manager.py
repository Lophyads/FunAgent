"""Source authorization and physical memory dispatch."""
from dataclasses import replace
from types import MappingProxyType
import time

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import ControlError, DomainError
from .common import text, validate_query
from .contracts import MemoryQuery, MemoryRecord, MemorySearchResult
from .policy import Outcome


class MemoryManager:
    def __init__(self, stores, *, owns_stores=True):
        sources = ConfMgr.GetInstance().Memory().Sources()
        if set(stores) != set(sources):
            raise DomainError("CONFIG_REFERENCE", "Invalid memory source bindings")
        for name, store in stores.items():
            if not set(sources[name].get("capabilities", ("search",))) <= store.capabilities:
                raise DomainError("UNSUPPORTED", "Memory source requests unsupported capabilities")
        self._sources, self._stores = sources, MappingProxyType(dict(stores))
        self._owns_stores = owns_stores

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
            try:
                context.CheckCancelled()
                result = self._stores[name].Search(replace(query, source_ids=(name,)), child)
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
            except ControlError:
                statuses[name] = "timeout"
                warnings.append(f"{name}:DEADLINE")
                try:
                    context.CheckCancelled()
                except ControlError:
                    raise
            except DomainError as error:
                context.CheckCancelled()
                if error.code == "MEMORY_TIMEOUT":
                    statuses[name] = "timeout"
                    warnings.append(f"{name}:DEADLINE")
                    continue
                statuses[name] = "failed"
                code = error.code if error.code in {"MEMORY_PROTOCOL", "MEMORY_STORAGE", "MEMORY_SOURCE", "NOT_READY"} else "MEMORY_SOURCE"
                warnings.append(f"{name}:{code}")
            except Exception:
                context.CheckCancelled()
                statuses[name] = "failed"
                warnings.append(f"{name}:MEMORY_SOURCE")
        return finish()

    def List(self, query, context):
        return self._authorize(query.source_id, "list", context).List(query, context)

    def Write(self, draft, context):
        store = self._authorize(draft.source_id, "write", context)
        self._check_evidence(draft.evidence, context)
        return store.Write(draft, context)

    def Update(self, ref, patch, expected_version, context):
        store = self._authorize(ref.source_id, "update", context)
        self._check_evidence(patch.evidence, context)
        return store.Update(ref, patch, expected_version, context)

    @staticmethod
    def _check_evidence(evidence, context):
        if context.purpose in {"main", "review"}:
            trusted = context.review_evidence if context.purpose == "review" else context.memory_evidence
            if not evidence or not trusted or not set(evidence) <= set(trusted):
                raise DomainError("DENIED", "Memory writes require authorized evidence")

    def Delete(self, ref, expected_version, context):
        store = self._authorize(ref.source_id, "delete", context)
        self._check_evidence(context.memory_evidence, context)
        return store.Delete(ref, expected_version, context)

    def GetProfile(self, context):
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
