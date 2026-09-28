from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agentcore import Message, NormalizedModelResponse, SessionBuildRequest, SQLiteStore, ToolCall
from agentcore.storage import LeaseLostError, SCHEMA_VERSION, _SCHEMA
from tools.config import WorkspaceConfig
from tools.runtime import get_tool_services
from conftest import ScriptedTransport


def test_workspace_inherits_across_turns_and_agent_replacement(config_factory, agent_factory, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    for folder in (one, two):
        folder.mkdir()
        (folder / "note.txt").write_text(folder.name, encoding="utf-8")
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    base = SessionBuildRequest.from_defaults(config.session_defaults)
    for requested, expected in ((one, one), (None, one), (Path("../two"), two), (None, two)):
        transport = ScriptedTransport(
            NormalizedModelResponse(tool_calls=[ToolCall(id="read", name="read_file", arguments='{"path":"note.txt"}')], finish_reason="tool_calls"),
            NormalizedModelResponse(content="done"),
        )
        with agent_factory(config, transports={"primary": transport}) as agent:
            result = agent.run_turn_blocking("read", session_id="chat",
                                            session_request=replace(base, workspace=requested))
            assert result.status == "completed"
            reply = [m for m in agent._store.load_messages("chat") if m.role == "tool"][-1]
            assert json.loads(reply.content)["result"]["lines"][0]["text"] == expected.name
            assert Path(agent._store.load_session("chat").workspace) == expected
    assert not get_tool_services()._providers  # Files do not register any backend resources.


def test_invalid_workspace_does_not_replace_inherited_setting(config_factory, agent_factory, tmp_path):
    config = config_factory().model_copy(update={
        "workspace": WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path),
    })
    transport = ScriptedTransport(NormalizedModelResponse(content="done"))
    with agent_factory(config, transports={"primary": transport}) as agent:
        assert agent.run_turn_blocking("hello", session_id="chat").status == "completed"
        before = agent._store.load_messages("chat")
        request = replace(SessionBuildRequest.from_defaults(config.session_defaults), workspace=tmp_path.parent)
        result = agent.run_turn_blocking("invalid", session_id="chat", session_request=request)
        assert result.error.code == "invalid_workspace"
        assert agent._store.load_messages("chat") == before
        assert Path(agent._store.load_session("chat").workspace) == tmp_path


def test_v8_workspace_upgrade_preserves_data_and_fences_updates(tmp_path):
    path = tmp_path / "v8.db"
    # Reconstruct the previous schema, including a real existing session and history.
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA.replace("    workspace TEXT,\n", ""))
        connection.execute("PRAGMA user_version = 8")
        connection.execute(
            "INSERT INTO sessions(id,profile,surface,platform,system_prompt_bytes,created_at,updated_at) "
            "VALUES('legacy','default','cli','local',?,1,1)", (b"original prompt",),
        )
        connection.execute("INSERT INTO messages(session_id,role,content,created_at) VALUES('legacy','user','keep me',1)")
    store = SQLiteStore(path)
    store.open()
    try:
        session = store.load_session("legacy")
        assert session.workspace is None and session.system_prompt_bytes == b"original prompt"
        assert [m.content for m in store.load_messages("legacy")] == ["keep me"]
        lease = store.acquire_lease(session_id="legacy", owner_id="first", turn_id="first", ttl_seconds=30)
        store.append_user_message("legacy", Message.user("next"), fence=lease.fence, workspace=str(tmp_path))
        store.release_lease(lease)
        successor = store.acquire_lease(session_id="legacy", owner_id="second", turn_id="second", ttl_seconds=30)
        with pytest.raises(LeaseLostError):
            store.append_user_message("legacy", Message.user("stale"), fence=lease.fence, workspace="wrong")
        assert store.load_session("legacy").workspace == str(tmp_path)
        assert [m.content for m in store.load_messages("legacy")] == ["keep me", "next"]
        store.release_lease(successor)
    finally:
        store.close()
    store.open()  # Upgrade is idempotent.
    store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [row[1] for row in connection.execute("PRAGMA table_info(sessions)")].count("workspace") == 1
