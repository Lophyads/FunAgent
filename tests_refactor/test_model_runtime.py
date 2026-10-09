import pytest

from funagent.contracts import Message, RunContext, RunInput
from funagent.model import FallbackRule, ModelError
from fakes import MainStrategy, RecallStrategy, ReviewStrategy, ScriptedProvider, calls, final, response_for


@pytest.mark.parametrize("strategy, protocol", [(MainStrategy, "chat"), (RecallStrategy, "blocks"), (ReviewStrategy, "chat")])
def test_no_tool_final_preserves_input(assemble, strategy, protocol):
    provider = ScriptedProvider(final(protocol))
    runtime, _ = assemble(provider)
    original = [Message("user", "hello")]
    input = RunInput(original, system_prompt="system", evidence=("temporary evidence",))
    result = runtime.Run(input, strategy, RunContext())
    assert result.status == "completed"
    assert result.final == "done"
    assert (result.iterations, result.attempts) == (1, 1)
    assert original == [Message("user", "hello")]
    assert input.messages == (Message("user", "hello"),)
    payload = provider.requests[0].payload
    assert payload["protocol"] == protocol
    wire = payload.get("messages", payload.get("blocks"))
    assert len(wire) == 2
    assert "temporary evidence" in wire[0].get("content", wire[0].get("value"))


@pytest.mark.parametrize("strategy, protocol", [(MainStrategy, "chat"), (RecallStrategy, "blocks")])
def test_tool_batch_pairing_and_one_execution(assemble, strategy, protocol):
    provider = ScriptedProvider(calls(protocol, "c1", "c2"), final(protocol))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput((Message("user", "hello"),)), strategy, RunContext())
    assert result.status == "completed"
    assert len(handler.calls) == 2
    assert [r.call_id for r in result.tool_results] == ["c1", "c2"]
    payload = provider.requests[1].payload
    wire = payload.get("messages", payload.get("blocks"))
    assert [m.get("role", m.get("speaker")) for m in wire] == ["user", "assistant", "tool", "tool"]
    assert [m.get("call_id", m.get("reply_to")) for m in wire[2:]] == ["c1", "c2"]


@pytest.mark.parametrize("strategy, protocol", [(MainStrategy, "chat"), (RecallStrategy, "blocks"), (ReviewStrategy, "chat")])
def test_iteration_limit_never_requests_summary(assemble, strategy, protocol):
    provider = ScriptedProvider(*(calls(protocol, f"c{i}") for i in range(strategy.MaxIterations())))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput((Message("user", "loop"),)), strategy, RunContext())
    assert result.status == "limit"
    assert result.final is None
    assert result.iterations == strategy.MaxIterations()
    assert len(provider.requests) == strategy.MaxIterations()
    assert len(handler.calls) == (strategy.MaxIterations() if strategy.ToolSet() else 0)


class NoFallback(MainStrategy):
    @staticmethod
    def FallbackPolicy(): return FallbackRule()


def test_retry_is_same_iteration_and_does_not_replay_tools(assemble):
    provider = ScriptedProvider(calls("chat", "c1"), ModelError("TRANSPORT"), final("chat"))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput((Message("user", "hello"),)), MainStrategy, RunContext())
    assert result.status == "completed"
    assert (result.iterations, result.attempts) == (2, 3)
    assert len(handler.calls) == 1
    assert provider.requests[1].payload == provider.requests[2].payload


def test_fallback_reencodes_messages_and_tools_with_new_adapter(assemble):
    primary = ScriptedProvider(calls("chat", "c1"), ModelError("TRANSPORT"), ModelError("TRANSPORT"))
    secondary = ScriptedProvider(final("blocks"))
    runtime, handler = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput((Message("user", "hello"),)), MainStrategy, RunContext())
    assert result.status == "completed"
    assert result.profile_id == "fallback"
    assert (result.iterations, result.attempts) == (2, 4)
    assert len(handler.calls) == 1
    payload = secondary.requests[0].payload
    assert payload["deployment"] == "fallback-model"
    assert "messages" not in payload
    assert [m["speaker"] for m in payload["blocks"]] == ["user", "assistant", "tool"]
    assert payload["functions"][0]["operation"] == "echo"


@pytest.mark.parametrize("strategy, count", [(NoFallback, 2), (ReviewStrategy, 1)])
def test_retry_bound_is_controlled_by_policy(assemble, strategy, count):
    provider = ScriptedProvider(*(ModelError("TRANSPORT") for _ in range(5)))
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput(()), strategy, RunContext())
    assert result.status == "failed"
    assert result.final is None
    assert result.attempts == count
    assert len(provider.requests) == count
    assert result.iterations == 0


