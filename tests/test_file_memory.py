from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from agentcore import (
    Message, NormalizedModelResponse, SessionRuntimeSnapshot,
    SystemPromptSnapshot, ToolCall, TurnResult, TurnSnapshot,
)
from agentcore.models import SessionDefaults
from agentcore.prompt_builder import PromptBuildRequest, StaticSystemPromptBuilder
from conftest import ScriptedTransport, builtin_registry, scripted_provider_stack
from file_memory.feature import FileMemoryConfig, FileMemoryFeature, load_file_memory_config
from file_memory.prompt import FileMemoryPromptBuilder
from file_memory.review import MemoryReviewRunner, MemoryReviewScheduler, MemoryOnlyGuard
from file_memory.store import FileMemoryStore, MemoryError, MemoryOperation
from file_memory.tool import MemoryArgs, memory_entry
from agentcore.interfaces import ToolInvocation
from agentcore.background_review import ReviewTask
from agentcore.runtime_cwd import ResolvedWorkspace
from tools.config import WorkspaceConfig
from tools.file_tool import (
    ListDirectoryArgs, ReadFileArgs, SearchFilesArgs,
    list_directory, read_file, search_files,
)
from tools.runtime import get_tool_registry


def test_store_roundtrip_limits_and_malformed_file(tmp_path):
    store = FileMemoryStore(tmp_path, memory_char_limit=40, user_char_limit=20)
    write = store.apply(MemoryOperation("add", "memory", "Long-term fact"))
    assert write.changed
    assert not store.apply(MemoryOperation("add", "memory", "Long-term fact")).changed
    assert store.read("memory").entries == ("Long-term fact",)
    assert store.read("user").entries == ()
    store.apply(MemoryOperation("add", "memory", "Second"))
    with pytest.raises(MemoryError, match="exactly one"):
        store.apply(MemoryOperation("remove", "memory", old_text="e"))
    store.apply(MemoryOperation("replace", "memory", "Changed", "Long-term"))
    assert store.read("memory").entries == ("Changed", "Second")
    store.apply(MemoryOperation("remove", "memory", old_text="Second"))
    assert store.read("memory").entries == ("Changed",)
    with pytest.raises(MemoryError, match="limit"):
        store.apply(MemoryOperation("add", "user", "x" * 21))
    oversized = tmp_path / "USER.md"
    oversized.write_text("x" * 21 + "\n§\nsmall", encoding="utf-8", newline="")
    store.apply(MemoryOperation("remove", "user", old_text="small"))
    assert store.read("user").chars == 21
    with pytest.raises(MemoryError, match="limit"):
        store.apply(MemoryOperation("add", "user", "more"))
    path = tmp_path / "MEMORY.md"
    path.write_text("Changed\n§\n", encoding="utf-8")
    with pytest.raises(MemoryError, match="formatting|malformed"):
        store.apply(MemoryOperation("add", "memory", "new"))
    assert path.read_text(encoding="utf-8") == "Changed\n§\n"
    path.write_text("Changed", encoding="utf-8")
    with pytest.raises(MemoryError, match="separator"):
        store.apply(MemoryOperation("add", "memory", "bad\n§\nentry"))


def test_store_concurrent_adds_do_not_lose_entries(tmp_path):
    store = FileMemoryStore(tmp_path, memory_char_limit=2000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: store.apply(MemoryOperation("add", "memory", f"entry {i}")), range(24)))
    assert set(store.read("memory").entries) == {f"entry {i}" for i in range(24)}


def test_store_rejects_configured_symlink_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    store = FileMemoryStore(alias)
    with pytest.raises(MemoryError, match="symlink"):
        store.read("memory")


def test_store_cross_process_adds_do_not_lose_entries(tmp_path):
    store = FileMemoryStore(tmp_path, memory_char_limit=4000)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from file_memory.store import FileMemoryStore, MemoryOperation\n"
        "store = FileMemoryStore(Path(sys.argv[1]), memory_char_limit=4000)\n"
        "for i in range(30): store.apply(MemoryOperation('add', 'memory', f'child {i}'))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], env=env)
    try:
        for i in range(30):
            store.apply(MemoryOperation("add", "memory", f"parent {i}"))
        assert child.wait(timeout=15) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert len(store.read("memory").entries) == 60


