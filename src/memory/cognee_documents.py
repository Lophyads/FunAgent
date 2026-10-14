"""Document-level management for FunAgent-owned text in one Cognee dataset.

Cognee recall returns chunks, while its management API addresses source data.
The small envelope keeps FunAgent metadata with the source document across
Cognee re-indexing and process restarts.
"""
import base64
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
from binascii import Error as Base64Error
from urllib.parse import quote
from uuid import UUID

from ..frame.contracts import ControlError, DomainError, Page
from .common import text, timestamp, validate_content, validate_filters, validate_metadata
from .contracts import MemoryRecord, MemoryRef, UNSET


MARKER = "<!-- funagent-memory-v1:"
NAME = re.compile(r"funagent-[0-9a-f]{64}(?:\.txt)?\Z")


def _server_time(value):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).astimezone(timezone.utc).isoformat() if parsed.tzinfo is None else timestamp(value)
    except (ValueError, TypeError, AttributeError):
        raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee document timestamp") from None


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee data identifier") from None


def _pack(content, metadata):
    header = base64.urlsafe_b64encode(json.dumps(metadata, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf8")).decode("ascii")
    return (content + "\n" + MARKER + header + " -->").encode("utf8")


def _unpack(raw):
    try:
        content, header = raw.decode("utf8").rsplit("\n", 1)
        if not header.startswith(MARKER) or not header.endswith(" -->"):
            raise ValueError
        metadata = json.loads(base64.urlsafe_b64decode(header[len(MARKER):-4]))
        if not isinstance(metadata, dict) or not text(content):
            raise ValueError
        return content, metadata
    except (ValueError, UnicodeError, TypeError, AttributeError, Base64Error):
        raise DomainError("MEMORY_PROTOCOL", "Invalid FunAgent Cognee document") from None


class CogneeDocuments:
    """Mixin: management resolves one public dataset name to a private UUID."""

    def _document_context(self, context, capability):
        context.CheckCancelled()
        if capability not in self.capabilities or self._document_dataset is None:
            raise DomainError("UNSUPPORTED", "Cognee document management is not enabled")
        return replace(context, deadline=min(context.deadline or float("inf"), time.monotonic() + self._timeout))

    def _rows(self, context):
        offset = 0
        while True:
            context.CheckCancelled()
            page = self._http.Request("GET", f"/datasets/{self._document_dataset}/data", context,
                                      params={"limit": 100, "offset": offset}, status_codes={404: "NOT_FOUND"})
            if not isinstance(page, list) or len(page) > 100:
                raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee data page")
            for row in page:
                if not isinstance(row, dict) or not text(row.get("name")):
                    raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee data row")
                if NAME.fullmatch(row["name"]):
                    yield offset, row
                offset += 1
            if len(page) < 100:
                break

    def _raw(self, identifier, context):
        return self._http.Request("GET", f"/datasets/{self._document_dataset}/data/{identifier}/raw", context,
                                  response_mode="bytes", status_codes={404: "NOT_FOUND"})

    def _record(self, row, context):
        identifier = _uuid(row.get("id"))
        if _uuid(row.get("datasetId", row.get("dataset_id"))) != self._document_dataset:
            raise DomainError("MEMORY_PROTOCOL", "Cognee data is outside the requested dataset")
        raw = self._raw(identifier, context)
        content, data = _unpack(raw)
        if data.get("source_id") != self.source_id or data.get("type") != "note" or not text(data.get("write_key")):
            return None
        try:
            validate_content(content, data.get("evidence"), data.get("type"))
            subject, event_at, origin = validate_metadata(data.get("subject"), data.get("event_at"), data.get("origin"))
            created = _server_time(row.get("createdAt", row.get("created_at")))
            updated = _server_time(row.get("updatedAt", row.get("updated_at")))
        except (DomainError, TypeError, KeyError):
            raise DomainError("MEMORY_PROTOCOL", "Invalid FunAgent Cognee document metadata") from None
        citation = (f"cognee://{quote(self.source_id, safe='')}/data/{identifier}"
                    f"?dataset={quote(self._document_dataset_name, safe='')}")
        return MemoryRecord(MemoryRef(self.source_id, identifier), self.kind, data["type"], content,
                            citation, sha256(raw).hexdigest(), tuple(data["evidence"]), False,
                            subject, event_at, created, updated, origin)

    def _find(self, identifier, context):
        identifier = _uuid(identifier)
        for _, row in self._rows(context):
            if _uuid(row.get("id")) == identifier:
                record = self._record(row, context)
                if record is not None:
                    return record
        raise DomainError("NOT_FOUND", "Cognee memory document not found")

    def List(self, query, context):
        if (query.source_id != self.source_id or type(query.limit) is not int or not 1 <= query.limit <= 100 or
                query.type is not None and not text(query.type)):
            raise DomainError("INVALID_REQUEST", "Invalid memory list query")
        validate_filters(query)
        identity = [self._document_dataset_name, self.source_id, query.type, query.subject,
                    timestamp(query.event_from), timestamp(query.event_to), query.origin]
        after = 0
        if query.cursor is not None:
            try:
                saved, after = json.loads(base64.urlsafe_b64decode(query.cursor.encode("ascii")))
                if saved != identity or type(after) is not int or after <= 0:
                    raise ValueError
            except (ValueError, UnicodeError, TypeError, AttributeError, Base64Error):
                raise DomainError("INVALID_REQUEST", "Invalid memory cursor") from None
        child = self._document_context(context, "list")
        try:
            with self._http.Borrow(child):
                records, last = [], after
                for offset, row in self._rows(child):
                    if offset < after:
                        continue
                    record = self._record(row, child)
                    if record is None or not self._matches(record, query):
                        continue
                    if len(records) == query.limit:
                        cursor = base64.urlsafe_b64encode(json.dumps([identity, last]).encode()).decode()
                        return Page(tuple(records), cursor)
                    records.append(record)
                    last = offset + 1
                return Page(tuple(records))
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory list timed out") from None

    @staticmethod
    def _matches(record, query):
        return (getattr(query, "type", None) is None or record.type == query.type) and (
            not getattr(query, "types", ()) or record.type in query.types) and (
            query.subject is None or record.subject == query.subject) and (
            query.origin is None or record.origin == query.origin) and (
            query.event_from is None or record.event_at is not None and record.event_at >= timestamp(query.event_from)) and (
            query.event_to is None or record.event_at is not None and record.event_at <= timestamp(query.event_to))

    def Write(self, draft, context):
        validate_content(draft.content, draft.evidence, draft.type)
        if draft.type != "note":
            raise DomainError("UNSUPPORTED", "Cognee stores only note memories")
        subject, event_at, origin = validate_metadata(draft.subject, draft.event_at, draft.origin)
        if draft.source_id != self.source_id or not text(draft.write_key):
            raise DomainError("INVALID_REQUEST", "Invalid memory write identity")
        metadata = {"source_id": self.source_id, "write_key": draft.write_key, "type": draft.type,
                    "evidence": list(draft.evidence), "subject": subject, "event_at": event_at, "origin": origin}
        raw = _pack(draft.content, metadata)
        if len(raw) > 2 * 1024 * 1024:
            raise DomainError("INVALID_REQUEST", "Cognee memory document is too large")
        name = "funagent-" + sha256((self.source_id + ":" + draft.write_key).encode()).hexdigest() + ".txt"
        child = self._document_context(context, "write")
        try:
            with self._document_lock, self._http.Borrow(child):
                for _, row in self._rows(child):
                    if row["name"].removesuffix(".txt") == name.removesuffix(".txt"):
                        record = self._record(row, child)
                        if record is not None and self._raw(record.ref.record_id, child) == raw:
                            return record
                        raise DomainError("CONFLICT", "Memory write identity already used")
                result = self._http.Request("POST", "/remember", child,
                    data={"datasetId": self._document_dataset, "run_in_background": "false", "self_improvement": "false"},
                    files=[("data", (name, raw, "text/plain"))])
                if not isinstance(result, dict) or result.get("status") != "completed" or (
                        result.get("dataset_id") is not None and _uuid(result["dataset_id"]) != self._document_dataset):
                    raise DomainError("MEMORY_SOURCE", "Cognee document indexing did not complete")
                for _, row in self._rows(child):
                    if row["name"].removesuffix(".txt") == name.removesuffix(".txt"):
                        record = self._record(row, child)
                        if record is not None and self._raw(record.ref.record_id, child) == raw:
                            return record
                raise DomainError("MEMORY_PROTOCOL", "Cognee did not return the written document")
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory write timed out") from None

    def _current(self, ref, expected_version, context):
        if ref.source_id != self.source_id or not text(expected_version):
            raise DomainError("INVALID_REQUEST", "Invalid memory reference or version")
        record = self._find(ref.record_id, context)
        if record.version != expected_version:
            raise DomainError("CONFLICT", "Memory version changed")
        return record

    def Update(self, ref, patch, expected_version, context):
        child = self._document_context(context, "update")
        try:
            with self._document_lock, self._http.Borrow(child):
                old = self._current(ref, expected_version, child)
                category = old.type if patch.type is None else patch.type
                validate_content(patch.content, patch.evidence, category)
                if category != "note":
                    raise DomainError("UNSUPPORTED", "Cognee stores only note memories")
                subject, event_at, origin = validate_metadata(*(getattr(old, key) if getattr(patch, key) is UNSET
                                                                else getattr(patch, key) for key in ("subject", "event_at", "origin")))
                raw = self._raw(old.ref.record_id, child)
                if sha256(raw).hexdigest() != expected_version:
                    raise DomainError("CONFLICT", "Memory version changed")
                _, metadata = _unpack(raw)
                metadata.update(type=category, evidence=list(patch.evidence), subject=subject,
                                event_at=event_at, origin=origin)
                revised = _pack(patch.content, metadata)
                if len(revised) > 2 * 1024 * 1024:
                    raise DomainError("INVALID_REQUEST", "Cognee memory document is too large")
                result = self._http.Request("PATCH", "/update", child,
                    params={"dataset_id": self._document_dataset, "data_id": old.ref.record_id},
                    files=[("data", ("funagent-" + sha256((self.source_id + ":" + metadata["write_key"]).encode()).hexdigest() + ".txt",
                                     revised, "text/plain"))], status_codes={404: "NOT_FOUND"})
                if not isinstance(result, dict) or result.get("status") not in {"incremental", "unchanged", "full_rebuild"} or (
                        result.get("data_id") is not None and _uuid(result["data_id"]) != old.ref.record_id):
                    raise DomainError("MEMORY_SOURCE", "Cognee document update failed")
                record = self._find(old.ref.record_id, child)
                if (record.content != patch.content or record.evidence != tuple(patch.evidence) or
                        record.type != category or (record.subject, record.event_at, record.origin) !=
                        (subject, event_at, origin)):
                    raise DomainError("MEMORY_PROTOCOL", "Cognee document update was not visible")
                return record
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory update timed out") from None

    def Delete(self, ref, expected_version, context):
        child = self._document_context(context, "delete")
        try:
            with self._document_lock, self._http.Borrow(child):
                self._current(ref, expected_version, child)
                self._http.Request("DELETE", f"/datasets/{self._document_dataset}/data/{_uuid(ref.record_id)}",
                                   child, response_mode="bytes", status_codes={404: "NOT_FOUND"})
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory delete timed out") from None
