import pytest

from funagent.contracts import DomainError, Message, ModelRequest, RunContext, RunInput, ToolCall, ToolDefinition
from funagent.model import ModelRuntime, ProviderResponse
from funagent.tool import ToolMgr
from fakes import ChatAdapter, BlocksAdapter, MainStrategy, RecallStrategy, ScriptedProvider, calls, final


@pytest.mark.parametrize("adapter, protocol", [(ChatAdapter, "chat"), (BlocksAdapter, "blocks")])
def test_adapter_contract_can_swap_wire_format(conf_dir, adapter, protocol):
    from funagent.conf import ConfMgr
    ConfMgr.Initialize(conf_dir)
    profile = ConfMgr.GetInstance().Model().Profile("main" if protocol == "chat" else "fallback")
    message = Message("assistant", calls=(ToolCall("c1", "echo", {"value": "x"}),))
    tool = ToolDefinition("echo", "Echo", {"type": "object"})
    request = adapter.Encode(ModelRequest((message,), (tool,)), profile)
    assert request.payload["protocol"] == protocol
    assert adapter.Decode(ProviderResponse(final(protocol))).content == "done"
    reply = adapter.Decode(ProviderResponse(calls(protocol, "c1")))
    assert reply.calls == (ToolCall("c1", "echo", {"value": "c1"}),)
    assert reply.finish_reason == "tool_calls"


@pytest.mark.parametrize("strategy, protocol", [(MainStrategy, "chat"), (RecallStrategy, "blocks")])
def test_provider_receives_bounded_attempt_context_and_immutable_input(assemble, strategy, protocol):
    parent = RunContext(run_id="r", deadline=None)
    provider = ScriptedProvider(final(protocol))
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput((Message("user", "hello"),)), strategy, parent)
    attempt = provider.contexts[0]
    assert result.status == "completed"
    assert 0 < attempt.Remaining() <= (5.0 if protocol == "chat" else 3.0)
    assert attempt.cancel is parent.cancel
    assert attempt.run_id == "r"
    assert parent.deadline is None


def test_runtime_owns_providers_closes_each_connection_once(assemble):
    provider = ScriptedProvider(final("chat"))
    assemble(provider, secondary=provider)
    ModelRuntime.Close()
    ModelRuntime.Close()
    assert provider.closed == 1
    with pytest.raises(DomainError) as error:
        ModelRuntime.GetInstance()
    assert error.value.code == "NOT_READY"


def test_invalid_startup_bindings_do_not_publish_runtime(conf_dir):
    from funagent.conf import ConfMgr
    ConfMgr.Initialize(conf_dir)
    profile = ConfMgr.GetInstance().Model().Profile("main")
    provider = ScriptedProvider()
    with pytest.raises(DomainError) as error:
        ModelRuntime.Initialize({"main": profile}, {"fake": provider}, {})
    assert error.value.code == "CONFIG_REFERENCE"
    assert provider.closed == 0
    with pytest.raises(DomainError):
        ModelRuntime.GetInstance()
    provider.Close()


def test_tool_definitions_and_dispatch_use_same_immutable_registry(assemble):
    provider = ScriptedProvider(final("chat"))
    _, handler = assemble(provider)
    registry = ToolMgr.GetInstance()
    assert registry.Definitions(()) == ()
    assert tuple(t.name for t in registry.Definitions(("echo",))) == ("echo",)
    with pytest.raises(TypeError):
        registry.Definitions(("echo",))[0].parameters["type"] = "string"
    denied = registry.Execute(ToolCall("c1", "echo"), (), RunContext())
    invalid = registry.Execute(ToolCall("c2", "ghost"), ("ghost",), RunContext())
    assert (denied.status, invalid.status) == ("denied", "invalid")
    assert not handler.calls


def test_declared_fallback_requires_startup_binding(conf_dir):
    from funagent.conf import ConfMgr
    ConfMgr.Initialize(conf_dir)
    profile = ConfMgr.GetInstance().Model().Profile("main")
    provider = ScriptedProvider()
    try:
        with pytest.raises(DomainError) as error:
            ModelRuntime.Initialize({"main": profile}, {"fake": provider}, {"chat": ChatAdapter})
        assert error.value.code == "CONFIG_REFERENCE"
        with pytest.raises(DomainError):
            ModelRuntime.GetInstance()
    finally:
        ModelRuntime.Close()
        if not provider.closed:
            provider.Close()


def test_close_failure_still_closes_other_owned_connections_and_clears_runtime(assemble):
    class FaultyProvider(ScriptedProvider):
        def Close(self):
            self.closed += 1
            raise RuntimeError("SECRET_TOKEN")
    first, second = FaultyProvider(), ScriptedProvider()
    assemble(first, secondary=second)
    with pytest.raises(DomainError) as error:
        ModelRuntime.Close()
    assert error.value.code == "CLOSE_FAILED"
    assert "SECRET_TOKEN" not in str(error.value)
    assert first.closed == second.closed == 1
    with pytest.raises(DomainError):
        ModelRuntime.GetInstance()


