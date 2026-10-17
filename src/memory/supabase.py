"""Episodic memory over the versioned FunAgent PostgREST RPC contract."""
import base64
import json
import os
from dataclasses import asdict
from math import isfinite

from ..frame.contracts import DomainError, Page
from .contracts import MemoryRecord, MemoryRef, MemorySearchResult, MemoryIssue, MemoryFailure, UNSET
from .failure import IssueWarnings
from .common import text, validate_filters, timestamp, validate_query, validate_content, validate_metadata, normalize
from .embedding import ValidateProfile, ValidateVector
from .transport import JSONHTTP, ValidateConnection


class SupabaseMemoryStore:
    supported_types = None
    supports_deleted_read = True
    capabilities = frozenset({"search", "list", "write", "update", "delete"})

    def __init__(self, settings, source_id, embedder, *, transport=None, owns_embedder=False):
        if not text(source_id) or not text(settings.get("namespace")) or settings.get("kind", "episodic") != "episodic":
            raise DomainError("CONFIG_INVALID", "Supabase memory requires an episodic namespace")
        ValidateProfile(embedder.profile)
        ValidateConnection(settings["endpoint"], settings.get("timeout", 30))
        token = os.environ.get(settings.get("credential_env", "SUPABASE_MEMORY_TOKEN"))
        api_key = os.environ.get(settings.get("api_key_env", settings.get("credential_env", "SUPABASE_MEMORY_TOKEN")))
        if not token or not api_key:
            raise DomainError("CONFIG_CREDENTIAL", "Supabase memory credentials are missing")
        self.source_id, self.kind = source_id, "episodic"
        self._namespace, self._embedder = settings["namespace"], embedder
        self._owns_embedder, self._embedder_closed = owns_embedder, False
        self._profile = ValidateProfile(embedder.profile)
        self._threshold = settings.get("similarity_threshold", 0.0)
        if type(self._threshold) not in (int, float) or not isfinite(self._threshold) or not -1 <= self._threshold <= 1:
            raise DomainError("CONFIG_INVALID", "Invalid memory similarity threshold")
        headers = {"apikey": api_key}
        if not token.startswith(("sb_secret_", "sb_publishable_")):
            headers["Authorization"] = "Bearer " + token
        self._http = JSONHTTP(settings["endpoint"].rstrip("/"), headers,
                              settings.get("timeout", 30), transport=transport)

    def _rpc(self, name, values, context):
        return self._http.Post("/rpc/funagent_memory_" + name,
                               {"p_namespace": self._namespace, "p_source_id": self.source_id} | values, context,
                               mutation=name in {"add", "replace", "remove"},
                               stage="search" if name.startswith("search") else "request")

    def _record(self, row):
        try:
            if (row["namespace"] != self._namespace or row["source_id"] != self.source_id or
                    row["kind"] != self.kind or type(row["seq"]) is not int or row["seq"] <= 0 or
                    any(not text(row[k]) for k in ("record_id", "content", "type", "version")) or
                    type(row["deleted"]) is not bool or not isinstance(row["evidence"], list) or
                    not row["evidence"] or any(not text(e) for e in row["evidence"])):
                raise ValueError
            for field in ("event_at", "created_at", "updated_at"):
                timestamp(row.get(field))
            return MemoryRecord(MemoryRef(self.source_id, row["record_id"]), self.kind, row["type"], row["content"],
                                f"memory://{self.source_id}/{row['record_id']}", row["version"], tuple(row["evidence"]),
                                row["deleted"], row.get("subject"), row.get("event_at"), row.get("created_at"),
                                row.get("updated_at"), row.get("origin"))
        except (KeyError, TypeError, ValueError, DomainError):
            raise DomainError("MEMORY_PROTOCOL", "Invalid Supabase memory record") from None

    def _rows(self, payload, limit):
        if not isinstance(payload, list) or len(payload) > limit:
            raise DomainError("MEMORY_PROTOCOL", "Invalid Supabase memory batch")
        records = tuple(self._record(row) for row in payload)
        if len({r.ref for r in records}) != len(records):
            raise DomainError("MEMORY_PROTOCOL", "Duplicate Supabase memory record")
        return records

    @staticmethod
    def _filters(query):
        validate_filters(query)
        return {"p_subject": query.subject, "p_origin": query.origin,
                "p_event_from": timestamp(query.event_from), "p_event_to": timestamp(query.event_to)}

    def List(self, query, context):
        with self._http.Borrow(context):
            if (query.source_id != self.source_id or type(query.limit) is not int or not 1 <= query.limit <= 100 or
                    query.type is not None and not text(query.type)):
                raise DomainError("INVALID_REQUEST", "Invalid memory list query")
            filters = self._filters(query)
            identity = [self._namespace, self.source_id, query.type, *filters.values()]
            after = 0
            if query.cursor is not None:
                try:
                    saved, after = json.loads(base64.urlsafe_b64decode(query.cursor.encode("ascii")))
                    if saved != identity or type(after) is not int or after <= 0:
                        raise ValueError
                except (ValueError, TypeError, AttributeError, UnicodeError):
                    raise DomainError("INVALID_REQUEST", "Invalid memory cursor") from None
            payload = self._rpc("list", {"p_type": query.type, "p_limit": query.limit + 1, "p_after": after} | filters, context)
            records = self._rows(payload, query.limit + 1)
            sequences = [row["seq"] for row in payload]
            if any(r.deleted for r in records) or sequences != sorted(set(sequences)) or any(seq <= after for seq in sequences):
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory page order")
            cursor = None
            if len(records) > query.limit:
                cursor = base64.urlsafe_b64encode(json.dumps([identity, payload[query.limit - 1]["seq"]]).encode()).decode()
            return Page(records[:query.limit], cursor)

    def Search(self, query, context):
        with self._http.Borrow(context):
            validate_query(query)
            if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
                return MemorySearchResult()
            vector = self._vector(query.text, "query", context) if query.text.strip() else None
            try:
                payload = self._rpc("search_v2", {"p_types": list(query.types), "p_limit": query.limit,
                    "p_include_deleted": query.include_deleted, "p_profile": asdict(self._profile),
                    "p_vector": vector, "p_threshold": self._threshold} | self._filters(query), context)
            except MemoryFailure as error:
                if error.issue.http_status == 404:
                    raise MemoryFailure(MemoryIssue(code="MEMORY_PROTOCOL", stage="search",
                        source_id=self.source_id, http_status=404)) from None
                raise
            if not isinstance(payload, dict) or set(payload) != {"records", "profile_coverage"}:
                raise DomainError("MEMORY_PROTOCOL", "Invalid Supabase search v2 response")
            coverage = payload["profile_coverage"]
            if (not isinstance(coverage, dict) or set(coverage) != {"matching", "incompatible"} or
                    any(type(v) is not int or v < 0 for v in coverage.values())):
                raise DomainError("MEMORY_PROTOCOL", "Invalid Supabase profile coverage")
            records = self._rows(payload["records"], query.limit)
            if len(records) > coverage["matching"]:
                raise DomainError("MEMORY_PROTOCOL", "Invalid Supabase coverage count")
            if not query.include_deleted and any(r.deleted for r in records):
                raise DomainError("MEMORY_PROTOCOL", "Unexpected deleted memory record")
            issues = ()
            if vector is not None and coverage["incompatible"]:
                issue = MemoryIssue(code="EMBEDDING_PROFILE_MISMATCH", stage="search", source_id=self.source_id)
                if not coverage["matching"]:
                    raise MemoryFailure(issue)
                issues = (issue,)
            return MemorySearchResult(records, {self.source_id: "partial" if issues else "ok"},
                                      IssueWarnings(issues), issues=issues)

    def _vector(self, content, purpose, context):
        if self._embedder.profile != self._profile:
            raise DomainError("CONFIG_INVALID", "Embedding profile changed; rebuild required")
        vectors = self._embedder.Embed((content,), purpose, context)
        context.CheckCancelled()
        if not isinstance(vectors, (tuple, list)) or len(vectors) != 1:
            raise DomainError("EMBEDDING_PROTOCOL", "Invalid embedding batch")
        return json.dumps(ValidateVector(vectors[0], self._profile.dimensions), allow_nan=False)

    def Write(self, draft, context):
        with self._http.Borrow(context):
            validate_content(draft.content, draft.evidence, draft.type)
            subject, event_at, origin = validate_metadata(draft.subject, draft.event_at, draft.origin)
            if draft.source_id != self.source_id or not text(draft.write_key):
                raise DomainError("INVALID_REQUEST", "Invalid memory write identity")
            vector = self._vector(draft.content, "document", context)
            payload = self._rpc("add", {"p_write_key": draft.write_key, "p_data": {
                "content": draft.content, "search_text": normalize(draft.content), "type": draft.type,
                "evidence": list(draft.evidence), "subject": subject, "event_at": event_at, "origin": origin},
                "p_vector": vector, "p_profile": asdict(self._profile)}, context)
            return self._record(payload)

    def Update(self, ref, patch, expected_version, context):
        with self._http.Borrow(context):
            self._reference(ref, expected_version)
            validate_content(patch.content, patch.evidence, "note" if patch.type is None else patch.type)
            values = {"content": patch.content, "search_text": normalize(patch.content), "evidence": list(patch.evidence)}
            if patch.type is not None:
                values["type"] = patch.type
            subject, event_at, origin = validate_metadata(*(None if getattr(patch, field) is UNSET else getattr(patch, field)
                                                           for field in ("subject", "event_at", "origin")))
            for field, value in (("subject", subject), ("event_at", event_at), ("origin", origin)):
                if getattr(patch, field) is not UNSET:
                    values[field] = value
            vector = self._vector(patch.content, "document", context)
            payload = self._rpc("replace", {"p_record_id": ref.record_id, "p_expected_version": expected_version,
                                           "p_patch": values, "p_vector": vector, "p_profile": asdict(self._profile)}, context)
            return self._record(payload)

    def _reference(self, ref, expected_version):
        if ref.source_id != self.source_id or not text(ref.record_id) or not text(expected_version):
            raise DomainError("INVALID_REQUEST", "Invalid memory reference or version")

    def Delete(self, ref, expected_version, context):
        with self._http.Borrow(context):
            self._reference(ref, expected_version)
            payload = self._rpc("remove", {"p_record_id": ref.record_id, "p_expected_version": expected_version}, context)
            if not isinstance(payload, dict) or payload.get("removed") is not True:
                raise DomainError("MEMORY_PROTOCOL", "Invalid memory removal receipt")

    def Close(self):
        self._http.Close()
        if self._owns_embedder and not self._embedder_closed:
            self._embedder.Close()
            self._embedder_closed = True
