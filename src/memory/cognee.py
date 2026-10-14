"""Cognee chunk recall plus opt-in document management over HTTP."""
from collections.abc import Mapping
from dataclasses import replace
from threading import RLock
import os
import re
import time
from urllib.parse import quote, urlencode
from uuid import UUID

from ..frame.contracts import ControlError, DomainError
from .common import text, validate_query
from .contracts import MemoryRecord, MemoryRef, MemorySearchResult
from .transport import JSONHTTP, ValidateConnection
from .cognee_documents import CogneeDocuments, VisibleText
from .cognee_datasets import CogneeDataset


SEARCH_TYPES = frozenset({"CHUNKS", "CHUNKS_LEXICAL"})


def ValidateSettings(settings):
    """Shared by configuration loading and standalone construction; no IO."""
    allowed = {"store", "kind", "capabilities", "read_access", "write_access", "source_description",
               "endpoint", "datasets", "search_type", "credential_env", "auth_header", "timeout"}
    capabilities = settings.get("capabilities", ("search",)) if isinstance(settings, Mapping) else ()
    valid = (isinstance(settings, Mapping) and set(settings) <= allowed and
             settings.get("kind", "semantic") == "semantic" and
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
    if (not isinstance(selected, (list, tuple)) or not 1 <= len(selected) <= 100 or
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
        self.source_id, self.kind = source_id, "semantic"
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
        self._document_dataset = registered.dataset_id if managed and registered is not None else None
        self._document_dataset_name = registered.dataset_name if managed and registered is not None else None
        scoped_id = _dataset_id or self._document_dataset
        self._scope = ({"dataset_ids": (scoped_id,)} if scoped_id is not None
                       else {"datasets": self._dataset_names})
        self._scope_dataset_ids = frozenset(self._scope.get("dataset_ids", ()))
        self._document_lock = RLock()
        self._search_type, self._timeout = settings.get("search_type", "CHUNKS"), settings.get("timeout", 2)
        self._http = JSONHTTP(settings["endpoint"].rstrip("/"), Headers(settings), self._timeout, transport=transport)

    def Search(self, query, context):
        context.CheckCancelled()
        validate_query(query)
        if (query.source_ids and self.source_id not in query.source_ids) or (query.kind and query.kind != self.kind):
            return MemorySearchResult()
        if (not query.text.strip() or query.include_deleted or any(getattr(query, key) is not None
                for key in ("subject", "origin", "event_from", "event_to"))):
            raise DomainError("UNSUPPORTED", "Cognee supports text search without record metadata filters")
        if query.types and "knowledge" not in query.types:
            return MemorySearchResult((), {self.source_id: "ok"})
        child = replace(context, deadline=min(context.deadline or float("inf"), time.monotonic() + self._timeout))
        try:
            with self._http.Borrow(child):
                payload = self._http.Post("/recall", {"query": query.text, "search_type": self._search_type,
                    "scope": ["graph"], "top_k": query.limit, "only_context": False, "stream": False,
                    **{key: list(value) for key, value in self._scope.items()}}, child)
                records = self._records(payload, query.limit)
                child.CheckCancelled()
                return MemorySearchResult(records, {self.source_id: "ok"})
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Cognee memory search timed out") from None

    def _records(self, payload, limit):
        if not isinstance(payload, list) or len(payload) > limit:
            raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee recall response")
        records, seen = [], set()
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
                try:
                    dataset_id = str(UUID(dataset_id))
                except (TypeError, ValueError, AttributeError):
                    raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee evidence dataset_id") from None
                if self._scope_dataset_ids and dataset_id not in self._scope_dataset_ids:
                    raise DomainError("MEMORY_PROTOCOL", "Cognee evidence is outside the requested scope")
            if dataset_name is not None and (not text(dataset_name) or dataset_name not in self._dataset_names):
                raise DomainError("MEMORY_PROTOCOL", "Cognee evidence is outside the requested scope")
            identifier = metadata["chunk_id"]
            if identifier in seen:
                raise DomainError("MEMORY_PROTOCOL", "Duplicate Cognee evidence")
            seen.add(identifier)
            origin = metadata.get("document_name")
            if origin is not None and (not text(origin) or len(origin) > 512):
                raise DomainError("MEMORY_PROTOCOL", "Invalid Cognee source document name")
            locators = {"data_id": metadata["data_id"], "datasets": self._dataset_names}
            citation = "cognee://" + quote(self.source_id, safe="") + "/" + quote(identifier, safe="") + "?" + urlencode(locators, doseq=True)
            records.append(MemoryRecord(MemoryRef(self.source_id, identifier), self.kind, "knowledge", VisibleText(row["text"]),
                                        citation, origin=origin))
        return tuple(records)

    def Close(self):
        self._http.Close()
