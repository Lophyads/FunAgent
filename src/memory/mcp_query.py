"""A fixed read-only knowledge query borrows one startup-owned MCP port."""
from collections.abc import Mapping
from dataclasses import replace
import json
import time
from urllib.parse import quote, urlencode

from ..frame.contracts import ControlError, DomainError
from ..tool.contracts import BackendResult
from ..tool.protocols import KnowledgeBackend
from ..frame.schema import ToolValidator


class MCPMemoryQuery:
    def __init__(self, settings, backend: KnowledgeBackend):
        self._backend = backend
        self._collection = settings["collection"]
        self._top_k = settings.get("top_k", 5)
        self._timeout = settings.get("timeout", 2)
        spec = backend.ToolSpec("query_knowledge_hub")
        if spec is None or spec.decision.outcome != "allow" or spec.decision.effect != "none":
            raise DomainError("CONFIG_INVALID", "Knowledge MCP requires an included read-only query")
        if not ToolValidator(spec.parameters).is_valid({"query": "probe", "collection": self._collection, "top_k": self._top_k}):
            raise DomainError("CONFIG_INVALID", "Knowledge MCP query schema is incompatible")

    def __call__(self, query, context):
        context.CheckCancelled()
        deadline = time.monotonic() + self._timeout
        child = replace(context, deadline=min(context.deadline, deadline) if context.deadline is not None else deadline)
        try:
            result = self._backend.Execute("query_knowledge_hub", {
                "query": query.text, "collection": self._collection, "top_k": self._top_k}, child)
            child.CheckCancelled()
        except ControlError:
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Knowledge query timed out") from None
        if not isinstance(result, BackendResult) or result.status != "ok":
            context.CheckCancelled()
            if isinstance(result, BackendResult):
                try:
                    if json.loads(result.content).get("code") == "MCP_TIMEOUT":
                        raise DomainError("MEMORY_TIMEOUT", "Knowledge query timed out")
                except (ValueError, AttributeError):
                    pass
            raise DomainError("MEMORY_SOURCE", "Knowledge MCP query failed")
        try:
            wire = json.loads(result.content)
            data = wire["structuredContent"]
            if (wire.get("isError", False) is not False or not isinstance(data, dict)
                    or type(data.get("version")) is not int or data["version"] != 1
                    or data.get("collection") != self._collection
                    or type(data.get("result_count")) is not int
                    or not isinstance(data.get("results"), list)
                    or not 0 <= data["result_count"] <= self._top_k
                    or data["result_count"] != len(data["results"])
                    or not isinstance(data.get("metadata", {}), dict) or data.get("metadata", {}).get("error")):
                raise ValueError
            rows, seen = [], set()
            for row in data["results"]:
                if (not isinstance(row, dict) or any(not isinstance(row.get(f), str) or not row[f].strip()
                    for f in ("chunk_id", "text", "source")) or row["chunk_id"] in seen
                        or len(row["text"]) > 500000 or len(row["chunk_id"]) > 512
                        or row.get("page") is not None and type(row["page"]) not in {int, str}):
                    raise ValueError
                seen.add(row["chunk_id"])
                citation = "knowledge://" + quote(self._collection, safe="") + "/" + quote(row["chunk_id"], safe="")
                citation += "?" + urlencode({"source": row["source"], **({"page": row["page"]} if row.get("page") is not None else {})})
                rows.append({"record_id": row["chunk_id"], "content": row["text"],
                             "type": "knowledge", "citation": citation, "origin": row["source"]})
            return rows
        except (ValueError, TypeError, KeyError):
            raise DomainError("MEMORY_PROTOCOL", "Invalid knowledge evidence") from None
