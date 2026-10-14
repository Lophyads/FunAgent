"""Eval owns ingestion; retrieval uses the application's read-only driver."""
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from src.frame.contracts import DomainError
from src.memory.cognee import CogneeMemoryStore, Headers, ValidateSettings
from src.memory.contracts import MemoryQuery
from src.memory.transport import JSONHTTP
from .dataset import Encode


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError("EVAL_PROTOCOL", "Cognee returned an invalid identifier") from None


class CogneeBackend:
    def __init__(self, settings, space, *, transport=None):
        ValidateSettings(settings | {"datasets": [space]})
        if not space.startswith("eval_"):
            raise DomainError("EVAL_SCOPE", "Cognee evaluation requires an eval_ dataset")
        self.settings, self.space, self.transport = settings, space, transport
        self.receipts, self._mapping, self._items = [], {}, set()
        self.dataset_id, self.store = None, None
        self._http = JSONHTTP(settings["endpoint"].rstrip("/"), Headers(settings),
                              settings.get("timeout", 30), transport=transport,
                              protocol="EVAL_PROTOCOL", source="EVAL_SOURCE")

    def Ingest(self, items, context):
        if self.dataset_id is not None:
            raise DomainError("EVAL_NOT_EMPTY", "Cognee backend already ingested")
        dataset = self._http.Post("/datasets", {"name": self.space}, context)
        if not isinstance(dataset, dict) or dataset.get("name") != self.space:
            raise DomainError("EVAL_PROTOCOL", "Invalid Cognee evaluation dataset")
        self.dataset_id = _uuid(dataset.get("id"))
        # Save the locator before uploading, including on partially failed runs.
        self.receipts.append({"dataset_id": self.dataset_id, "dataset_name": self.space})
        existing = self._http.Request("GET", f"/datasets/{self.dataset_id}/data", context,
                                      params={"limit": 1})
        if not isinstance(existing, list):
            raise DomainError("EVAL_PROTOCOL", "Invalid Cognee dataset data")
        if existing:
            raise DomainError("EVAL_NOT_EMPTY", "Cognee evaluation dataset is not empty")
        self._items = {item["id"] for item in items}
        result = self._http.Request("POST", "/remember", context,
            data={"datasetId": self.dataset_id, "run_in_background": "true", "self_improvement": "false",
                  "external_metadata": Encode([{"eval_item_id": i["id"]} for i in items])},
            files=[("data", (i["id"] + ".txt", i["text"].encode("utf8"), "text/plain")) for i in items])
        if not isinstance(result, dict) or result.get("status") not in {"running", "completed", "errored"}:
            raise DomainError("EVAL_PROTOCOL", "Invalid Cognee ingestion response")
        if result.get("dataset_id") is not None and _uuid(result["dataset_id"]) != self.dataset_id:
            raise DomainError("EVAL_PROTOCOL", "Cognee ingestion used another dataset")
        pipeline_id = result.get("pipeline_run_id")
        self.receipts.append({"status": result["status"],
                              "pipeline_run_id": _uuid(pipeline_id) if pipeline_id is not None else None})
        if result["status"] == "errored":
            raise DomainError("INDEX_FAILED", "Cognee ingestion failed")
        return self.receipts

    def WaitReady(self, context):
        if self.dataset_id is None:
            raise DomainError("NOT_READY", "Cognee evaluation has not ingested")
        while True:
            context.CheckCancelled()
            result = self._http.Request("GET", "/datasets/status", context,
                params={"dataset": self.dataset_id, "pipeline": "cognify_pipeline"})
            if not isinstance(result, dict):
                raise DomainError("EVAL_PROTOCOL", "Invalid Cognee indexing status")
            status = result.get(self.dataset_id)
            if status == "DATASET_PROCESSING_COMPLETED":
                break
            if status == "DATASET_PROCESSING_ERRORED":
                raise DomainError("INDEX_FAILED", "Cognee dataset indexing failed")
            if status not in {None, "DATASET_PROCESSING_INITIATED", "DATASET_PROCESSING_STARTED"}:
                raise DomainError("EVAL_PROTOCOL", "Unknown Cognee indexing status")
            context.cancel.wait(min(2, context.Remaining()))
        offset, seen = 0, set()
        while True:
            rows = self._http.Request("GET", f"/datasets/{self.dataset_id}/data", context,
                                      params={"limit": 1000, "offset": offset})
            if not isinstance(rows, list) or len(rows) > 1000:
                raise DomainError("EVAL_PROTOCOL", "Invalid Cognee data listing")
            for row in rows:
                if not isinstance(row, dict):
                    raise DomainError("EVAL_LINEAGE", "Missing Cognee source metadata")
                metadata = row.get("externalMetadata", row.get("external_metadata"))
                item_id = metadata.get("eval_item_id") if isinstance(metadata, dict) else None
                data_id = _uuid(row.get("id"))
                scope = row.get("datasetId", row.get("dataset_id"))
                if (item_id not in self._items or item_id in seen or data_id in self._mapping or
                        scope is not None and _uuid(scope) != self.dataset_id):
                    raise DomainError("EVAL_LINEAGE", "Cognee source mapping is incomplete or ambiguous")
                seen.add(item_id)
                self._mapping[data_id] = item_id
                self.receipts.append({"item_id": item_id, "data_id": data_id, "dataset_id": self.dataset_id})
            offset += len(rows)
            if len(rows) < 1000:
                break
        if seen != self._items:
            raise DomainError("EVAL_LINEAGE", "Cognee did not ingest every evaluation item")
        self.store = CogneeMemoryStore(self.settings | {"dataset_ids": [self.dataset_id]}, "eval",
                                       transport=self.transport)

    def Search(self, text, limit, context):
        if self.store is None:
            raise DomainError("NOT_READY", "Cognee evaluation index is not ready")
        result = self.store.Search(MemoryQuery(text=text, source_ids=("eval",), limit=limit), context)
        evidence = []
        for record in result.records:
            data_ids = parse_qs(urlsplit(record.citation).query).get("data_id", [])
            item = self._mapping.get(_uuid(data_ids[0])) if len(data_ids) == 1 else None
            evidence.append({"record_id": record.ref.record_id, "item_ids": [item] if item else [],
                             "text": record.content, "citation": record.citation,
                             "unmapped": item is None, "truncated": record.truncated})
        return {"evidence": evidence, "raw": [asdict(r) for r in result.records], "warnings": list(result.warnings)}

    def Close(self):
        try:
            if self.store:
                self.store.Close()
        finally:
            self._http.Close()
