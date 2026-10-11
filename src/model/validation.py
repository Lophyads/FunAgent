"""Complete reply validation, separating call identity from correctable args."""
from collections.abc import Mapping
from dataclasses import replace
from math import isfinite

from ..frame.contracts import ModelReply, ToolCall, thaw
from ..frame.schema import ToolValidator
from .protocols import ModelError


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    if isinstance(value, tuple):
        return all(_json_value(item) for item in value)
    return False


def ValidateReply(reply, seen=(), definitions=()):
    if not isinstance(reply, ModelReply):
        raise ModelError("PROTOCOL")
    if reply.finish_reason == "length":
        raise ModelError("TRUNCATED")
    if reply.finish_reason not in {"stop", "tool_calls"}:
        raise ModelError("PROTOCOL")
    if not isinstance(reply.usage, Mapping) or any(
        not isinstance(key, str) or type(value) is not int or value < 0 for key, value in reply.usage.items()
    ):
        raise ModelError("PROTOCOL")
    if reply.calls:
        if reply.finish_reason != "tool_calls" or (reply.content is not None and not isinstance(reply.content, str)):
            raise ModelError("PROTOCOL")
        batch, calls = set(), []
        schemas = {definition.name: definition.parameters for definition in definitions}
        for call in reply.calls:
            if not isinstance(call, ToolCall) or not isinstance(call.call_id, str) or not call.call_id.strip():
                raise ModelError("PROTOCOL")
            if not isinstance(call.name, str) or not call.name.strip():
                raise ModelError("PROTOCOL")
            if call.call_id in seen or call.call_id in batch or call.argument_error not in (None, "invalid_arguments"):
                raise ModelError("PROTOCOL")
            batch.add(call.call_id)
            try:
                invalid = call.argument_error or not isinstance(call.args, Mapping) or not _json_value(call.args)
            except RecursionError:
                invalid = True
            if invalid:
                call = replace(call, args={}, argument_error="invalid_arguments")
            elif call.name in schemas and not ToolValidator(schemas[call.name]).is_valid(thaw(call.args)):
                # Keep valid JSON only until dispatch checks action authority.
                # Runtime journals a sanitized copy, never these rejected args.
                call = replace(call, argument_error="invalid_arguments")
            calls.append(call)
        return replace(reply, calls=tuple(calls))
    if reply.finish_reason != "stop" or (reply.content is not None and not isinstance(reply.content, str)):
        raise ModelError("PROTOCOL")
    if not reply.content or not reply.content.strip():
        raise ModelError("EMPTY_REPLY")
    return reply
