from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcore import (
    NoopMemoryProvider, NormalizedModelResponse, SessionBuildRequest,
    SessionContextFactory, StaticSystemPromptBuilder, ToolEntry, ToolRegistry,
    UserInput,
)
from tools.config import WorkspaceConfig
from tools.browser_backend import AgentBrowserBackend
from unittest.mock import patch
from tools.lifecycle import ResourceUnavailableError
from tools.runtime import ToolSystemNotInitialized, get_tool_services
from tools.toolsets import DefaultToolsetResolver
from conftest import ScriptedTransport, configure_tools
from test_loop_persistence import EchoArgs, memory_loop, tool_response


class StatefulBrowser(AgentBrowserBackend):
    """Offline browser using the real backend's cache, locks and cleanup."""

    def __init__(self):
        with patch("tools.browser_backend.shutil.which", return_value="unused-browser"):
            super().__init__(allow_private=False, idle_seconds=900)
        self.visits = {}
        self.closed = []

    def action(self, key, command, args, context, **kwargs):
        with self._locked_session(key, context.check_active):
            self.visits[key] = self.visits.get(key, 0) + 1
            return self.visits[key]

    def _command(self, session, command, args, context, **kwargs):
        return {"success": True}

    def close_session(self, key, *, deadline, idle_before=None):
        previous = self._sessions.get(key)
        super().close_session(key, deadline=deadline, idle_before=idle_before)
        if previous is not None and key not in self._sessions:
            self.closed.append(key)
            self.visits.pop(key, None)


def visit(context):
    return context.services.browser().action(context.resource_key, "visit", [], context)


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
    assert not get_tool_services()._providers


def test_foreground_backend_survives_turn_and_agent_replacement(config_factory, agent_factory):
    config = config_factory()
    registry = ToolRegistry()

    def echo(args, context):
        return context.services.browser().action(context.resource_key, "visit", [], context)

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


def test_review_cleanup_preserves_foreground_resources(config_factory, context_factory):
    config = config_factory()
    configure_tools(config)
    services = get_tool_services()
    browser = StatefulBrowser()
    services._provider_factories["browser"] = lambda: browser
    with context_factory(session_id="parent") as parent:
        assert not browser.visits
        assert visit(parent) == 1
        services.grants.grant(parent.resource_key, "parent-grant", "session")
        loop, _, _, _ = memory_loop(
            config, ScriptedTransport(tool_response("review"), NormalizedModelResponse(content="done")),
            handler=lambda args, context: visit(context),
        )
        assert loop.run().status == "completed"
        review_key, = set(browser.visits) - {parent.resource_key}
        assert not browser.closed
        # The reviewer owns its private key; a generic Loop never closes shared resources.
        assert services.reset_session_resources(review_key).completed
        assert browser.closed == [review_key]
        assert visit(parent) == 2
        assert services.grants.allowed(parent.resource_key, "parent-grant")


def test_expired_context_cannot_reopen_reset_browser(config_factory, context_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    browser = StatefulBrowser()
    services._provider_factories["browser"] = lambda: browser
    with context_factory() as previous:
        assert visit(previous) == 1
    assert services.reset_session_resources(previous.resource_key).completed
    with context_factory() as current:
        assert previous.resource_key == current.resource_key
        assert visit(current) == 1
        with pytest.raises(ResourceUnavailableError):
            visit(previous)
        assert visit(current) == 2


def test_resource_cleanup_retry_joins_pending_close_then_allows_another_reset(config_factory, context_factory):
    configure_tools(config_factory())
    services = get_tool_services()
    release, entered = threading.Event(), threading.Event()
    calls = []

    class SlowBrowser(StatefulBrowser):
        def _command(self, session, command, args, context, **kwargs):
            calls.append(command)
            entered.set()
            assert release.wait(3)
            return {"success": True}

    browser = SlowBrowser()
    services._provider_factories["browser"] = lambda: browser
    with context_factory() as context:
        assert visit(context) == 1
        key = context.resource_key
    try:
        report = services.reset_session_resources(key, timeout=0.03)
        assert not report.completed and any(issue.timed_out for issue in report.issues)
        assert entered.wait(1)
        assert not services.reset_session_resources(key, timeout=0.03).completed
        assert calls == ["close"]
        release.set()
        assert services.reset_session_resources(key, timeout=2).completed
        assert calls == ["close"]
        with context_factory() as current:
            assert visit(current) == 1
        assert services.reset_session_resources(key).completed
        assert calls == ["close", "close"]
    finally:
        release.set()
