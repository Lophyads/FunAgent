"""Read-only MCP binding and strict decoding of full evidence or legacy snippets."""

from __future__ import annotations

import json
import re
import uuid
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from agentcore.interfaces import ToolInvocation, TurnCancelled
from agentcore.storage import LeaseLostError
from agentcore.turn_state import RunBudgetExceeded

from .models import KnowledgeItem, KnowledgePhaseTimeout, KnowledgeRetrievalError

if TYPE_CHECKING:
    from agentcore.interfaces import ToolGuard
    from agentcore.models import KnowledgeMcpConfig
    from agentcore.turn_state import TurnRuntime
    from tools.registry import ToolRegistry
    from tools.services import ToolServices
    from .models import KnowledgeRequest


def _field(value: Any, name: str, alias: str | None = None, default=None):
    if isinstance(value, dict):
        return value.get(name, value.get(alias, default) if alias else default)
    return getattr(value, name, getattr(value, alias, default) if alias else default)


def decode_result(result: Any, collection: str) -> tuple[KnowledgeItem, ...]:
    if _field(result, "is_error", "isError", False):
        raise KnowledgeRetrievalError("mcp_error")
    structured = _field(result, "structured_content", "structuredContent")
    snippet_only = False
    if structured is not None:
        if (not isinstance(structured, dict) or type(structured.get("version")) is not int or
                structured.get("version") != 1):
            raise KnowledgeRetrievalError("result_protocol")
        payload = structured
        rows = payload.get("results")
    else:
        payload = None
        for block in _field(result, "content", default=()) or ():
            text = _field(block, "text", default="")
            marker = re.fullmatch(
                r"\s*(?:---\s*)?(?:\*\*)?References \(JSON\):?(?:\*\*)?\s*\n([\s\S]*)", text,
            ) if isinstance(text, str) else None
            if marker is None:
                continue
            body = marker.group(1)
            wrapped = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```\s*", body)
            if wrapped:
                body = wrapped.group(1)
            try:
                payload = json.loads(body)
            except (ValueError, TypeError):
                raise KnowledgeRetrievalError("result_protocol") from None
            break
        if not isinstance(payload, dict):
            raise KnowledgeRetrievalError("result_protocol")
        rows = payload.get("citations")
        snippet_only = True
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict) or metadata.get("error"):
        raise KnowledgeRetrievalError("result_protocol")
    result_count = payload.get("result_count", metadata.get("result_count"))
    if type(result_count) is not int or result_count < 0 or not isinstance(rows, list):
        raise KnowledgeRetrievalError("result_protocol")
    result_collection = payload.get("collection", metadata.get("collection", collection))
    if result_collection != collection:
        raise KnowledgeRetrievalError("collection_mismatch")
    if result_count == 0:
        if rows:
            raise KnowledgeRetrievalError("result_protocol")
        return ()
    if not rows:
        raise KnowledgeRetrievalError("result_protocol")
    if len(rows) != result_count:
        raise KnowledgeRetrievalError("result_protocol")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            raise KnowledgeRetrievalError("result_protocol")
        content = row.get("text_snippet" if snippet_only else "text")
        chunk_id = row.get("chunk_id", "")
        row_metadata = row.get("metadata", {})
        if not isinstance(row_metadata, dict):
            raise KnowledgeRetrievalError("result_protocol")
        source, title, page = row.get("source", ""), row.get("title", row_metadata.get("title", "")), row.get("page")
        if (not isinstance(content, str) or not content.strip() or
                not all(isinstance(value, str) for value in (chunk_id, source, title)) or
                (page is not None and type(page) not in {int, str})):
            raise KnowledgeRetrievalError("result_protocol")
        items.append(KnowledgeItem(chunk_id, collection, content, source, title, page, snippet_only))
    return tuple(items)


class McpKnowledgeRetriever:
    def __init__(self, *, config: KnowledgeMcpConfig, registry: ToolRegistry,
                 services: ToolServices, guard: ToolGuard) -> None:
        # MCP is optional; importing knowledge alone must not import its SDK.
        self.config = config
        self.registry = registry
        self.guard = guard
        server = services.config.tools.mcp_servers.get(config.server)
        if server is None or not server.enabled:
            raise ValueError("knowledge MCP server must exist and be enabled")
        if server.requires_approval:
            raise ValueError("automatic knowledge retrieval requires requires_approval=false")
        if ((server.include and not any(fnmatchcase(config.tool, p) for p in server.include)) or
                any(fnmatchcase(config.tool, p) for p in server.exclude)):
            raise ValueError("knowledge MCP tool is excluded by its server policy")
        # This config explicitly binds a read-only query. It does not grant the
        # planner a tool selector or expose the remote handler to the main model.
        if config.tool != "query_knowledge_hub":
            raise ValueError("built-in knowledge binding only supports read-only query_knowledge_hub")
        from tools.mcp_adapter import _tool_name

        self.tool_name = _tool_name(config.server, config.tool)
        if registry.get_entry(self.tool_name) is None:
            raise ValueError("knowledge MCP tool has not been discovered and registered")
        self.hidden_model_tools = frozenset(
            _tool_name(config.server, name) for name in {*config.hidden_tools, config.tool}
        )

    def search(self, query: str, *, request: KnowledgeRequest, top_k: int,
               runtime: TurnRuntime, check_active) -> tuple[KnowledgeItem, ...]:
        check_active()
        entry = self.registry.get_entry(self.tool_name)
        if (entry is None or entry.requires_approval or
                not entry.required_permissions.issubset(request.permissions) or
                not entry.required_capabilities.issubset(request.capabilities)):
            raise KnowledgeRetrievalError("access_denied")
        client = runtime.services.mcp()
        if not client.is_available(self.config.server):
            raise KnowledgeRetrievalError("unavailable")
        try:
            arguments = entry.args_model.model_validate({
                "query": query, "top_k": top_k, "collection": self.config.collection,
            }).model_dump(mode="json")
        except ValidationError:
            raise KnowledgeRetrievalError("invalid_arguments") from None
        try:
            decision = runtime.call_operation(
                lambda: self.guard.check(ToolInvocation(
                    session_id=request.session_id, turn_id=request.turn_id,
                    tool_call_id=f"knowledge-{uuid.uuid4().hex}", tool_name=self.tool_name,
                    arguments=arguments, requires_approval=False,
                )),
                timeout=min(runtime.config.timeouts.policy_seconds, runtime.config.knowledge.timeout_seconds),
                cancel_event=runtime.cancel_event, deadline=runtime.deadline,
                check_active=check_active,
            )
        except (TurnCancelled, RunBudgetExceeded, LeaseLostError, KnowledgePhaseTimeout):
            raise
        except TimeoutError:
            check_active()
            raise KnowledgeRetrievalError("guard_timeout") from None
        check_active()
        if not decision.allowed:
            raise KnowledgeRetrievalError("guard_denied")
        try:
            result = client.call_tool(
                self.config.server, self.config.tool, arguments, check_active=check_active,
            )
        except (TurnCancelled, RunBudgetExceeded, LeaseLostError, KnowledgePhaseTimeout):
            raise
        except (RuntimeError, OSError, TimeoutError) as exc:
            check_active()
            raise KnowledgeRetrievalError(type(exc).__name__) from None
        check_active()
        # Only metadata is observed; never forward source text to event payloads.
        if _field(result, "structured_content", "structuredContent") is None:
            runtime.observability.publish(
                "knowledge.snippet_only", trace_id=request.turn_id,
                session_id=request.session_id, turn_id=request.turn_id,
                phase="prepare_knowledge", payload={"status": "snippet_only"},
            )
        return decode_result(result, self.config.collection)
