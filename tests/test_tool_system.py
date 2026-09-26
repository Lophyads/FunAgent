from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace

import pytest
from pydantic import BaseModel

from agentcore import ToolContext, ToolEntry, ToolRegistry
from agentcore.turn_phase import TurnPhases
from tools.model_tools import get_tool_definitions, load_tools
from conftest import builtin_registry
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


def test_builtin_discovery_and_toolset_policy_share_one_entry_view(tmp_path, availability):
    registry = builtin_registry()
    registry.freeze()

    entries, schema_bytes = load_tools(
        _request("default"), registry, availability=availability
    )

    names = set(entries)
    assert names == {
        "calculator", "current_time", "list_directory", "read_file",
        "search_files", "web_extract", "web_search",
    }
    assert {item["function"]["name"] for item in json.loads(schema_bytes)} == names
    assert schema_bytes.startswith(b"[")
    assert "write_file" not in names
    assert "terminal" not in names

    coding_entries, _ = load_tools(
        _request(
            "coding", disabled=("file",),
            permissions=frozenset({"file.write", "process.execute"}),
        ),
        registry, availability=availability,
    )
    assert set(coding_entries) == {"calculator", "current_time", "terminal", "web_search", "web_extract"}


def test_availability_check_is_memoized_once_per_load(availability):
    class Args(BaseModel):
        value: str = "ok"

    calls = 0

    def available(context) -> bool:
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

    entries, _ = load_tools(_request("probe"), registry, availability=availability)

    assert set(entries) == {"probe_one", "probe_two"}
    assert calls == 1


def test_unavailable_and_broken_checks_hide_tools(availability):
    class Args(BaseModel):
        pass

    def handler(args: Args, context: ToolContext) -> None:
        return None

    def broken(context) -> bool:
        raise RuntimeError("backend is unavailable")

    registry = ToolRegistry()
    registry.register(ToolEntry(
        tool_id="test.disabled", implementation_version="1",
        name="disabled", description="disabled", args_model=Args,
        handler=handler, toolset="probe", check_fn=lambda context: False,
    ))
    registry.register(ToolEntry(
        tool_id="test.broken", implementation_version="1",
        name="broken", description="broken", args_model=Args,
        handler=handler, toolset="probe", check_fn=broken,
    ))
    registry.freeze()

    entries, schema_bytes = load_tools(
        _request("probe"), registry, availability=availability
    )

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


def test_file_tools_reject_escape_and_detect_concurrent_replacement(tmp_path, context_factory):
    registry = builtin_registry()
    registry.freeze()
    with context_factory() as context:
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

def test_tool_loading_keeps_schema_and_handler_on_the_same_registry_generation(availability):
    class Args(BaseModel):
        pass

    first = ToolEntry(
        tool_id="test.replace", implementation_version="1", name="replaceable",
        description="original", args_model=Args, handler=lambda args, context: "original",
        toolset="replaceable",
    )
    replacement = replace(
        first, implementation_version="2", description="replacement",
        handler=lambda args, context: "replacement",
    )
    registry = ToolRegistry()
    registry.replace_all((first,))

    class ReplacingResolver:
        def resolve(self, request, entries):
            registry.replace_all((replacement,))
            return ("replaceable",)

    request = _request("replaceable")
    entries, schema = load_tools(request, registry, ReplacingResolver(), availability=availability)
    assert entries["replaceable"] is first
    assert json.loads(schema)[0]["function"]["description"] == "original"
    assert registry.get_entry("replaceable") is replacement
    definitions = get_tool_definitions(request, registry, availability=availability)
    assert definitions[0]["function"]["description"] == "replacement"
    with pytest.raises(TypeError):
        entries["replaceable"] = replacement