def test_prompt_prefers_user_entries_and_obeys_budget(tmp_path):
    store = FileMemoryStore(tmp_path)
    store.apply(MemoryOperation("add", "memory", "less important"))
    store.apply(MemoryOperation("add", "user", "prefers tea"))
    builder = FileMemoryPromptBuilder(StaticSystemPromptBuilder("System"), store, 24)
    request = PromptBuildRequest(())
    prompt = builder.build(request)
    assert "USER.md: prefers tea" in prompt.text
    assert "less important" not in prompt.text
    assert "untrusted context" in prompt.text
    assert prompt.fingerprint == hashlib.sha256(prompt.content_bytes).hexdigest()
    (tmp_path / "USER.md").write_text("broken\n§\n", encoding="utf-8", newline="")
    recovered = FileMemoryPromptBuilder(StaticSystemPromptBuilder("System"), store, 80).build(request)
    assert "less important" in recovered.text
    assert "broken" not in recovered.text


def _snapshot(*, status="completed", trigger="user", messages=()):
    prompt = b"prompt"
    context = SessionRuntimeSnapshot(
        SystemPromptSnapshot("prompt", prompt, hashlib.sha256(prompt).hexdigest()),
        {"memory": memory_entry(FileMemoryStore(__import__("pathlib").Path("unused")))}, b"[]",
    )
    from agentcore.models import TurnError
    return TurnSnapshot(
        result=TurnResult(status=status, session_id="s", turn_id="t",
                          error=TurnError(code="failed", message="failed") if status != "completed" else None),
        messages=tuple(message.model_dump(mode="json") for message in messages),
        context=context, trigger_kind=trigger,
    )


def test_review_cadence_resets_after_successful_foreground_memory_result():
    queued = []
    scheduler = MemoryReviewScheduler(SimpleNamespace(submit=queued.append), 2)
    history = (Message.user("one"), Message.user("two"))
    scheduler.schedule(_snapshot(messages=history))
    assert len(queued) == 1
    scheduler.schedule(_snapshot(trigger="internal_recovery", messages=history))
    scheduler.schedule(_snapshot(status="failed", messages=history))
    assert len(queued) == 1
    successful = Message.tool_result("ok", tool_call_id="c", tool_name="memory",
                                     effect_disposition="confirmed", tool_outcome_state="succeeded")
    scheduler.schedule(_snapshot(messages=(*history, successful, Message.user("three"))))
    assert len(queued) == 1
    scheduler.schedule(_snapshot(messages=(*history, successful, Message.user("three"), Message.user("four"))))
    assert len(queued) == 2


def test_memory_tool_review_restriction_and_dynamic_approval(tmp_path, context_factory):
    store = FileMemoryStore(tmp_path / "memories")
    front = memory_entry(store)
    review = memory_entry(store, review=True)
    assert front.policy_resolver(MemoryArgs(action="list", target="user")).approval_scopes == ()
    assert front.policy_resolver(MemoryArgs(action="add", target="user", content="x")).approval_scopes == ()
    assert front.policy_resolver(MemoryArgs(action="remove", target="user", old_text="x")).approval_scopes
    assert review.policy_resolver(MemoryArgs(action="remove", target="user", old_text="x")).approval_scopes == ()
    assert review.args_model.model_fields["action"].annotation == Literal["list", "add"]
    with context_factory() as context:
        front.handler(MemoryArgs(action="add", target="user", content="prefers tea"), context)
        assert review.handler(MemoryArgs(action="list", target="user"), context)["entries"] == ("prefers tea",)
        with pytest.raises(PermissionError):
            review.handler(MemoryArgs(action="remove", target="user", old_text="tea"), context)
        assert store.read("user").entries == ("prefers tea",)
    assert not MemoryOnlyGuard().check(ToolInvocation("s", "t", "c", "read_file", {}, False)).allowed


def test_review_add_skips_full_document(tmp_path, context_factory):
    store = FileMemoryStore(tmp_path / "memories", user_char_limit=5)
    store.apply(MemoryOperation("add", "user", "12345"))
    with context_factory() as context:
        result = memory_entry(store, review=True).handler(
            MemoryArgs(action="add", target="user", content="more"), context,
        )
    assert result["skipped"] == "capacity"
    assert store.read("user").entries == ("12345",)


def test_generic_file_tools_do_not_expose_memory_directory(tmp_path, context_factory):
    root = tmp_path / "memories"
    root.mkdir()
    (root / "USER.md").write_text("private preference", encoding="utf-8")
    with context_factory() as context:
        context.services.protect_path(root)
        try:
            with pytest.raises(ValueError, match="protected"):
                read_file(ReadFileArgs(path="memories/USER.md"), context)
            assert "memories" not in {item["name"] for item in list_directory(ListDirectoryArgs(), context)["entries"]}
            assert search_files(SearchFilesArgs(query="private"), context)["matches"] == []
        finally:
            context.services.unprotect_path(root)


