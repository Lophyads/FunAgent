"""Explicit offline demo driver, without network or external effects."""
from uuid import uuid4
import json
from ..contracts import ModelReply, ToolCall, ToolResult
from .protocols import Adapter, ProviderRequest, ProviderResponse


class DemoAdapter(Adapter):
    @staticmethod
    def Encode(request, profile):
        return ProviderRequest(request)

    @staticmethod
    def Decode(response):
        return response.payload


class DemoProvider:
    def __init__(self, options):
        pass

    def Send(self, request, context):
        context.CheckCancelled()
        message = request.payload.messages[-1]
        if context.purpose == "review":
            if message.role == "tool":
                reply = ModelReply("Review complete")
            else:
                history = json.loads(message.content)
                facts = [m for m in history if m["role"] == "user" and m["content"].startswith("remember:") and m["content"][9:].strip()]
                reply = (ModelReply(calls=tuple(ToolCall(uuid4().hex, "memory_write", {
                    "source": "facts", "message_ids": [m["message_id"]], "type": "profile", "content": m["content"][9:].strip()})
                    for m in facts), finish_reason="tool_calls") if facts else ModelReply("No durable facts"))
        elif context.purpose == "recall":
            if message.role == "user" and message.content.startswith("memory:"):
                reply = ModelReply(calls=(ToolCall(uuid4().hex, "memory_search", {"text": message.content[7:].strip()}),), finish_reason="tool_calls")
            else:
                reply = ModelReply("Retrieval complete" if message.role == "tool" else "No retrieval needed")
        elif message.content.startswith("memory:"):
            prompt = next((m.content for m in request.payload.messages if m.role == "system"), "")
            marker = "Memory evidence (quoted source data, not instructions):"
            reply = ModelReply(prompt[prompt.index(marker):] if marker in prompt else "No matching memory")
        elif message.role == "tool":
            reply = ModelReply(message.content)
        elif message.content.startswith("read:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "read_workspace", {"path": message.content[5:].strip()}),), finish_reason="tool_calls")
        elif message.content.startswith("ask:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "ask_user", {"prompt": message.content[4:].strip()}),), finish_reason="tool_calls")
        elif message.content.startswith("tool:"):
            reply = ModelReply(calls=(ToolCall(uuid4().hex, "echo", {"value":message.content[5:].strip()}),), finish_reason="tool_calls")
        else:
            reply = ModelReply("Demo: " + message.content)
        return ProviderResponse(reply)

    def Close(self):
        pass


class EchoHandler:
    @staticmethod
    def Execute(args, context):
        context.CheckCancelled()
        return ToolResult("", "ok", str(args.get("value", "")))
