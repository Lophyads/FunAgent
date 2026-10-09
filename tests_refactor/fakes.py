"""Offline scripted IO and two intentionally different wire encodings."""
from collections import deque
from threading import Lock

from funagent.contracts import ModelReply, ToolCall, ToolResult
from funagent.model import Adapter, FallbackRule, ModelStrategy, ProviderRequest, ProviderResponse


def plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class ScriptedProvider:
    def __init__(self, *actions):
        self.actions = deque(actions)
        self.requests = []
        self.contexts = []
        self.closed = 0
        self.lock = Lock()

    def Send(self, request, context):
        context.CheckCancelled()
        with self.lock:
            self.requests.append(request)
            self.contexts.append(context)
            action = self.actions.popleft()
        if isinstance(action, Exception):
            raise action
        return action(request, context) if callable(action) else ProviderResponse(action)

    def Close(self):
        self.closed += 1


class ChatAdapter(Adapter):
    @staticmethod
    def Encode(request, profile):
        return ProviderRequest({
            "protocol": "chat", "model": profile.model,
            "messages": [{"role": m.role, "content": m.content,
                          "calls": [{"id": c.call_id, "name": c.name, "args": plain(c.args)} for c in m.calls],
                          "call_id": m.call_id, "status": m.status} for m in request.messages],
            "tools": [{"name": t.name, "parameters": plain(t.parameters)} for t in request.tools],
        })

    @staticmethod
    def Decode(response):
        data = response.payload
        return ModelReply(data.get("text"),
                          tuple(ToolCall(c.get("id"), c.get("name"), c.get("args", {})) for c in data.get("calls", [])),
                          data.get("finish", "stop"), data.get("usage", {}))


class BlocksAdapter(Adapter):
    @staticmethod
    def Encode(request, profile):
        return ProviderRequest({
            "protocol": "blocks", "deployment": profile.model,
            "blocks": [{"speaker": m.role, "value": m.content,
                        "invocations": [{"key": c.call_id, "operation": c.name, "input": plain(c.args)} for c in m.calls],
                        "reply_to": m.call_id, "outcome": m.status} for m in request.messages],
            "functions": [{"operation": t.name, "schema": plain(t.parameters)} for t in request.tools],
        })

    @staticmethod
    def Decode(response):
        data = response.payload
        return ModelReply(data.get("value"),
                          tuple(ToolCall(c.get("key"), c.get("operation"), c.get("input", {})) for c in data.get("invocations", [])),
                          data.get("end", "stop"), data.get("tokens", {}))


class SpyToolHandler:
    def __init__(self, action=None):
        self.calls = []
        self.action = action

    def Execute(self, args, context):
        self.calls.append((args, context))
        if self.action:
            return self.action(args, context)
        return ToolResult("forged-id", "ok", str(args.get("value", "echo")))


class MainStrategy(ModelStrategy):
    @staticmethod
    def SelectModel(): return "main"
    @staticmethod
    def MaxIterations(): return 3
    @staticmethod
    def ToolSet(): return ("echo",)
    @staticmethod
    def FallbackPolicy(): return FallbackRule(("fallback",))


class RecallStrategy(ModelStrategy):
    @staticmethod
    def SelectModel(): return "fallback"
    @staticmethod
    def MaxIterations(): return 2
    @staticmethod
    def ToolSet(): return ("echo",)
    @staticmethod
    def FallbackPolicy(): return FallbackRule()


class ReviewStrategy(ModelStrategy):
    @staticmethod
    def SelectModel(): return "main"
    @staticmethod
    def MaxIterations(): return 1
    @staticmethod
    def ToolSet(): return ()
    @staticmethod
    def FallbackPolicy(): return FallbackRule(retry_codes=frozenset())


def final(protocol, text="done"):
    return {"text": text} if protocol == "chat" else {"value": text}


def calls(protocol, *ids, name="echo"):
    if protocol == "chat":
        return {"calls": [{"id": key, "name": name, "args": {"value": key}} for key in ids], "finish": "tool_calls"}
    return {"invocations": [{"key": key, "operation": name, "input": {"value": key}} for key in ids], "end": "tool_calls"}


def response_for(protocol, chat_payload):
    """Express the exact same response scenario using either wire format."""
    if protocol == "chat" or not isinstance(chat_payload, dict):
        return chat_payload
    result = {}
    keys = {"text": "value", "finish": "end", "usage": "tokens"}
    for key, value in chat_payload.items():
        if key == "calls":
            result["invocations"] = [{ {"id": "key", "name": "operation", "args": "input"}.get(k, k): v
                                       for k, v in call.items()} for call in value]
        else:
            result[keys.get(key, key)] = value
    return result
