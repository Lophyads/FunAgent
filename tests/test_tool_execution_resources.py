from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agentcore import (
    NoopMemoryProvider, NormalizedModelResponse, SessionBuildRequest,
    SessionContextFactory, StaticSystemPromptBuilder, ToolEntry, ToolRegistry,
    TurnCancelled, UserInput,
)
from tools.config import WorkspaceConfig
from tools.context import ToolResources
from tools.lifecycle import ResourceUnavailableError
from tools.runtime import ToolSystemNotInitialized, get_tool_services
from tools.toolsets import DefaultToolsetResolver
from conftest import ScriptedTransport, configure_tools
from test_loop_persistence import EchoArgs, memory_loop, tool_response


class StatefulBrowser:
    """Offline backend that makes resource reuse and teardown observable."""

    def __init__(self):
        self.visits = {}
        self.closed = []
        self.lock = threading.Lock()

    def action(self, key, command, args, context, **kwargs):
        context.check_active()
        with self.lock:
            self.visits[key] = self.visits.get(key, 0) + 1
            return self.visits[key]

    def close_session(self, key, *, deadline):
        with self.lock:
            self.closed.append(key)
            self.visits.pop(key, None)

    def close(self, *, deadline):
        self.visits.clear()


def visit(use):
    return ToolResources(use).browser().action(
        "visit", [], SimpleNamespace(check_active=use.check),
    )


def test_model_context_can_be_built_without_a_tool_runtime(config_factory, availability):
    config = config_factory()
    registry = ToolRegistry()
    registry.freeze()
    factory = SessionContextFactory(
        prompt_builder=StaticSystemPromptBuilder("shared prompt"),
        registry=registry, toolset_resolver=DefaultToolsetResolver(),
        memory=NoopMemoryProvider(), memory_max_chars=100,
        defaults=SessionBuildRequest.from_defaults(config.session_defaults),
    )
    built = factory.build(
        "chat", UserInput(text="hello"), None,
        persisted=None, availability=availability,
    )
    assert built.snapshot.prompt.text.startswith("shared prompt")
    assert json.loads(built.snapshot.tool_schema_bytes) == []
    # Context construction neither initializes ToolServices nor registers a turn.
    with pytest.raises(ToolSystemNotInitialized):
        get_tool_services()


def test_same_model_context_runs_concurrently_in_independent_workspaces(config_factory, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    barrier = threading.Barrier(2)

    def echo(args, context):
        barrier.wait(timeout=5)
        context.check_active()
        return str(context.workspace.cwd)

    first, model_context, first_adapter, _ = memory_loop(
        config, ScriptedTransport(tool_response("one"), NormalizedModelResponse(content="done")),
        handler=echo, workspace=one, turn_id="one",
    )
    second, _, second_adapter, _ = memory_loop(
        config, ScriptedTransport(tool_response("two"), NormalizedModelResponse(content="done")),
        model_context=model_context, workspace=two, turn_id="two",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(first.run), pool.submit(second.run)]
        assert all(future.result(timeout=10).status == "completed" for future in futures)
    for loop, expected in ((first, one), (second, two)):
        reply = next(message for message in loop.messages if message.role == "tool")
        assert json.loads(reply.content)["result"] == str(expected)
    assert first_adapter.canonical_requests[0].system_prompt == second_adapter.canonical_requests[0].system_prompt
    assert first_adapter.canonical_requests[0].tool_schema_bytes == second_adapter.canonical_requests[0].tool_schema_bytes
    assert first._state.context == second._state.context == model_context
    assert not get_tool_services()._executions
    assert not get_tool_services()._sessions


def test_foreground_backend_survives_turn_and_agent_replacement(config_factory, agent_factory):
    config = config_factory()
    registry = ToolRegistry()

    def echo(args, context):
        return context.resources.browser().action("visit", [], context)

    registry.register(ToolEntry(
        tool_id="test.echo", implementation_version="1", name="echo",
        description="Visit stateful browser", args_model=EchoArgs, handler=echo,
        side_effects="none",
    ))
    browser = StatefulBrowser()
    for expected in (1, 2):
        with agent_factory(
            config, tool_registry=registry,
            transports={"primary": ScriptedTransport(
                tool_response("visit"), NormalizedModelResponse(content="done"),
            )},
        ) as agent:
            services = get_tool_services()
            services._provider_factories["browser"] = lambda: browser
            assert agent.run_turn_blocking("visit", session_id="chat").status == "completed"
            reply = [message for message in agent._store.load_messages("chat") if message.role == "tool"][-1]
            assert json.loads(reply.content)["result"] == expected
        assert not browser.closed
    assert len(browser.visits) == 1


def test_review_end_and_reaping_preserve_active_foreground_resources(config_factory):
    config = config_factory()
    configure_tools(config)
    services = get_tool_services()
    browser = StatefulBrowser()
    services._provider_factories["browser"] = lambda: browser
    services.begin_execution(
        namespace="foreground", profile="default", task_id="parent-session",
        owner_id="parent", turn_id="live",
    )
    parent = services.create_use("parent", "live", "call", lambda: None)
    services.grants.grant(parent.key, "parent-grant", "session")
    assert not browser.visits  # registration does not start a backend
    try:
        with services.tool_use(parent):
            assert visit(parent) == 1

            def review(args, context):
                return context.resources.browser().action("visit", [], context)

            loop, _, _, _ = memory_loop(
                config, ScriptedTransport(tool_response("review"), NormalizedModelResponse(content="done")),
                handler=review,
            )
            assert loop.run().status == "completed"
            review_key, = set(browser.visits) - {parent.key}
            assert not browser.closed  # ending execution is not resource teardown
            assert services.reap_idle(now=time.monotonic() + 2000).completed
            assert browser.closed == [review_key]
            assert visit(parent) == 2
            assert services.grants.allowed(parent.key, "parent-grant")
            assert review_key not in services._sessions
    finally:
        services.end_execution("parent", "live")


def test_ended_execution_cannot_borrow_or_reuse_resources_from_a_later_turn(config_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    identity = dict(namespace="db", profile="default", task_id="chat", owner_id="owner")
    services.begin_execution(**identity, turn_id="old")
    old = services.create_use("owner", "old", "call", lambda: None)
    services.end_execution("owner", "old")
    services.begin_execution(**identity, turn_id="new")
    try:
        current = services.create_use("owner", "new", "call", lambda: None)
        assert old.key == current.key
        current.check()
        with pytest.raises(ResourceUnavailableError):
            old.check()
        with pytest.raises(ResourceUnavailableError):
            services.create_use("owner", "old", "late", lambda: None)

        def cancelled():
            raise TurnCancelled("cancelled before tool dispatch")

        with pytest.raises(TurnCancelled):
            services.create_use("owner", "new", "cancelled", cancelled)
    finally:
        services.end_execution("owner", "new")


def test_private_resources_are_not_reaped_until_late_tool_handler_exits(config_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    browser = StatefulBrowser()
    services._provider_factories["browser"] = lambda: browser
    services.begin_execution(owner_id="review", turn_id="turn", profile="default")
    use = services.create_use("review", "turn", "call", lambda: None)
    with services.tool_use(use):
        assert visit(use) == 1
        services.end_execution("review", "turn")
        with pytest.raises(ResourceUnavailableError):
            use.check()
        assert services.reap_idle(now=time.monotonic() + 2000).completed
        assert not browser.closed
    assert services.reap_idle(now=time.monotonic() + 2000).completed
    assert browser.closed == [use.key]
    assert use.key not in services._sessions
