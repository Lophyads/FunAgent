"""Episodic notes over Cognee's document and chunk-recall APIs."""
from collections.abc import Mapping
from threading import RLock
import os
import re
from uuid import UUID

from ..frame.contracts import ControlError, DomainError
from .common import text, validate_query
from .contracts import MemorySearchResult
from .transport import JSONHTTP, ValidateConnection
from .cognee_documents import CogneeDocuments, _uuid
from .cognee_datasets import CogneeDataset


SEARCH_TYPES = frozenset({"CHUNKS", "CHUNKS_LEXICAL"})


def ValidateSettings(settings):
    """Shared by configuration loading and standalone construction; no IO."""
    allowed = {"store", "kind", "capabilities", "read_access", "write_access", "source_description",
               "endpoint", "datasets", "search_type", "credential_env", "auth_header", "timeout"}
    capabilities = settings.get("capabilities", ("search",)) if isinstance(settings, Mapping) else ()
    valid = (isinstance(settings, Mapping) and set(settings) <= allowed and
             settings.get("kind", "episodic") == "episodic" and
             isinstance(capabilities, (list, tuple)) and all(isinstance(value, str) for value in capabilities) and
             len(capabilities) == len(set(capabilities)) and
             "search" in capabilities and set(capabilities) <= {"search", "list", "write", "update", "delete"} and
             (not set(capabilities) & {"write", "update", "delete"} or "list" in capabilities) and
             isinstance(settings.get("search_type", "CHUNKS"), str) and
             settings.get("search_type", "CHUNKS") in SEARCH_TYPES and
             isinstance(settings.get("auth_header", "Authorization"), str) and
             settings.get("auth_header", "Authorization") in {"Authorization", "X-Api-Key"})
    if not valid:
        raise DomainError("CONFIG_INVALID", "Invalid Cognee memory settings")
    ValidateConnection(settings.get("endpoint"), settings.get("timeout", 2))
    selected = settings.get("datasets")
    if (not isinstance(selected, (list, tuple)) or len(selected) != 1 or
            any(not text(value) or len(value) > 512 for value in selected) or len(set(selected)) != len(selected)):
        raise DomainError("CONFIG_INVALID", "Invalid Cognee dataset scope")
    if set(capabilities) - {"search"} and (len(selected) != 1 or CogneeDataset.Find(selected[0]) is None):
        raise DomainError("CONFIG_REFERENCE", "Cognee document management requires one registered dataset name")
    if "credential_env" in settings:
        name = settings["credential_env"]
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise DomainError("CONFIG_INVALID", "Invalid Cognee credential environment variable")
    elif "auth_header" in settings:
        raise DomainError("CONFIG_INVALID", "Cognee auth_header requires credential_env")


def Headers(settings):
    headers = {"Accept": "application/json"}
    if "credential_env" in settings:
        token = os.environ.get(settings["credential_env"])
        if not text(token) or any(char.isspace() for char in token):
            raise DomainError("CONFIG_CREDENTIAL", "Cognee memory credential is missing or invalid")
        header = settings.get("auth_header", "Authorization")
        headers[header] = "Bearer " + token if header == "Authorization" else token
    return headers


