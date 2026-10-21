"""Stateless Chat Completions wire adapter; transport lives in openai_provider."""
import json

from ..frame.contracts import ModelReply, ModelRequest, thaw
from .protocols import Adapter, ModelError, ProviderRequest, ProviderResponse
from .validation import ValidateReply, ParseArguments


class OpenAIChatAdapter(Adapter[ModelRequest, ModelReply]):
    @staticmethod
    def Encode(request, profile):
        messages = []
        for message in request.messages:
            wire = {"role": message.role, "content": message.content}
            if message.calls:
                wire["tool_calls"] = [{"id": c.call_id, "type": "function", "function": {
                    "name": c.name, "arguments": json.dumps({} if c.argument_error else thaw(c.args),
                                                             ensure_ascii=False, allow_nan=False)}} for c in message.calls]
            if message.role == "tool":
                wire["tool_call_id"] = message.call_id
            messages.append(wire)
        payload = {"model": profile.model, "messages": messages, "stream": profile.streaming}
        if profile.streaming:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {"name": t.name,
                "description": t.description, "parameters": thaw(t.parameters)}} for t in request.tools]
        return ProviderRequest(payload)

    @staticmethod
    def Decode(response):
        try:
            data = response.payload
            if len(data["choices"]) != 1:
                raise ValueError
            choice = data["choices"][0]
            message = choice["message"]
            if message["role"] != "assistant":
                raise ValueError
            calls = []
            raw_calls = message.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                raise ValueError
            for call in raw_calls or ():
                if call["type"] != "function":
                    raise ValueError
                function = call["function"]
                parsed = ParseArguments(function.get("arguments"), call["id"], function["name"])
                calls.append(parsed)
            usage = data.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError
            usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}
            return ValidateReply(ModelReply(message.get("content"), tuple(calls), choice["finish_reason"], usage))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError("PROTOCOL") from None

    @staticmethod
    def NewStream():
        from .chat_accumulator import ChatAccumulator
        return ChatAccumulator(OpenAIChatAdapter)
