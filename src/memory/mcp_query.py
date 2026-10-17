"""A fixed read-only knowledge query borrows one startup-owned MCP port."""
from dataclasses import replace
import json
import time
from math import isfinite
from urllib.parse import quote, urlencode

from ..frame.contracts import ControlError, DomainError
from ..tool.contracts import BackendResult
from ..tool.protocols import KnowledgeBackend
from ..frame.tool_validator import ToolValidator
from .contracts import MemoryFailure, MemoryIssue, MemoryQueryBatch
from .failure import ReadIssues


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
        except ControlError as error:
            if error.code != 'DEADLINE':
                raise
            context.CheckCancelled()
            raise DomainError("MEMORY_TIMEOUT", "Knowledge query timed out") from None
        if not isinstance(result, BackendResult) or result.status not in {"ok", "error"}:
            raise DomainError("MEMORY_SOURCE", "Knowledge MCP query failed")
        try:
            if not isinstance(result.content, str) or len(result.content.encode('utf8')) > 1048576:
                raise ValueError
            wire = json.loads(result.content)
            if not isinstance(wire, dict):
                raise ValueError
            if result.status == 'error' and 'structuredContent' not in wire:
                code = 'MEMORY_TIMEOUT' if wire.get('code') == 'MCP_TIMEOUT' else 'MEMORY_SOURCE'
                raise DomainError(code, 'Knowledge MCP query failed')
            data = wire["structuredContent"]
            if (not isinstance(data, dict)
                    or type(data.get("version")) is not int or data["version"] not in {1, 2}
                    or data.get("collection") != self._collection
                    or type(data.get("result_count")) is not int
                    or not isinstance(data.get("results"), list)
                    or not 0 <= data["result_count"] <= self._top_k
                    or data["result_count"] != len(data["results"])
                    or not isinstance(data.get("metadata", {}), dict)):
                raise ValueError
            status, issues = "ok", ()
            if data['version'] == 1:
                if wire.get('isError', False) is not False or result.status != 'ok' or data.get('metadata', {}).get('error'):
                    raise ValueError
                status = 'partial'
                issues = (MemoryIssue(code='RETRIEVAL_UNVERIFIED', stage='search'),)
            else:
                retrieval = data['retrieval']
                if not isinstance(retrieval, dict) or set(retrieval) != {'status', 'paths', 'issues'}:
                    raise ValueError
                state, paths = retrieval['status'], retrieval['paths']
                if (not isinstance(state, str) or state not in {'ready', 'empty', 'partial', 'failed'} or
                        not isinstance(paths, dict) or set(paths) != {'dense', 'sparse'} or
                        any(not isinstance(v, str) or v not in {'ok', 'failed', 'skipped'} for v in paths.values())):
                    raise ValueError
                issues = ReadIssues(retrieval['issues'])
                if (state == 'ready' and not data['results'] or state in {'empty', 'failed'} and data['results'] or
                        state in {'ready', 'empty'} and (issues or 'failed' in paths.values()) or
                        state != 'failed' and 'ok' not in paths.values() or
                        state in {'partial', 'failed'} and not issues or
                        type(wire.get('isError', False)) is not bool or
                        wire.get('isError', False) != (state == 'failed') or
                        result.status == 'error' and state != 'failed' or
                        state != 'failed' and data.get('metadata', {}).get('error')):
                    raise ValueError
                if state == 'failed':
                    raise MemoryFailure(issues[0], related=issues[1:])
                status = 'partial' if state == 'partial' else 'ok'
            rows, seen = [], set()
            for row in data["results"]:
                if (not isinstance(row, dict) or any(not isinstance(row.get(f), str) or not row[f].strip()
                    for f in ("chunk_id", "text", "source")) or row["chunk_id"] in seen
                        or len(row["text"]) > 500000 or len(row["chunk_id"]) > 512 or len(row['source']) > 512
                        or 'score' in row and (type(row['score']) not in {int, float} or not isfinite(row['score']))
                        or row.get("page") is not None and type(row["page"]) not in {int, str}):
                    raise ValueError
                seen.add(row["chunk_id"])
                citation = "knowledge://" + quote(self._collection, safe="") + "/" + quote(row["chunk_id"], safe="")
                citation += "?" + urlencode({"source": row["source"], **({"page": row["page"]} if row.get("page") is not None else {})})
                rows.append({"record_id": row["chunk_id"], "content": row["text"],
                             "type": "knowledge", "citation": citation, "origin": row["source"]})
            return MemoryQueryBatch(rows=rows, status=status, issues=issues)
        except (ValueError, TypeError, KeyError):
            raise DomainError("MEMORY_PROTOCOL", "Invalid knowledge evidence") from None