def test_file_memory_config_resolves_directory(tmp_path):
    path = tmp_path / "agentcore.toml"
    path.write_text('[file_memory]\nenabled = true\ndirectory = "notes"\nreview_every_turns = 0\n', encoding="utf-8")
    settings = load_file_memory_config(path)
    assert settings.directory == tmp_path / "notes"
    assert settings.review_every_turns == 0


def test_feature_installs_tool_and_protects_root(tmp_path, context_factory, config_factory):
    settings = FileMemoryConfig(enabled=True, directory=tmp_path / "memories", review_every_turns=0)
    with context_factory() as context:
        feature = FileMemoryFeature(settings, config_factory())
        feature.install_tool(get_tool_registry())
        try:
            assert get_tool_registry().get_entry("memory") is not None
            assert context.services.is_protected_path(tmp_path / "memories" / "USER.md")
        finally:
            assert feature.close()
        assert not context.services.is_protected_path(tmp_path / "memories" / "USER.md")


def test_new_session_sees_file_write_while_existing_prompt_stays_frozen(
    tmp_path, config_factory, agent_factory,
):
    store = FileMemoryStore(tmp_path / "memories")
    settings = FileMemoryConfig(enabled=True, directory=tmp_path / "memories", review_every_turns=0)
    config = config_factory().model_copy(update={
        "session_defaults": SessionDefaults(enabled_toolsets=("default", "memory")),
    })
    feature = FileMemoryFeature(settings, config)
    registry = builtin_registry()
    registry.replace_all((*registry.entries(), memory_entry(store)))
    response = NormalizedModelResponse(tool_calls=[ToolCall(
        id="call-1", name="memory", arguments=json.dumps({
            "action": "add", "target": "user", "content": "prefers tea",
        }),
    )])
    transport = ScriptedTransport(response, NormalizedModelResponse(content="saved"),
                                  NormalizedModelResponse(content="old"),
                                  NormalizedModelResponse(content="new"))
    agent = agent_factory(config, transports={"primary": transport}, tool_registry=registry,
                          prompt_builder=feature.prompt_builder)
    try:
        assert agent.run_turn_blocking("remember", session_id="first").status == "completed"
        assert store.read("user").entries == ("prefers tea",)
        assert agent.run_turn_blocking("again", session_id="first").status == "completed"
        assert agent.run_turn_blocking("hello", session_id="second").status == "completed"
        system_prompts = [request.body["messages"][0]["content"] for request in transport.requests]
        assert "prefers tea" not in system_prompts[0]
        assert system_prompts[0] == system_prompts[1] == system_prompts[2]
        assert "prefers tea" in system_prompts[3]
    finally:
        assert agent.close_blocking().completed
        assert feature.close()


def test_review_runner_adds_without_persisting_or_using_other_tools(
    tmp_path, config_factory,
):
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    store = FileMemoryStore(tmp_path / "memories")
    entry = memory_entry(store)
    other = next(item for item in builtin_registry().entries() if item.name != "memory")
    prompt = b"original prompt"
    context = SessionRuntimeSnapshot(
        SystemPromptSnapshot(prompt.decode(), prompt, hashlib.sha256(prompt).hexdigest()),
        {"memory": entry, other.name: other},
        json.dumps([entry.definition(), other.definition()]).encode(),
    )
    original = (Message.user("I prefer tea"), Message.assistant_text("noted"))
    snapshot = TurnSnapshot(
        result=TurnResult(status="completed", session_id="s", turn_id="foreground"),
        messages=tuple(item.model_dump(mode="json") for item in original),
        context=context, workspace=ResolvedWorkspace(tmp_path, tmp_path),
        trace_id="foreground",
    )
    transport = ScriptedTransport(
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="review-call", name="memory", arguments=json.dumps({
                "action": "add", "target": "user", "content": "prefers tea",
            }),
        )]),
        NormalizedModelResponse(content="review done"),
    )
    resolver, api_modes, _ = scripted_provider_stack(config, {"primary": transport})
    runner = MemoryReviewRunner(config, store)
    runner.resolver = resolver
    runner.api_modes = api_modes
    try:
        runner(ReviewTask("review-id", snapshot, threading.Event()))
        assert store.read("user").entries == ("prefers tea",)
        assert len(snapshot.messages) == 2
        assert "Current file memory" in transport.requests[0].body["messages"][-1]["content"]
        assert transport.requests[0].body["messages"][0]["content"] == "original prompt"
        assert [tool["function"]["name"] for tool in transport.requests[0].body["tools"]] == ["memory"]
    finally:
        assert runner.close(3)