def test_all_profiles_exhausted_is_structured(assemble):
    primary = ScriptedProvider(ModelError("TRANSPORT"), ModelError("TRANSPORT"))
    secondary = ScriptedProvider(ModelError("TIMEOUT"))
    runtime, _ = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.status == "failed"
    assert result.reason == "PROFILES_EXHAUSTED"
    assert result.error_code == "TIMEOUT"
    assert result.profile_id == "fallback"
    assert result.attempts == 3


@pytest.mark.parametrize("permit", [False, True])
def test_fatal_fallback_happens_only_when_policy_permits(assemble, permit):
    class Policy(MainStrategy):
        @staticmethod
        def FallbackPolicy():
            return FallbackRule(("fallback",), fallback_codes=frozenset({"AUTH"}) if permit else frozenset())
    primary = ScriptedProvider(ModelError("AUTH"))
    secondary = ScriptedProvider(final("blocks"))
    runtime, _ = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput(()), Policy, RunContext())
    assert result.status == ("completed" if permit else "failed")
    assert len(primary.requests) == 1
    assert len(secondary.requests) == int(permit)


def test_successful_fallback_stays_active_in_later_iterations(assemble):
    primary = ScriptedProvider(ModelError("TRANSPORT"), ModelError("TRANSPORT"))
    secondary = ScriptedProvider(calls("blocks", "c1"), final("blocks"))
    runtime, handler = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.status == "completed"
    assert len(primary.requests) == 2
    assert len(secondary.requests) == 2
    assert len(handler.calls) == 1


@pytest.mark.parametrize("payload, code", [
    ({"calls": [{"id": "valid", "name": "echo"}, {"name": "echo"}], "finish": "tool_calls"}, "PROTOCOL"),
    (calls("chat", "duplicate", "duplicate"), "PROTOCOL"),
    ({"calls": [{"id": "c1", "name": "echo", "args": []}], "finish": "tool_calls"}, "PROTOCOL"),
    ({"calls": [{"id": "c1", "name": "", "args": {}}], "finish": "tool_calls"}, "PROTOCOL"),
    ({"calls": [{"id": "c1", "name": "echo", "args": {"x": float("nan")}}], "finish": "tool_calls"}, "PROTOCOL"),
    ({"calls": [{"id": "c1", "name": "echo"}], "finish": "length"}, "TRUNCATED"),
    ({"text": "partial", "finish": "length"}, "TRUNCATED"),
    ({"text": ""}, "PROTOCOL"), ({"text": None}, "PROTOCOL"),
    ({"text": 12}, "PROTOCOL"), ({"text": "done", "usage": {"tokens": -1}}, "PROTOCOL"),
    ({"text": "done", "finish": "unexpected"}, "PROTOCOL"),
    ("SECRET_TOKEN", "PROTOCOL"),
])
@pytest.mark.parametrize("strategy, protocol", [(MainStrategy, "chat"), (RecallStrategy, "blocks")])
def test_invalid_response_never_dispatches_partial_batch_or_fakes_final(assemble, payload, code, strategy, protocol):
    provider = ScriptedProvider(response_for(protocol, payload))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput(()), strategy, RunContext())
    assert result.status == "failed"
    assert result.error_code == code
    assert result.final is None
    assert not result.tool_results
    assert not handler.calls
    assert len(provider.requests) == 1


def test_call_id_cannot_be_reused_in_a_later_iteration(assemble):
    provider = ScriptedProvider(calls("chat", "c1"), calls("chat", "c1"))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.status == "failed"
    assert result.error_code == "PROTOCOL"
    assert len(handler.calls) == 1
    assert len(result.tool_results) == 1


@pytest.mark.parametrize("strategy, name, status", [(ReviewStrategy, "echo", "denied"), (MainStrategy, "ghost", "invalid")])
def test_unauthorized_or_unknown_tool_has_paired_result(assemble, strategy, name, status):
    provider = ScriptedProvider(calls("chat", "c1", name=name), final("chat"))
    runtime, handler = assemble(provider)
    result = runtime.Run(RunInput(()), strategy, RunContext())
    assert result.tool_results[0].status == status
    assert result.tool_results[0].call_id == "c1"
    assert result.tool_results[0].effect == "none"
    assert not handler.calls


