"""P3 tool validation, trusted workspace, and truthful effects."""
from pathlib import Path
import pytest

from funagent.contracts import DomainError, RunContext, ToolCall, ToolDefinition, ToolResult
from funagent.tool import ToolMgr
from fakes import SpyToolHandler

SCHEMA = {
    "type": "object", "required": ["items"], "additionalProperties": False,
    "properties": {"items": {"type": "array", "minItems": 1,
        "items": {"type": "object", "required": ["name", "count"], "additionalProperties": False,
            "properties": {"name": {"type": "string", "enum": ["read"]},
                           "count": {"type": "integer", "minimum": 1, "maximum": 3}}}}},
}


@pytest.mark.parametrize("args", [
    {}, {"items": []}, {"items": [{"name": "write", "count": 1}]},
    {"items": [{"name": "read", "count": True}]},
    {"items": [{"name": "read", "count": 4}]},
    {"items": [{"name": "read", "count": 1}], "access": ["admin"]},
])
def test_schema_invalid_never_dispatches(args):
    spy = SpyToolHandler()
    ToolMgr.Initialize({"read": ToolDefinition("read", "read", SCHEMA)}, {"read": spy})
    result = ToolMgr.GetInstance().Execute(ToolCall("c1", "read", args), ("read",), RunContext())
    assert result.status == "invalid" and result.effect == "none"
    assert not spy.calls


def test_definitions_and_dispatch_share_allowed_set_and_frozen_schema():
    spy = SpyToolHandler()
    ToolMgr.Initialize({"read": ToolDefinition("read", "read", SCHEMA)}, {"read": spy})
    mgr = ToolMgr.GetInstance()
    assert [d.name for d in mgr.Definitions(("read",))] == ["read"]
    assert mgr.Definitions(()) == ()
    args = {"items": [{"name": "read", "count": 2}]}
    assert mgr.Execute(ToolCall("ok", "read", args), ("read",), RunContext()).status == "ok"
    assert mgr.Execute(ToolCall("deny", "read", args), (), RunContext()).status == "denied"
    assert mgr.Execute(ToolCall("bad", "missing", args), ("read",), RunContext()).status == "invalid"
    assert len(spy.calls) == 1
    with pytest.raises(TypeError):
        mgr.Definitions(("read",))[0].parameters["type"] = "string"


def test_invalid_schema_fails_startup_without_publishing_registry():
    with pytest.raises(DomainError) as error:
        ToolMgr.Initialize({"read": ToolDefinition("read", "read", {"type": "nonsense"})}, {"read": SpyToolHandler()})
    assert error.value.code == "CONFIG_INVALID"
    with pytest.raises(DomainError) as error:
        ToolMgr.GetInstance()
    assert error.value.code == "NOT_READY"


def test_unknown_exception_after_side_effect_is_truthful_and_sanitized():
    effects = []
    def fail(args, context):
        effects.append("written")
        raise RuntimeError("SECRET_TOKEN")
    spy = SpyToolHandler(fail)
    ToolMgr.Initialize({"write": ToolDefinition("write", "write", {"type": "object"})}, {"write": spy})
    result = ToolMgr.GetInstance().Execute(ToolCall("c", "write"), ("write",), RunContext())
    assert effects == ["written"] and result.status == "unknown" and result.effect == "unknown"
    assert "SECRET_TOKEN" not in result.content


from funagent.tool import ToolFailure
from funagent.tool.workspace import ReadWorkspaceHandler
from dataclasses import replace

