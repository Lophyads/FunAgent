"""Explicit offline demo driver, without network or external effects."""
from uuid import uuid4
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
        if message.role == "tool":
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
