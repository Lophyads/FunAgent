from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agentcore import Message, NormalizedModelResponse, SessionBuildRequest, SessionDefaults, SQLiteStore, ToolCall
from agentcore.storage import LeaseLostError, SCHEMA_VERSION, StorageError, _SCHEMA
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


def test_individual_tool_policy_is_resolved_for_each_turn(config_factory, agent_factory):
    config = config_factory().model_copy(update={"session_defaults": SessionDefaults(
        enabled_tools=("write_file",), disabled_tools=("calculator",),
        permissions=frozenset({"file.write"}),
    )})
    transport = ScriptedTransport(
        NormalizedModelResponse(content="first"),
        NormalizedModelResponse(tool_calls=[ToolCall(
            id="calculate", name="calculator", arguments='{"expression":"1 + 2"}',
        )], finish_reason="tool_calls"),
        NormalizedModelResponse(content="3"),
        NormalizedModelResponse(content="third"),
    )
    with agent_factory(config, transports={"primary": transport}) as agent:
        first = agent.run_turn_blocking("first", session_id="policy")
        second = agent.run_turn_blocking("second", session_id="policy", session_request=SessionBuildRequest(
            enabled_tools=("calculator",), disabled_tools=("read_file",),
        ))
        third = agent.run_turn_blocking("third", session_id="policy")
        tool_reply = next(message for message in agent._store.load_messages("policy") if message.role == "tool")
    assert first.status == second.status == third.status == "completed"
    assert json.loads(tool_reply.content)["ok"] is True
    names = [{item["function"]["name"] for item in request.body["tools"]} for request in transport.requests]
    assert "write_file" in names[0] and "calculator" not in names[0]
    assert "calculator" in names[1] and not {"write_file", "read_file"}.intersection(names[1])
    assert names[1] == names[2]
    assert names[3] == names[0]


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
        legacy_schema = _SCHEMA.replace(
            "    id TEXT PRIMARY KEY,\n    system_prompt_bytes",
            "    id TEXT PRIMARY KEY,\n    profile TEXT NOT NULL,\n"
            "    surface TEXT NOT NULL,\n    platform TEXT NOT NULL,\n"
            "    system_prompt_bytes", 1,
        ).replace("    workspace TEXT,\n", "", 1)
        connection.executescript(legacy_schema)
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
        fresh = store.acquire_lease(session_id="new", owner_id="new", turn_id="new", ttl_seconds=30)
        store.create_session(
            "new", system_prompt_bytes=b"new prompt",
            user_message=Message.user("new question"), fence=fresh.fence,
        )
        store.release_lease(fresh)
    finally:
        store.close()
    store.open()  # Upgrade is idempotent.
    store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = [row[1] for row in connection.execute("PRAGMA table_info(sessions)")]
        assert columns.count("workspace") == 1
        assert {"profile", "surface", "platform"}.issubset(columns)
        assert connection.execute(
            "SELECT profile,surface,platform FROM sessions WHERE id='legacy'"
        ).fetchone() == ("default", "cli", "local")
        assert connection.execute(
            "SELECT profile,surface,platform FROM sessions WHERE id='new'"
        ).fetchone() == ("", "", "")


def test_new_database_has_no_session_identity_columns(tmp_path):
    path = tmp_path / "new.db"
    store = SQLiteStore(path)
    store.open()
    store.close()
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    assert {"profile", "surface", "platform"}.isdisjoint(columns)


def test_v9_upgrade_preserves_legacy_columns_and_accepts_new_sessions(tmp_path):
    path = tmp_path / "v9.db"
    with sqlite3.connect(path) as connection:
        legacy_schema = _SCHEMA.replace(
            "    id TEXT PRIMARY KEY,\n    system_prompt_bytes",
            "    id TEXT PRIMARY KEY,\n    profile TEXT NOT NULL,\n"
            "    surface TEXT NOT NULL,\n    platform TEXT NOT NULL,\n"
            "    system_prompt_bytes", 1,
        )
        connection.executescript(legacy_schema)
        connection.execute("PRAGMA user_version = 9")
        connection.execute(
            "INSERT INTO sessions(id,profile,surface,platform,system_prompt_bytes,"
            "created_at,updated_at,workspace) VALUES('old','alice','cli','win',?,1,1,?)",
            (b"prompt", str(tmp_path)),
        )
    store = SQLiteStore(path)
    store.open()
    try:
        assert store.load_session("old").workspace == str(tmp_path)
        lease = store.acquire_lease(session_id="new", owner_id="new", turn_id="new", ttl_seconds=30)
        store.create_session("new", system_prompt_bytes=b"new",
                             user_message=Message.user("hello"), fence=lease.fence)
        store.release_lease(lease)
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT profile,surface,platform FROM sessions WHERE id='old'"
        ).fetchone() == ("alice", "cli", "win")
        assert connection.execute(
            "SELECT profile,surface,platform FROM sessions WHERE id='new'"
        ).fetchone() == ("", "", "")


def test_partial_legacy_identity_schema_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "partial.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(_SCHEMA.replace(
            "    id TEXT PRIMARY KEY,\n    system_prompt_bytes",
            "    id TEXT PRIMARY KEY,\n    profile TEXT NOT NULL,\n    system_prompt_bytes", 1,
        ))
        connection.execute("PRAGMA user_version = 9")
    with pytest.raises(StorageError, match="sessions table layout"):
        SQLiteStore(path).open()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