READ_SCHEMA = {"type": "object", "required": ["path"], "additionalProperties": False,
               "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}}}


def read_registry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("workspace contents", encoding="utf8")
    ToolMgr.Initialize({"read_workspace": ToolDefinition("read_workspace", "Read file", READ_SCHEMA)},
                       {"read_workspace": ReadWorkspaceHandler})
    return ToolMgr.GetInstance(), RunContext(workspace=str(workspace), access=frozenset({"workspace.read"}))


def test_actual_workspace_read_and_no_access_override(tmp_path):
    mgr, context = read_registry(tmp_path)
    result = mgr.Execute(ToolCall("read", "read_workspace", {"path": "note.txt"}), ("read_workspace",), context)
    assert result.status == "ok" and result.content == "workspace contents" and result.effect == "none"
    no_access = replace(context, access=frozenset())
    assert mgr.Execute(ToolCall("deny", "read_workspace", {"path": "note.txt"}), ("read_workspace",), no_access).status == "denied"
    forged = {"path": "note.txt", "workspace": str(tmp_path), "access": ["workspace.read"]}
    assert mgr.Execute(ToolCall("forge", "read_workspace", forged), ("read_workspace",), no_access).status == "invalid"


@pytest.mark.parametrize("path", ["../secret.txt", "sub/../../secret.txt", "C:/Windows/win.ini", "note.txt:stream", "\\server/share/file"])
def test_workspace_escape_never_opens_file(tmp_path, monkeypatch, path):
    mgr, context = read_registry(tmp_path)
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observe)
    assert mgr.Execute(ToolCall("c", "read_workspace", {"path": path}), ("read_workspace",), context).status == "denied"
    assert not opened


def test_absolute_and_link_escape_are_denied(tmp_path):
    import os
    import subprocess
    mgr, context = read_registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf8")
    assert mgr.Execute(ToolCall("absolute", "read_workspace", {"path": str(secret)}), ("read_workspace",), context).status == "denied"
    link = Path(context.workspace) / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    try:
        result = mgr.Execute(ToolCall("link", "read_workspace", {"path": "linked/secret.txt"}), ("read_workspace",), context)
        assert result.status == "denied" and result.effect == "none"
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


@pytest.mark.parametrize("kind", ["missing", "directory", "large", "binary"])
def test_workspace_read_failure_is_ordinary_no_side_effect(tmp_path, kind):
    mgr, context = read_registry(tmp_path)
    target = Path(context.workspace) / kind
    if kind == "directory":
        target.mkdir()
    elif kind == "large":
        target.write_bytes(b"x" * 65537)
    elif kind == "binary":
        target.write_bytes(b"\xff\xfe")
    result = mgr.Execute(ToolCall("c", "read_workspace", {"path": kind}), ("read_workspace",), context)
    assert result.status in {"error", "invalid"} and result.effect == "none"


@pytest.mark.parametrize("effect, status", [("none", "error"), ("known", "error"), ("unknown", "unknown")])
def test_driver_can_confirm_effect_on_failure(effect, status):
    effects = []
    def fail(args, context):
        if effect != "none":
            effects.append("applied")
        raise ToolFailure("BACKEND_FAILED", effect=effect)
    ToolMgr.Initialize({"write": ToolDefinition("write", "write", {"type": "object"})}, {"write": SpyToolHandler(fail)})
    result = ToolMgr.GetInstance().Execute(ToolCall("c", "write"), ("write",), RunContext())
    assert result.status == status and result.effect == effect
    assert effects == ([] if effect == "none" else ["applied"])


@pytest.mark.parametrize("change", ["schema", "remote_ref", "approval", "timeout"])
def test_p3_invalid_config_has_no_partial_snapshot(conf_dir, change):
    from funagent.conf import ConfMgr
    tool = conf_dir / "tool.conf"
    if change == "schema":
        tool.write_text(tool.read_text().replace('type = "object"', 'type = "nonsense"'))
    elif change == "remote_ref":
        tool.write_text(tool.read_text().replace('type = "object"', 'type = "object"\n"$ref" = "https://invalid.example/schema"'))
    elif change == "approval":
        tool.write_text(tool.read_text().replace('handler = "echo"', 'handler = "echo"\nrequires_approval = "yes"'))
    else:
        serv = conf_dir / "serv.conf"
        serv.write_text(serv.read_text() + "\ninteraction_timeout = 0\n")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "CONFIG_INVALID"
    with pytest.raises(DomainError) as error:
        ConfMgr.GetInstance()
    assert error.value.code == "NOT_READY"