import httpx
import json
import time
from funagent.model.openai_chat import OpenAIProvider, OpenAIChatAdapter
from funagent.model import ModelError
from funagent.conf import ModelProfile


def completion(text="complete", calls=None, finish="stop"):
    return {"id": "response", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text,
                                                   "tool_calls": calls}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


def test_real_sdk_transport_encodes_pairs_auth_timeout_and_decodes(monkeypatch):
    captured = []
    monkeypatch.setattr("funagent.contracts.time.monotonic", lambda: 100.0)
    monkeypatch.setenv("P2_TEST_KEY", "test-token")
    def transport(request):
        captured.append(request)
        return httpx.Response(200, json=completion())
    provider = OpenAIProvider({"credential_env": "P2_TEST_KEY", "base_url": "https://offline.test/v1"},
                              transport=httpx.MockTransport(transport))
    try:
        profile = ModelProfile("main", "api", "openai_chat", "test-model", 2)
        request = ModelRequest((Message("assistant", calls=(ToolCall("c1", "echo", {"value":"你好"}),)),
                                Message("tool", "ok", call_id="c1", status="ok")),
                               (ToolDefinition("echo", "Echo", {"type":"object"}),))
        encoded = OpenAIChatAdapter.Encode(request, profile)
        reply = OpenAIChatAdapter.Decode(provider.Send(encoded, RunContext(deadline=time.monotonic()+2)))
        assert reply.content == "complete" and reply.usage["total_tokens"] == 5
        wire = json.loads(captured[0].content)
        assert wire["messages"][0]["tool_calls"][0]["id"] == "c1"
        assert json.loads(wire["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {"value":"你好"}
        assert wire["messages"][1] == {"role":"tool", "content":"ok", "tool_call_id":"c1"}
        assert wire["tools"][0]["function"]["name"] == "echo"
        assert captured[0].headers["authorization"] == "Bearer test-token"
        assert str(captured[0].url) == "https://offline.test/v1/chat/completions"
        assert 0 < captured[0].extensions["timeout"]["read"] <= 2
    finally:
        provider.Close()


@pytest.mark.parametrize("status, code", [(401,"AUTH"),(400,"INVALID_REQUEST"),(429,"RATE_LIMIT"),(503,"TRANSPORT")])
def test_real_provider_disables_sdk_retry_and_sanitizes_errors(monkeypatch, status, code):
    monkeypatch.setenv("P2_TEST_KEY", "SECRET_TOKEN")
    seen = []
    def transport(request):
        seen.append(request)
        return httpx.Response(status, json={"error":{"message":"SECRET_TOKEN wire failure"}})
    provider = OpenAIProvider({"credential_env":"P2_TEST_KEY"}, transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(ModelError) as error:
            provider.Send(OpenAIChatAdapter.Encode(ModelRequest((Message("user","hi"),)),
                          ModelProfile("main","api","openai_chat","test-model",1)), RunContext())
        assert error.value.code == code
        assert len(seen) == 1 and "SECRET_TOKEN" not in str(error.value)
    finally:
        provider.Close()


def test_real_provider_timeout_and_cancel_prevent_new_io(monkeypatch):
    monkeypatch.setenv("P2_TEST_KEY", "test-token")
    seen=[]
    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout("SECRET_TOKEN", request=request)
    provider = OpenAIProvider({"credential_env":"P2_TEST_KEY"}, transport=httpx.MockTransport(timeout))
    try:
        request = OpenAIChatAdapter.Encode(ModelRequest((Message("user","hi"),)), ModelProfile("main","api","openai_chat","test-model",1))
        with pytest.raises(ModelError) as error:
            provider.Send(request, RunContext())
        assert error.value.code == "TIMEOUT" and len(seen) == 1
        context=RunContext()
        context.cancel.set()
        with pytest.raises(DomainError) as error:
            provider.Send(request, context)
        assert error.value.code == "CANCELLED" and len(seen) == 1
    finally:
        provider.Close()


def test_real_adapter_decodes_tool_args_and_rejects_partial_batch():
    valid = [{"id":"c1","type":"function","function":{"name":"echo","arguments":'{"value":"你好"}'}}]
    reply = OpenAIChatAdapter.Decode(ProviderResponse(completion(None, valid, "tool_calls")))
    assert reply.calls == (ToolCall("c1", "echo", {"value":"你好"}),)
    invalid = valid + [{"id":"c2","type":"function","function":{"name":"echo","arguments":'{"value":'}}]
    with pytest.raises(ModelError) as error:
        OpenAIChatAdapter.Decode(ProviderResponse(completion(None, invalid, "tool_calls")))
    assert error.value.code == "PROTOCOL"
