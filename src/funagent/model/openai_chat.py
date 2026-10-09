"""Chat Completions wire adapter and one SDK attempt, with implicit retry disabled."""
import json
import os

from ..contracts import ControlError, DomainError, ModelReply, ToolCall, thaw
from .protocols import Adapter, ModelError, ProviderRequest, ProviderResponse




class OpenAIChatAdapter(Adapter):
    @staticmethod
    def Encode(request, profile):
        messages = []
        for message in request.messages:
            wire = {"role": message.role, "content": message.content}
            if message.calls:
                wire["tool_calls"] = [{"id": c.call_id, "type": "function", "function": {
                    "name": c.name, "arguments": json.dumps(thaw(c.args), ensure_ascii=False)}} for c in message.calls]
            if message.role == "tool":
                wire["tool_call_id"] = message.call_id
            messages.append(wire)
        payload = {"model": profile.model, "messages": messages, "stream": False}
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
            for call in message.get("tool_calls") or ():
                if call["type"] != "function":
                    raise ValueError
                function = call["function"]
                args = json.loads(function["arguments"])
                if not isinstance(args, dict):
                    raise ValueError
                calls.append(ToolCall(call["id"], function["name"], args))
            usage = data.get("usage") or {}
            usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}
            return ModelReply(message.get("content"), tuple(calls), choice["finish_reason"], usage)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError("PROTOCOL") from None


class OpenAIProvider:
    def __init__(self, options, *, transport=None):
        try:
            import openai
            import httpx
        except ImportError:
            raise DomainError("DRIVER_UNAVAILABLE", "OpenAI SDK is unavailable") from None
        credential_env = options.get("credential_env", "OPENAI_API_KEY")
        token = os.environ.get(credential_env)
        if not token:
            raise DomainError("CONFIG_CREDENTIAL", "Model credential environment variable is missing")
        self._openai = openai
        self._closed = False
        client = httpx.Client(transport=transport, timeout=30)
        try:
            self._client = openai.OpenAI(api_key=token, base_url=options.get("base_url"),
                                          http_client=client, timeout=30, max_retries=0)
        except Exception:
            client.close()
            raise DomainError("CONFIG_INVALID", "Invalid model connection configuration") from None

    def Send(self, request, context):
        if self._closed:
            raise DomainError("NOT_READY", "Provider closed")
        context.CheckCancelled()
        try:
            response = self._client.with_options(timeout=min(30, context.Remaining())).chat.completions.create(**request.payload)
            context.CheckCancelled()
            return ProviderResponse(response.model_dump())
        except ControlError:
            raise
        except self._openai.APITimeoutError:
            context.CheckCancelled()
            raise ModelError("TIMEOUT") from None
        except self._openai.APIConnectionError as error:
            context.CheckCancelled()
            if isinstance(error.__cause__, ControlError):
                raise error.__cause__ from None
            raise ModelError("TRANSPORT") from None
        except self._openai.APIStatusError as error:
            context.CheckCancelled()
            status = error.status_code
            code = ("AUTH" if status in {401,403} else "RATE_LIMIT" if status == 429
                    else "TIMEOUT" if status == 408 else "TRANSPORT" if status >= 500 or status == 409
                    else "INVALID_REQUEST")
            raise ModelError(code) from None
        except Exception:
            context.CheckCancelled()
            raise ModelError("PROVIDER_FATAL") from None

    def Close(self):
        if not self._closed:
            self._client.close()
            self._closed = True
