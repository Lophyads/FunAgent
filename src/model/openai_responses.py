"""Stateless Responses wire conversion and accepted-message replay."""
import json
from collections.abc import Mapping
from dataclasses import replace

from ..frame.contracts import Message, ModelReply, ModelReplay, thaw
from ..observability.projections import Usage
from .protocols import Adapter, ModelError, ProviderRequest
from .validation import ParseArguments, ValidateReply

NAMESPACE = "funagent"
MAX_BYTES = 1_048_576


def NormalizeItems(items):
    if not isinstance(items, (list, tuple)) or len(items) > 128:
        raise ModelError("PROTOCOL")
    result, ids, calls = [], set(), set()
    for original in items:
        if not isinstance(original, Mapping):
            raise ModelError("PROTOCOL")
        item = thaw(original)
        kind = item.get("type")
        if kind not in {"message", "reasoning", "function_call"}:
            raise ModelError("UNSUPPORTED")
        if item.get("status") not in (None, "completed"):
            raise ModelError("TRUNCATED")
        identity = item.get("id")
        if identity is not None:
            if not isinstance(identity, str) or not identity or identity in ids:
                raise ModelError("PROTOCOL")
            ids.add(identity)
        common = {k: item[k] for k in ("id", "type", "status") if item.get(k) is not None}
        if kind == "message":
            if item.get("role") != "assistant" or item.get("phase") not in (None, "commentary", "final_answer"):
                raise ModelError("PROTOCOL")
            content = item.get("content")
            if not isinstance(content, list):
                raise ModelError("PROTOCOL")
            parts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                    raise ModelError("UNSUPPORTED")
                key = "text" if part["type"] == "output_text" else "refusal"
                if not isinstance(part.get(key), str):
                    raise ModelError("PROTOCOL")
                parts.append({"type": part["type"], key: part[key], **({"annotations": []} if key == "text" else {})})
            common.update(role="assistant", content=parts)
            if item.get("phase") is not None:
                common["phase"] = item["phase"]
        elif kind == "reasoning":
            summary = item.get("summary", [])
            if (not isinstance(summary, list) or any(not isinstance(p, dict) or p.get("type") != "summary_text"
                    or not isinstance(p.get("text"), str) for p in summary)):
                raise ModelError("PROTOCOL")
            common["summary"] = [{"type": "summary_text", "text": p["text"]} for p in summary]
            if item.get("encrypted_content") is not None:
                if not isinstance(item["encrypted_content"], str):
                    raise ModelError("PROTOCOL")
                common["encrypted_content"] = item["encrypted_content"]
        else:
            if item.get("namespace") not in (None, NAMESPACE):
                raise ModelError("UNSUPPORTED")
            if any(not isinstance(item.get(k), str) or not item[k].strip() for k in ("call_id", "name")):
                raise ModelError("PROTOCOL")
            if not isinstance(item.get("arguments"), str) or item["call_id"] in calls:
                raise ModelError("PROTOCOL")
            calls.add(item["call_id"])
            common.update({k: item[k] for k in ("call_id", "name", "arguments")})
            if item.get("namespace") is not None:
                common["namespace"] = NAMESPACE
        result.append(common)
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf8")) > MAX_BYTES:
            raise ModelError("PROTOCOL")
    except (ValueError, TypeError, RecursionError):
        raise ModelError("PROTOCOL") from None
    return result


class OpenAIResponsesAdapter(Adapter):
    @staticmethod
    def Encode(request, profile):
        items, instructions = [], []
        for message in request.messages:
            if message.role == "system":
                instructions.append(message.content)
                continue
            replay = message.replay
            if (message.role == "assistant" and replay is not None and replay.version == 1 and
                    replay.format == "openai_responses" and profile.replay_scope and replay.scope == profile.replay_scope):
                items.extend(NormalizeItems(replay.items))
                continue
            if message.role == "tool":
                items.append({"type": "function_call_output", "call_id": message.call_id, "output": message.content})
                continue
            if message.role not in {"user", "assistant", "developer"}:
                raise ModelError("INVALID_REQUEST")
            if message.content:
                items.append({"role": message.role, "content": message.content})
            for call in message.calls:
                items.append({"type": "function_call", "call_id": call.call_id, "name": call.name,
                              "namespace": NAMESPACE, "arguments": json.dumps({} if call.argument_error else thaw(call.args),
                                                                            ensure_ascii=False, allow_nan=False)})
        payload = {"model": profile.model, "input": items, "store": False, "stream": True,
                   "include": ["reasoning.encrypted_content"]}
        if profile.reasoning_effort is not None:
            payload["reasoning"] = {"effort": profile.reasoning_effort}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if request.tools:
            payload["tools"] = [{"type": "namespace", "name": NAMESPACE,
                "description": "Authorized local FunAgent tools.", "tools": [
                    {"type": "function", "name": t.name, "description": t.description,
                     "parameters": thaw(t.parameters), "strict": False} for t in request.tools]}]
        return ProviderRequest(payload, operation="responses")

    @staticmethod
    def Decode(response):
        try:
            data = response.payload
            if data.get("status") != "completed" or data.get("error"):
                raise ModelError("TRUNCATED")
            items = NormalizeItems(data["output"])
            calls = tuple(ParseArguments(i["arguments"], i["call_id"], i["name"])
                          for i in items if i["type"] == "function_call")
            messages = [i for i in items if i["type"] == "message"]
            if not calls and any(i.get("phase") == "final_answer" for i in messages):
                messages = [i for i in messages if i.get("phase") == "final_answer"]
            elif not calls:
                messages = [i for i in messages if i.get("phase") is None]
            content = "".join(p.get("text", p.get("refusal", "")) for i in messages for p in i["content"])
            return ValidateReply(ModelReply(content or None, calls, "tool_calls" if calls else "stop", Usage(data),
                replay=ModelReplay(1, "openai_responses", "", tuple(items))))
        except (TypeError, KeyError, ValueError, AttributeError):
            raise ModelError("PROTOCOL") from None

    @staticmethod
    def BuildMessage(reply, profile):
        if reply.replay is None:
            raise ModelError("PROTOCOL")
        calls = {c.call_id: c for c in reply.calls}
        items = NormalizeItems(reply.replay.items)
        for item in items:
            if item["type"] == "function_call":
                call = calls.get(item["call_id"])
                if call is None or item["name"] != call.name:
                    raise ModelError("PROTOCOL")
                item["arguments"] = json.dumps({} if call.argument_error else thaw(call.args),
                                               ensure_ascii=False, allow_nan=False)
        replay = replace(reply.replay, scope=profile.replay_scope, items=tuple(items))
        return Message("assistant", reply.content or "", reply.calls, replay=replay)

    @staticmethod
    def NewStream():
        from .responses_accumulator import ResponsesAccumulator
        return ResponsesAccumulator(OpenAIResponsesAdapter)