def test_generic_provider_exception_is_sanitized_and_not_retried(assemble, caplog):
    provider = ScriptedProvider(RuntimeError("SECRET_TOKEN"))
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.status == "failed"
    assert result.error_code == "PROVIDER_FATAL"
    assert result.attempts == 1
    assert "SECRET_TOKEN" not in repr(result) + caplog.text


@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_control_before_run_sends_nothing(assemble, monkeypatch, control):
    provider = ScriptedProvider(final("chat"))
    runtime, handler = assemble(provider)
    monkeypatch.setattr("time.monotonic", lambda: 10.0)
    context = RunContext(deadline=10.0 if control == "deadline" else None)
    if control == "cancel":
        context.cancel.set()
    result = runtime.Run(RunInput(()), MainStrategy, context)
    assert result.status == "cancelled"
    assert result.reason == ("CANCELLED" if control == "cancel" else "DEADLINE")
    assert not provider.requests and not handler.calls


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_parent_control_after_io_cannot_be_swallowed_by_fallback(assemble, monkeypatch, control, failure):
    now = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    context = RunContext(deadline=20.0 if control == "deadline" else None)
    def action(request, attempt_context):
        if control == "cancel":
            context.cancel.set()
        else:
            now[0] = 20.0
        if failure:
            raise ModelError("TRANSPORT")
        from funagent.model import ProviderResponse
        return ProviderResponse(calls("chat", "c1"))
    primary = ScriptedProvider(action)
    secondary = ScriptedProvider(final("blocks"))
    runtime, handler = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput(()), MainStrategy, context)
    assert result.status == "cancelled"
    assert len(primary.requests) == 1
    assert not secondary.requests and not handler.calls


def test_control_after_decode_does_not_commit_final(assemble):
    from fakes import ChatAdapter, BlocksAdapter
    context = RunContext()
    class CancellingAdapter(ChatAdapter):
        @staticmethod
        def Decode(response):
            context.cancel.set()
            return ChatAdapter.Decode(response)
    provider = ScriptedProvider(final("chat"))
    runtime, _ = assemble(provider, adapters={"chat": CancellingAdapter, "blocks": BlocksAdapter})
    result = runtime.Run(RunInput(()), MainStrategy, context)
    assert result.status == "cancelled"
    assert result.final is None


def test_cancel_after_first_tool_stops_batch_and_marks_unexecuted_calls(assemble):
    from fakes import SpyToolHandler
    from funagent.contracts import ToolResult
    context = RunContext()
    def action(args, _):
        context.cancel.set()
        return ToolResult("ignored", "ok", "known work", "known")
    provider = ScriptedProvider(calls("chat", "c1", "c2"))
    runtime, handler = assemble(provider, handler=SpyToolHandler(action))
    result = runtime.Run(RunInput(()), MainStrategy, context)
    assert result.status == "cancelled"
    assert len(handler.calls) == 1
    assert [r.status for r in result.tool_results] == ["ok", "cancelled"]
    assert [r.effect for r in result.tool_results] == ["known", "none"]
    assert len(provider.requests) == 1


def test_deadline_after_last_tool_takes_priority_over_iteration_limit(assemble, monkeypatch):
    from fakes import SpyToolHandler
    from funagent.contracts import ToolResult
    class OneRound(MainStrategy):
        @staticmethod
        def MaxIterations(): return 1
    now = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    def action(args, context):
        now[0] = 20.0
        return ToolResult("ignored", "ok", "finished")
    provider = ScriptedProvider(calls("chat", "c1"))
    runtime, _ = assemble(provider, handler=SpyToolHandler(action))
    result = runtime.Run(RunInput(()), OneRound, RunContext(deadline=20.0))
    assert result.status == "cancelled"
    assert result.reason == "DEADLINE"
    assert result.tool_results[0].status == "ok"


def test_attempt_timeout_retries_without_cancelling_active_parent(assemble, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    parent = RunContext(run_id="parent", deadline=100.0)
    def expire(request, context):
        assert context.cancel is parent.cancel
        assert context.Remaining() == 5.0
        now[0] = context.deadline
        context.CheckCancelled()
    provider = ScriptedProvider(expire, final("chat"))
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput(()), MainStrategy, parent)
    assert result.status == "completed"
    assert result.attempts == 2
    assert not parent.cancel.is_set()
    parent.CheckCancelled()