class CogneeMemoryStore(CogneeDocuments):
    capabilities = frozenset({"search", "list", "write", "update", "delete"})

    def __init__(self, settings, source_id, *, transport=None, _dataset_id=None):
        ValidateSettings(settings)
        if not text(source_id):
            raise DomainError("CONFIG_INVALID", "Invalid Cognee memory source")
        self.source_id, self.kind = source_id, "episodic"
        self.capabilities = frozenset(settings.get("capabilities", ("search",)))
        self._dataset_names = tuple(settings["datasets"])
        registered = CogneeDataset.Find(self._dataset_names[0]) if len(self._dataset_names) == 1 else None
        managed = bool(self.capabilities - {"search"})
        if _dataset_id is not None:
            if managed:
                raise DomainError("CONFIG_INVALID", "Internal Cognee dataset scope is read-only")
            try:
                _dataset_id = str(UUID(_dataset_id))
            except (TypeError, ValueError, AttributeError):
                raise DomainError("CONFIG_INVALID", "Invalid internal Cognee dataset identifier") from None
        self._document_dataset = _dataset_id or (registered.dataset_id if registered is not None else None)
        if self._document_dataset is None:
            raise DomainError("CONFIG_REFERENCE", "Cognee notes require a registered dataset name")
        self._document_dataset_name = self._dataset_names[0]
        self._document_lock = RLock()
        self._search_type, self._timeout = settings.get("search_type", "CHUNKS"), settings.get("timeout", 2)
        self._http = JSONHTTP(settings["endpoint"].rstrip("/"), Headers(settings), self._timeout, transport=transport)

    def Search(self, query, context):
        context.CheckCancelled()
        validate_query(query)
        if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
            return MemorySearchResult()
        if query.types and "note" not in query.types:
            return MemorySearchResult((), {self.source_id: "ok"})
        if query.include_deleted:
            raise DomainError("UNSUPPORTED", "Cognee physically deletes notes; deleted receipts are unavailable")
        child = self._document_context(context, "search")
        try:
            with self._document_lock, self._http.Borrow(child):
                candidates = None
                if query.text.strip():
                    payload = self._http.Post("/recall", {"query": query.text, "search_type": self._search_type,
                        "scope": ["graph"], "top_k": 100, "only_context": False, "stream": False,
                        "dataset_ids": [self._document_dataset]}, child)
                    candidates = self._candidates(payload)
                    if not candidates:
                        return MemorySearchResult((), {self.source_id: "ok"})
                rows = self._rows(child)
                if candidates is not None:
                    by_id = {_uuid(row.get("id")): row for _, row in rows}
                    rows = ((0, by_id[identifier]) for identifier in candidates if identifier in by_id)
                records = []
                for _, row in rows:
                    record = self._record(row, child)
                    if record is not None and self._matches(record, query):
                        records.append(record)
                        if len(records) == query.limit:
                            break
                child.CheckCancelled()
                return MemorySearchResult(tuple(records), {self.source_id: "ok"})
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory search timed out") from None

    def _candidates(self, payload):
        if not isinstance(payload, list) or len(payload) > 100:
            raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee recall response")
        identifiers, seen = [], set()
        for row in payload:
            if isinstance(row, dict) and row.get("source") == "system":
                if row.get("status") == "memory_warming_up":
                    raise DomainError("NOT_READY", "Cognee dataset is not indexed")
                if row.get("status") == "build_failed":
                    raise DomainError("MEMORY_SOURCE", "Cognee dataset indexing failed")
            if (not isinstance(row, dict) or row.get("source") != "graph" or row.get("search_type") != self._search_type or
                    not text(row.get("text")) or len(row["text"]) > 65536 or row.get("system_prompt") is not None or
                    not isinstance(row.get("metadata"), dict)):
                raise DomainError("MEMORY_PROTOCOL", "Cognee must return source chunks, not generated answers")
            metadata = row["metadata"]
            if any(not text(metadata.get(key)) or len(metadata[key]) > 512 for key in ("chunk_id", "data_id")):
                raise DomainError("MEMORY_PROTOCOL", "Cognee evidence requires chunk_id and data_id")
            dataset_id, dataset_name = row.get("dataset_id"), row.get("dataset_name")
            if dataset_id is not None:
                dataset_id = _uuid(dataset_id)
                if dataset_id != self._document_dataset:
                    raise DomainError("MEMORY_PROTOCOL", "Cognee evidence is outside the requested scope")
            if dataset_name is not None and (not text(dataset_name) or dataset_name not in self._dataset_names):
                raise DomainError("MEMORY_PROTOCOL", "Cognee evidence is outside the requested scope")
            identifier = metadata["chunk_id"]
            if identifier in seen:
                raise DomainError("MEMORY_PROTOCOL", "Duplicate Cognee evidence")
            seen.add(identifier)
            identifiers.append(_uuid(metadata["data_id"]))
        return tuple(dict.fromkeys(identifiers))

    def Close(self):
        self._http.Close()
