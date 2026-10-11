"""Public imports share bindings without loading drivers or changing ownership."""

import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.frame.contracts import DomainError


BINDINGS = [
    ("api", "DiagnosticsMgr"),
    ("memory", "MemoryMgr"),
    ("memory", "MemoryOperationsMgr"),
    ("model", "ModelRuntime"),
    ("persistence", "DbMgr"),
    ("task", "TaskMgr"),
    ("tool", "ToolMgr"),
]

MODULE_NAMES = {
    "DiagnosticsMgr": "diagnostics_mgr",
    "MemoryMgr": "memory_mgr",
    "MemoryOperationsMgr": "memory_operations_mgr",
    "ModelRuntime": "model_runtime",
    "DbMgr": "db_mgr",
    "TaskMgr": "task_mgr",
    "ToolMgr": "tool_mgr",
}


def manager(domain, name):
    public = getattr(importlib.import_module(f"src.{domain}"), name)
    concrete = getattr(importlib.import_module(f"src.{domain}.{MODULE_NAMES[name]}"), name)
    assert public is concrete
    return public


def assert_unbound(binding):
    with pytest.raises(DomainError) as error:
        binding.GetInstance()
    assert error.value.code == "NOT_READY"


@pytest.mark.parametrize("domain,name", BINDINGS)
def test_package_and_module_share_one_binding(domain, name, monkeypatch):
    binding = manager(domain, name)
    monkeypatch.setattr(binding, "_instance", None)
    closed = []
    service = SimpleNamespace(Close=lambda: closed.append(True),
                              Stop=lambda: closed.append(True), IsStopped=lambda: True)
    assert_unbound(binding)
    binding.Initialize(service)
    public = getattr(importlib.import_module(f"src.{domain}"), name)
    assert public.GetInstance() is service
    with pytest.raises(DomainError) as error:
        public.Initialize(object())
    assert error.value.code == "ALREADY_INITIALIZED"
    public.Close()
    binding.Close()
    assert closed == [True]
    assert_unbound(binding)


@pytest.mark.parametrize("domain,name", [item for item in BINDINGS if item[0] != "task"])
def test_close_failure_releases_nonworker_binding(domain, name, monkeypatch):
    binding = manager(domain, name)
    monkeypatch.setattr(binding, "_instance", None)

    def fail():
        raise RuntimeError("injected close failure")

    binding.Initialize(SimpleNamespace(Close=fail))
    with pytest.raises(RuntimeError, match="injected close failure"):
        binding.Close()
    assert_unbound(binding)


@pytest.mark.parametrize("stopped", [False, True])
def test_task_close_failure_retains_only_a_live_worker(stopped, monkeypatch):
    binding = manager("task", "TaskMgr")
    monkeypatch.setattr(binding, "_instance", None)

    def fail():
        raise RuntimeError("injected drain failure")

    worker = SimpleNamespace(Stop=fail, IsStopped=lambda: stopped)
    binding.Initialize(worker)
    with pytest.raises(RuntimeError, match="injected drain failure"):
        binding.Close()
    if stopped:
        assert_unbound(binding)
    else:
        assert binding.GetInstance() is worker
        worker.Stop = lambda: None
        worker.IsStopped = lambda: True
        binding.Close()
        assert_unbound(binding)


@pytest.mark.parametrize("domain,name", [
    ("memory", "MemoryMgr"), ("model", "ModelRuntime"), ("tool", "ToolMgr"),
])
def test_optional_close_is_not_required(domain, name, monkeypatch):
    binding = manager(domain, name)
    monkeypatch.setattr(binding, "_instance", None)
    binding.Initialize(object())
    binding.Close()
    assert_unbound(binding)


def test_cold_public_imports_do_not_load_runtime_or_optional_drivers():
    # A fresh interpreter is required: other tests intentionally load these drivers.
    code = '''
import importlib
import sys
from src.frame.contracts import DomainError

for domain in ("app", "frame", "api", "memory", "model", "persistence", "task", "tool"):
    package = importlib.import_module("src." + domain)
    for name in getattr(package, "__all__", ()):
        getattr(package, name)
        if name.endswith("Mgr") or name == "ModelRuntime":
            try:
                getattr(package, name).GetInstance()
            except DomainError as error:
                assert error.code == "NOT_READY"
            else:
                raise AssertionError("Import bound a service: " + name)

for name in (
    "src.app.application", "src.api.http_server", "src.api.diagnostic_service",
    "src.model.runtime", "src.model.openai_chat", "src.model.factory",
    "src.memory.memory_manager", "src.memory.sqlite", "src.memory.supabase", "src.memory.embedding",
    "src.persistence.sqlite", "src.task.task_queue", "src.task.task_process",
    "src.tool.tool_registry", "src.tool.factory", "src.tool.backends.mcp",
    "openai", "httpx", "mcp",
):
    assert name not in sys.modules, name
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
