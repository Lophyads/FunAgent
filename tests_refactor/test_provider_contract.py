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
