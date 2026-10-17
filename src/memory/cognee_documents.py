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
from .common import text, timestamp, validate_content, validate_filters, validate_metadata, controlled_lock
from .contracts import MemoryFailure, MemoryIssue, MemoryRecord, MemoryRef, UNSET
from .failure import ErrorIssue


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

    def _completion(self, data_ids, context):
        """Fresh per-operation observations, independent of inspection TTL caches."""
        payload = self._http.Request('GET', f'/datasets/{self._document_dataset}/processing-status',
                                     context, stage='verify')
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise DomainError('MEMORY_PROTOCOL', 'Invalid Cognee completion response')
        states = {}
        for item in payload['items']:
            if not isinstance(item, dict) or type(item.get('completed')) is not bool:
                raise DomainError('MEMORY_PROTOCOL', 'Invalid Cognee completion observation')
            identifier = _uuid(item.get('id'))
            if identifier in states:
                raise DomainError('MEMORY_PROTOCOL', 'Duplicate Cognee completion observation')
            states[identifier] = item['completed']
        return states if data_ids is None else {identifier: states.get(identifier) for identifier in data_ids}

    def _index_failure(self, code, record, *, related=()):
        return MemoryFailure(MemoryIssue(code=code, stage='verify', source_id=self.source_id, effect='known'),
                             ref=record.ref, version=record.version, related=related)

    def _verify_indexed(self, ref, expected_version, context, *, synchronous=False):
        record = self._find(ref.record_id, context)
        if record.version != expected_version:
            raise MemoryFailure(MemoryIssue(code='INDEX_STATE_UNKNOWN', stage='verify', effect='unknown'))
        try:
            completed = self._completion((ref.record_id,), context)[ref.record_id]
        except ControlError:
            raise
        except DomainError as error:
            raise self._index_failure('INDEX_STATE_UNKNOWN', record,
                related=(ErrorIssue(error, source_id=self.source_id, stage='verify'),)) from None
        if completed is False:
            raise self._index_failure('INDEX_NOT_READY', record)
        # Only a fresh synchronous remember receipt binds this observation to raw.
        # The installed upstream has no verified update/idempotent version contract.
        if completed is not True or not synchronous:
            raise self._index_failure('INDEX_STATE_UNKNOWN', record)
        current = self._find(ref.record_id, context)
        if current.version != expected_version:
            raise MemoryFailure(MemoryIssue(code='INDEX_STATE_UNKNOWN', stage='verify', effect='unknown'))
        return current

    def _written(self, name, raw, context):
        found = None
        for _, row in self._rows(context):
            if row['name'].removesuffix('.txt') == name.removesuffix('.txt'):
                if found is not None:
                    raise DomainError('MEMORY_PROTOCOL', 'Duplicate Cognee document identity')
                record = self._record(row, context)
                if record is not None and record.version == sha256(raw).hexdigest():
                    found = record
                else:
                    raise DomainError('CONFLICT', 'Memory write identity already used')
        return found

    def _reconcile(self, error, name, raw, context):
        """Read back once within the original deadline; never resubmit a mutation."""
        context.CheckCancelled()
        try:
            record = self._written(name, raw, context)
            if record is not None:
                try:
                    self._verify_indexed(record.ref, record.version, context)
                except MemoryFailure as verified:
                    raise MemoryFailure(verified.issue, ref=verified.ref, version=verified.version,
                        related=(*verified.related, ErrorIssue(error, source_id=self.source_id, effect='unknown'))[:16]) from None
        except ControlError:
            raise
        except MemoryFailure as verified:
            if verified.issue.effect == 'known':
                raise
        except DomainError:
            pass
        raise error

    def Write(self, draft, context):
        validate_content(draft.content, draft.evidence, draft.type)
        if draft.type != 'note':
            raise DomainError('UNSUPPORTED', 'Cognee stores only note memories')
        subject, event_at, origin = validate_metadata(draft.subject, draft.event_at, draft.origin)
        if draft.source_id != self.source_id or not text(draft.write_key):
            raise DomainError('INVALID_REQUEST', 'Invalid memory write identity')
        raw = _pack(draft.content, dict(source_id=self.source_id, write_key=draft.write_key, type=draft.type,
            evidence=list(draft.evidence), subject=subject, event_at=event_at, origin=origin))
        if len(raw) > 2 * 1024 * 1024:
            raise DomainError('INVALID_REQUEST', 'Cognee memory document is too large')
        name = 'funagent-' + sha256((self.source_id + ':' + draft.write_key).encode()).hexdigest() + '.txt'
        child = self._document_context(context, 'write')
        submitted = False
        try:
            with controlled_lock(self._document_lock, child), self._http.Borrow(child):
                existing = self._written(name, raw, child)
                if existing is not None:
                    return self._verify_indexed(existing.ref, existing.version, child)
                try:
                    result = self._http.Request('POST', '/remember', child, mutation=True, stage='index',
                        data={'datasetId': self._document_dataset, 'run_in_background': 'false', 'self_improvement': 'false'},
                        files=[('data', (name, raw, 'text/plain'))])
                    submitted = True
                except ControlError:
                    raise
                except MemoryFailure as error:
                    if error.issue.effect == 'unknown':
                        self._reconcile(error, name, raw, child)
                    raise
                record = self._written(name, raw, child)
                if record is None:
                    raise MemoryFailure(MemoryIssue(code='MEMORY_PROTOCOL', stage='verify', effect='unknown'))
                valid = (isinstance(result, dict) and result.get('status') == 'completed' and
                         result.get('dataset_id', self._document_dataset) == self._document_dataset and
                         result.get('data_id', record.ref.record_id) == record.ref.record_id)
                return self._verify_indexed(record.ref, record.version, child, synchronous=valid)
        except ControlError:
            raise
        except MemoryFailure as error:
            if submitted and error.issue.effect == 'none':
                raise MemoryFailure(replace(error.issue, effect='unknown'), related=error.related) from None
            raise
        except DomainError as error:
            if submitted:
                raise MemoryFailure(ErrorIssue(error, stage='verify', effect='unknown')) from None
            raise

    def _current(self, ref, expected_version, context):
        if ref.source_id != self.source_id or not text(expected_version):
            raise DomainError("INVALID_REQUEST", "Invalid memory reference or version")
        record = self._find(ref.record_id, context)
        if record.version != expected_version:
            raise DomainError("CONFLICT", "Memory version changed")
        return record

    def Update(self, ref, patch, expected_version, context):
        child = self._document_context(context, 'update')
        submitted = False
        try:
            with controlled_lock(self._document_lock, child), self._http.Borrow(child):
                old = self._current(ref, expected_version, child)
                category = old.type if patch.type is None else patch.type
                validate_content(patch.content, patch.evidence, category)
                if category != 'note':
                    raise DomainError('UNSUPPORTED', 'Cognee stores only note memories')
                subject, event_at, origin = validate_metadata(*(getattr(old, key) if getattr(patch, key) is UNSET
                    else getattr(patch, key) for key in ('subject', 'event_at', 'origin')))
                raw = self._raw(old.ref.record_id, child)
                if sha256(raw).hexdigest() != expected_version:
                    raise DomainError('CONFLICT', 'Memory version changed')
                _, metadata = _unpack(raw)
                metadata.update(type=category, evidence=list(patch.evidence), subject=subject, event_at=event_at, origin=origin)
                revised = _pack(patch.content, metadata)
                if len(revised) > 2 * 1024 * 1024:
                    raise DomainError('INVALID_REQUEST', 'Cognee memory document is too large')
                name = 'funagent-' + sha256((self.source_id + ':' + metadata['write_key']).encode()).hexdigest() + '.txt'
                try:
                    result = self._http.Request('PATCH', '/update', child, mutation=True, stage='index',
                        params={'dataset_id': self._document_dataset, 'data_id': old.ref.record_id},
                        files=[('data', (name, revised, 'text/plain'))], status_codes={404: 'NOT_FOUND'})
                    submitted = True
                except ControlError:
                    raise
                except MemoryFailure as error:
                    if error.issue.effect == 'unknown':
                        self._reconcile(error, name, revised, child)
                    raise
                record = self._find(ref.record_id, child)
                if record.version != sha256(revised).hexdigest():
                    raise MemoryFailure(MemoryIssue(code='INDEX_STATE_UNKNOWN', stage='verify', effect='unknown'))
                if (not isinstance(result, dict) or result.get('data_id', ref.record_id) != ref.record_id or
                        result.get('dataset_id', self._document_dataset) != self._document_dataset):
                    raise self._index_failure('INDEX_STATE_UNKNOWN', record,
                        related=(MemoryIssue(code='MEMORY_PROTOCOL', stage='index', effect='known'),))
                return self._verify_indexed(record.ref, record.version, child)
        except ControlError:
            raise
        except MemoryFailure as error:
            if submitted and error.issue.effect == 'none':
                raise MemoryFailure(replace(error.issue, effect='unknown'), related=error.related) from None
            raise
        except DomainError as error:
            if submitted:
                raise MemoryFailure(ErrorIssue(error, stage='verify', effect='unknown')) from None
            raise

    def Delete(self, ref, expected_version, context):
        child = self._document_context(context, "delete")
        try:
            with controlled_lock(self._document_lock, child), self._http.Borrow(child):
                self._current(ref, expected_version, child)
                self._http.Request("DELETE", f"/datasets/{self._document_dataset}/data/{_uuid(ref.record_id)}",
                                   child, mutation=True, response_mode="bytes", status_codes={404: "NOT_FOUND"})
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory delete timed out") from None