def test_parallel_and_sequential_runs_keep_dialogue_and_counts_local(assemble):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from funagent.model import ProviderResponse
    barrier = Barrier(2)
    def action(request, context):
        wire = request.payload["messages"]
        user = next(m["content"] for m in wire if m["role"] == "user")
        assert sum(m["role"] == "user" for m in wire) == 1
        if wire[-1]["role"] == "user":
            if user in {"a", "b"}:
                barrier.wait(timeout=3)
            return ProviderResponse(calls("chat", user))
        assert wire[-1]["call_id"] == user
        return ProviderResponse(final("chat", user))
    provider = ScriptedProvider(*(action for _ in range(6)))
    runtime, handler = assemble(provider)
    def run(text):
        return runtime.Run(RunInput((Message("user", text),)), MainStrategy, RunContext(run_id=text))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, text) for text in ("a", "b")]
        try:
            results = [future.result(timeout=5) for future in futures]
        finally:
            barrier.abort()
    results.append(run("c"))
    assert [r.final for r in results] == ["a", "b", "c"]
    assert all((r.iterations, r.attempts) == (2, 2) for r in results)
    assert [[t.call_id for t in r.tool_results] for r in results] == [["a"], ["b"], ["c"]]
    assert len(handler.calls) == 3


@pytest.mark.parametrize("phase", ["encode", "decode"])
def test_adapter_fault_has_standard_sanitized_failure(assemble, phase):
    from fakes import ChatAdapter, BlocksAdapter
    class BrokenAdapter(ChatAdapter):
        @staticmethod
        def Encode(request, profile):
            if phase == "encode":
                raise ValueError("SECRET_TOKEN")
            return ChatAdapter.Encode(request, profile)
        @staticmethod
        def Decode(response):
            raise ValueError("SECRET_TOKEN")
    provider = ScriptedProvider(final("chat"))
    runtime, handler = assemble(provider, adapters={"chat": BrokenAdapter, "blocks": BlocksAdapter})
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.status == "failed" and result.error_code == "PROTOCOL"
    assert result.attempts == (0 if phase == "encode" else 1)
    assert not handler.calls
    assert "SECRET_TOKEN" not in repr(result)


def test_nonstandard_provider_error_code_cannot_leak_a_secret(assemble):
    provider = ScriptedProvider(ModelError("SECRET_TOKEN"))
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.error_code == "PROVIDER_FATAL"
    assert "SECRET_TOKEN" not in repr(result)


def test_protocol_failure_fallback_is_explicit_and_never_dispatches_bad_batch(assemble):
    class PermitProtocol(MainStrategy):
        @staticmethod
        def FallbackPolicy():
            return FallbackRule(("fallback",), fallback_codes=frozenset({"PROTOCOL"}))
    primary = ScriptedProvider({"calls": [{"id": "c1", "name": "echo"}, {"name": "echo"}], "finish": "tool_calls"})
    secondary = ScriptedProvider(final("blocks"))
    runtime, handler = assemble(primary, secondary=secondary)
    result = runtime.Run(RunInput(()), PermitProtocol, RunContext())
    assert result.status == "completed"
    assert (result.iterations, result.attempts) == (1, 2)
    assert not handler.calls


def test_cancelled_run_waits_for_running_tool_and_preserves_unknown_effect(assemble):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from fakes import SpyToolHandler
    entered, release = Event(), Event()
    context = RunContext()
    effects = []
    def blocked(args, tool_context):
        entered.set()
        assert release.wait(timeout=3)
        effects.append("actual effect")
        tool_context.CheckCancelled()
    provider = ScriptedProvider(calls("chat", "c1", "c2"))
    runtime, handler = assemble(provider, handler=SpyToolHandler(blocked))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.Run, RunInput(()), MainStrategy, context)
        try:
            assert entered.wait(timeout=2)
            context.cancel.set()
            assert not future.done()
            assert provider.closed == 0
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.status == "cancelled"
    assert effects == ["actual effect"]
    assert len(handler.calls) == 1
    assert [r.status for r in result.tool_results] == ["unknown", "cancelled"]
    assert [r.effect for r in result.tool_results] == ["unknown", "none"]


def test_usage_accumulates_only_valid_successful_replies(assemble):
    first = calls("chat", "c1")
    first["usage"] = {"input": 2, "output": 1}
    second = final("chat")
    second["usage"] = {"input": 4, "output": 2}
    provider = ScriptedProvider(first, ModelError("TRANSPORT"), second)
    runtime, _ = assemble(provider)
    result = runtime.Run(RunInput(()), MainStrategy, RunContext())
    assert result.usage == {"input": 6, "output": 3}
    with pytest.raises(TypeError):
        result.usage["input"] = 99
