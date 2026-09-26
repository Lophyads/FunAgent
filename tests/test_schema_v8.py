from __future__ import annotations
from tools.runtime import get_tool_registry

import sqlite3
import time

import pytest
from pydantic import BaseModel

from agentcore import (
    Message, NormalizedModelResponse, SQLiteStore, StorageError,
    ToolCall, ToolEntry,
)
from agentcore.storage import LeaseLostError, SCHEMA_VERSION
from conftest import ScriptedTransport


def test_schema_v8_has_exactly_four_business_tables(tmp_path):
    path = tmp_path / "new.db"
    store = SQLiteStore(path)
    store.open()
    store.close()
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION
    assert tables == {
        "sessions", "messages", "session_leases", "delivery_obligations",
    }


def test_legacy_database_is_rejected_without_implicit_migration(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO schema_meta VALUES('version','7')")
    store = SQLiteStore(path)
    with pytest.raises(StorageError, match="incompatible SQLite schema"):
        store.open()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM schema_meta").fetchone()[0] == "7"


def test_tail_repair_completes_only_missing_siblings_and_never_replays(tmp_path):
    store = SQLiteStore(tmp_path / "repair.db")
    store.open()
    lease = store.acquire_lease(
        session_id="session", owner_id="one", turn_id="runtime-only", ttl_seconds=30,
    )
    assert lease is not None
    store.create_session(
        "session", profile="default", surface="cli", platform="test",
        system_prompt_bytes=b"system", user_message=Message.user("run"),
        fence=lease.fence,
    )
    calls = [
        ToolCall(id="read", name="clock"),
        ToolCall(id="read_missing", name="clock"),
        ToolCall(id="write", name="save"),
    ]
    parent_id = store.append_tool_calls(
        "session", Message.assistant_tools(
            calls, tool_call_effects={
                "read": "none", "read_missing": "none", "write": "possible",
            },
        ), fence=lease.fence,
    )
    store.append_tool_result(
        "session", Message.tool_result(
            '{"ok":true}', tool_call_id="read", tool_name="clock",
            reply_to_message_id=parent_id, tool_outcome_state="succeeded",
            effect_disposition="none",
        ), fence=lease.fence,
    )
    repaired = store.repair_latest_tool_batch("session", fence=lease.fence)
    assert [message.tool_call_id for message in repaired.messages] == [
        "read_missing", "write",
    ]
    assert [message.tool_outcome_state for message in repaired.messages] == [
        "unknown", "unknown",
    ]
    assert [message.effect_disposition for message in repaired.messages] == [
        "none", "unknown",
    ]
    assert store.repair_latest_tool_batch("session", fence=lease.fence).messages == ()
    store.release_lease(lease)
    store.close()


def test_expired_lease_marks_resume_and_fences_old_owner(tmp_path):
    path = tmp_path / "fence.db"
    store = SQLiteStore(path)
    store.open()
    old = store.acquire_lease(
        session_id="session", owner_id="old", turn_id="old-turn", ttl_seconds=30,
    )
    assert old is not None
    store.create_session(
        "session", profile="default", surface="cli", platform="test",
        system_prompt_bytes=b"system", user_message=Message.user("unfinished"),
        fence=old.fence,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE session_leases SET expires_at=? WHERE session_id='session'",
            (time.time() - 1,),
        )
    assert store.mark_stale_leases() == ["session"]
    assert store.get_session_recovery("session")["resume_pending"] == 1
    new = store.acquire_lease(
        session_id="session", owner_id="new", turn_id="new-turn", ttl_seconds=30,
    )
    assert new is not None and new.fencing_token > old.fencing_token
    with pytest.raises(LeaseLostError):
        store.append_user_message("session", Message.user("stale"), fence=old.fence)
    store.release_lease(new)
    store.close()


class EchoArgs(BaseModel):
    text: str


def test_existing_session_uses_new_toolset_but_original_prompt(
    config_factory, agent_factory,
):
    config = config_factory(system_prompt="original prompt")
    transport = ScriptedTransport(
        NormalizedModelResponse(content="first", finish_reason="stop"),
        NormalizedModelResponse(content="second", finish_reason="stop"),
    )
    with agent_factory(config, transports={"primary": transport}) as agent:
        assert agent.run_turn_blocking("one", session_id="same").status == "completed"
        get_tool_registry().replace_all((ToolEntry(
            tool_id="echo", implementation_version="2", name="echo",
            description="Echo text", args_model=EchoArgs,
            handler=lambda args, context: args.text, side_effects="none",
        ),))
        assert agent.run_turn_blocking("two", session_id="same").status == "completed"
    assert transport.requests[0].body["messages"][0]["content"] == "original prompt"
    assert transport.requests[1].body["messages"][0]["content"] == "original prompt"
    assert {item["function"]["name"] for item in transport.requests[1].body["tools"]} == {"echo"}


def test_incomplete_text_does_not_create_a_final_tail(config_factory, agent_factory):
    transport = ScriptedTransport(
        NormalizedModelResponse(content="partial", finish_reason="length")
    )
    with agent_factory(config_factory(), transports={"primary": transport}) as agent:
        result = agent.run_turn_blocking("hello", session_id="partial")
        tail = agent._store.latest_message("partial")
    assert result.status == "failed"
    assert result.completion_reason == "incomplete_response"
    assert tail is not None and tail[1].role == "user"
