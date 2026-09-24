from __future__ import annotations

import hashlib
import inspect

import pytest
from pydantic import BaseModel

from agentcore import ToolContext, ToolEntry, ToolRegistry
from agentcore.turn_phase import TurnPhases
from tools.model_tools import default_tool_registry, load_tools
from tools.toolsets import ToolsetRequest


def _request(
    *enabled: str,
    disabled: tuple[str, ...] = (),
    permissions: frozenset[str] = frozenset(),
) -> ToolsetRequest:
    return ToolsetRequest(
        profile="test", surface="pytest", platform="windows",
        enabled_toolsets=enabled,
        disabled_toolsets=disabled,
        permissions=permissions,
    )


def test_builtin_discovery_and_toolset_policy_share_one_entry_view(tmp_path):
    registry = default_tool_registry(workspace_root=tmp_path)
    registry.freeze()

    definitions, names, entries, schema_bytes = load_tools(
        _request("default"), registry
    )

    assert names == {
        "calculator", "current_time", "list_directory", "read_file",
        "search_files",
    }
    assert set(entries) == names
    assert {item["function"]["name"] for item in definitions} == names
    assert schema_bytes.startswith(b"[")
    assert "write_file" not in names
    assert "terminal" not in names

    _, coding_names, _, _ = load_tools(
        _request(
            "coding", disabled=("file",),
            permissions=frozenset({"file.write", "process.execute"}),
        ),
        registry,
    )
    assert coding_names == {"calculator", "current_time", "terminal"}


def test_availability_check_is_memoized_once_per_load():
    class Args(BaseModel):
        value: str = "ok"

    calls = 0

    def available() -> bool:
        nonlocal calls
        calls += 1
        return True

    def handler(args: Args, context: ToolContext) -> dict[str, str]:
        return {"value": args.value}

    registry = ToolRegistry()
    for name in ("probe_one", "probe_two"):
        registry.register(ToolEntry(
            tool_id=f"test.{name}", implementation_version="1",
            name=name, description=name, args_model=Args, handler=handler,
            toolset="probe", check_fn=available, side_effects="none",
        ))
    registry.freeze()

    _, names, _, _ = load_tools(_request("probe"), registry)

    assert names == {"probe_one", "probe_two"}
    assert calls == 1


def test_unavailable_and_broken_checks_hide_tools():
    class Args(BaseModel):
        pass

    def handler(args: Args, context: ToolContext) -> None:
        return None

    def broken() -> bool:
        raise RuntimeError("backend is unavailable")

    registry = ToolRegistry()
    registry.register(ToolEntry(
        tool_id="test.disabled", implementation_version="1",
        name="disabled", description="disabled", args_model=Args,
        handler=handler, toolset="probe", check_fn=lambda: False,
    ))
    registry.register(ToolEntry(
        tool_id="test.broken", implementation_version="1",
        name="broken", description="broken", args_model=Args,
        handler=handler, toolset="probe", check_fn=broken,
    ))
    registry.freeze()

    definitions, names, entries, schema_bytes = load_tools(
        _request("probe"), registry
    )

    assert definitions == ()
    assert names == frozenset()
    assert not entries
    assert schema_bytes == b"[]"


def test_registry_rejects_duplicate_name_and_version_identity():
    class Args(BaseModel):
        pass

    def handler(args: Args, context: ToolContext) -> None:
        return None

    registry = ToolRegistry()
    registry.register(ToolEntry(
        tool_id="test.first", implementation_version="1", name="first",
        description="first", args_model=Args, handler=handler,
    ))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ToolEntry(
            tool_id="test.other", implementation_version="1", name="first",
            description="duplicate name", args_model=Args, handler=handler,
        ))
    with pytest.raises(ValueError, match="implementation already registered"):
        registry.register(ToolEntry(
            tool_id="test.first", implementation_version="1", name="second",
            description="duplicate version", args_model=Args, handler=handler,
        ))


def test_file_tools_reject_escape_and_detect_concurrent_replacement(tmp_path):
    registry = default_tool_registry(workspace_root=tmp_path)
    registry.freeze()
    context = ToolContext("session", "turn", "call")
    read_file = registry.get_entry("read_file")
    write_file = registry.get_entry("write_file")
    assert read_file is not None and write_file is not None

    with pytest.raises(ValueError, match="outside the workspace"):
        read_file.handler(
            read_file.args_model(path=str(tmp_path.parent / "outside.txt")),
            context,
        )

    target = tmp_path / "note.txt"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since it was read"):
        write_file.handler(
            write_file.args_model(
                path="note.txt", content="new", expected_sha256="0" * 64
            ),
            context,
        )

    expected = hashlib.sha256(b"old").hexdigest()
    result = write_file.handler(
        write_file.args_model(
            path="note.txt", content="new", expected_sha256=expected
        ),
        context,
    )
    assert target.read_text(encoding="utf-8") == "new"
    assert result["sha256"] == hashlib.sha256(b"new").hexdigest()


def test_turn_phase_delegates_tool_execution():
    source = inspect.getsource(TurnPhases)
    assert "tool_round_runner.run" in source
    assert ".handler(" not in source
    assert "model_validate(raw_args)" not in source
