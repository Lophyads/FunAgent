"""Adapt remote MCP tools to the existing synchronous ToolEntry contract."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any, ClassVar, TYPE_CHECKING

from jsonschema import ValidationError, validators
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, model_validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

from .config import McpServerConfig
from .registry import ToolEntry

if TYPE_CHECKING:
    from .mcp_client import McpClient

logger = logging.getLogger(__name__)


class McpToolError(RuntimeError):
    pass


def _deny_external_schema(uri: str):
    raise NoSuchResource(ref=uri)


def _check_references(resource: Resource, resolver) -> None:
    # Resource.subresources visits schema keywords, not instance data in defaults/examples.
    resolver = resolver.in_subresource(resource)
    if isinstance(resource.contents, dict):
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            reference = resource.contents.get(key)
            if reference is not None:
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise ValueError("MCP input schemas only support document-local references")
                resolver.lookup(reference)
    for child in resource.subresources():
        _check_references(child, resolver)


class _McpArguments(BaseModel):
    model_config = ConfigDict(extra="allow")
    _schema: ClassVar[dict]
    _validator: ClassVar[Any]

    @model_validator(mode="before")
    @classmethod
    def validate_arguments(cls, value):
        if not isinstance(value, dict):
            raise ValueError("MCP arguments must be a JSON object")
        try:
            cls._validator.validate(value)
        except (ValidationError, Unresolvable) as exc:
            raise ValueError(f"invalid MCP arguments: {exc}") from None
        return value

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        # Standalone tool schemas may contain $defs unknown to Pydantic's ref remapper.
        return copy.deepcopy(cls._schema)


def _args_model(name: str, input_schema: dict) -> type[BaseModel]:
    schema = copy.deepcopy(input_schema)
    validator_class = validators.validator_for(schema)
    validator_class.check_schema(schema)
    resource = Resource.from_contents(schema, default_specification=DRAFT202012)
    registry = Registry(retrieve=_deny_external_schema).with_resource("urn:funagent:mcp:input", resource)
    _check_references(resource, registry.resolver("urn:funagent:mcp:input"))
    validator = validator_class(schema, registry=registry)
    return type(f"McpArguments_{name}", (_McpArguments,), {
        "_schema": schema, "_validator": validator, "__module__": __name__,
    })


def _tool_name(server_id: str, remote_name: str) -> str:
    raw = f"mcp__{server_id}__{remote_name}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(name) > 64:
        identity = json.dumps([server_id, remote_name], ensure_ascii=False).encode("utf-8")
        suffix = hashlib.sha256(identity).hexdigest()[:10]
        name = f"{name[:53]}_{suffix}"
    return name


def normalize_mcp_result(result: CallToolResult) -> Any:
    texts = [block.text for block in result.content if isinstance(block, TextContent) and block.text]
    text = "\n".join(texts)
    if result.is_error:
        summary = text or json.dumps(result.structured_content, ensure_ascii=False)
        raise McpToolError((summary if summary != "null" else "MCP tool returned an error")[:1024])
    unsupported = sorted({block.type for block in result.content if not isinstance(block, TextContent)})
    if text:
        if unsupported:
            text += f"\n[MCP content not rendered: {', '.join(unsupported)}]"
        return text
    if result.structured_content is not None:
        if unsupported:
            return {"result": result.structured_content, "unsupported_content": unsupported}
        return result.structured_content
    return f"[MCP content not rendered: {', '.join(unsupported)}]" if unsupported else ""


def _build_entry(client: McpClient, server_id: str, config: McpServerConfig,
                 tool: Tool, name: str, check) -> ToolEntry:
    args_model = _args_model(name, tool.input_schema)

    def handler(args, ctx):
        result = client.call_tool(
            server_id, tool.name, args.model_dump(mode="json"), check_active=ctx.check_active,
        )
        return normalize_mcp_result(result)

    return ToolEntry(
        tool_id=f"mcp.{name[5:]}", implementation_version="1", name=name,
        description=(tool.description or "").strip() or f"Call {tool.name} on MCP server {server_id}.",
        args_model=args_model, handler=handler, toolset="mcp", check_fn=check,
        requires_approval=config.requires_approval, side_effects="possible",
        required_capabilities=frozenset({"approval"}) if config.requires_approval else frozenset(),
        timeout_seconds=config.tool_timeout_seconds,
    )


def build_mcp_entries(
    client: McpClient, servers: Mapping[str, McpServerConfig],
    discovered: Mapping[str, tuple[Tool, ...]], *, existing_entries: Sequence[ToolEntry] = (),
) -> tuple[ToolEntry, ...]:
    candidates = []
    for server_id, tools in discovered.items():
        config = servers[server_id]
        if not config.enabled:
            continue
        check = lambda _ctx, sid=server_id: client.is_available(sid)
        for tool in tools:
            if not tool.name.strip():
                logger.warning("mcp.tool_skipped server=%s reason=blank_name", server_id)
                continue
            if config.include and not any(fnmatchcase(tool.name, p) for p in config.include):
                continue
            if any(fnmatchcase(tool.name, p) for p in config.exclude):
                continue
            candidates.append((server_id, config, tool, _tool_name(server_id, tool.name), check))
    counts = Counter(candidate[3] for candidate in candidates)
    existing_names = {entry.name for entry in existing_entries}
    existing_ids = {entry.tool_id for entry in existing_entries}
    entries = []
    for server_id, config, tool, name, check in candidates:
        if counts[name] != 1 or name in existing_names or f"mcp.{name[5:]}" in existing_ids:
            logger.warning("mcp.tool_skipped server=%s name=%s reason=name_conflict", server_id, name)
            continue
        try:
            entry = _build_entry(client, server_id, config, tool, name, check)
            json.dumps(entry.definition(), allow_nan=False)
            entries.append(entry)
        except Exception as exc:
            logger.warning("mcp.tool_skipped server=%s name=%s error_type=%s",
                           server_id, name, type(exc).__name__)
    return tuple(entries)
