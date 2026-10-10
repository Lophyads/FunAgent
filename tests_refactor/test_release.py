"""P7 delivery boundaries: startup rollback, drain ownership and distribution."""
from pathlib import Path
from shutil import copyfile
from threading import Event, Thread, enumerate as threads
import builtins
import tomllib

import pytest

from funagent import global_server
from funagent.conf import ConfMgr
from funagent.contracts import DomainError, SessionCreate, TurnRequest
from funagent.interaction import InteractionMgr
from funagent.memory import MemoryMgr
from funagent.model import ModelRuntime
from funagent.model.demo import DemoProvider
from funagent.persistence import DbMgr
from funagent.review import ReviewScheduler
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent.tool import ToolMgr
from test_task_flow import wait_task

ROOT = Path(__file__).parents[1]
RESOURCES = (ConfMgr, DbMgr, ModelRuntime, ToolMgr, InteractionMgr, MemoryMgr, TaskMgr, ReviewScheduler)


def assert_closed():
    for resource in RESOURCES:
        with pytest.raises(DomainError) as error:
            resource.GetInstance()
        assert error.value.code == "NOT_READY"
    assert not any(t.name.startswith("funagent-") for t in threads())


def test_distribution_has_only_new_entry_and_required_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf8"))
    assert project["project"]["scripts"] == {"funagent": "funagent.global_server:main"}
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/funagent"]
    assert project["project"]["dependencies"] == ["jsonschema>=4.18,<5"]
    assert set(project["project"]["optional-dependencies"]) == {"openai", "dev"}
    assert "markers" not in project["tool"]["pytest"]["ini_options"]
    assert "asyncio_mode" not in project["tool"]["pytest"]["ini_options"]


def test_old_source_config_schema_and_tests_are_removed():
    assert {p.name for p in (ROOT / "src").iterdir() if p.is_dir() and p.name != "__pycache__"} == {"funagent"}
    for path in ("tests", "agentcore.toml", "agentcore.toml.example", "sql", "examples/custom_tool.py", "examples/interaction_host.py"):
        assert not (ROOT / path).exists(), path


@pytest.mark.parametrize("fault", ["config", "provider", "tools", "database", "memory", "review_worker"])
def test_startup_failure_rolls_back_owned_resources_and_restarts(cli_conf, monkeypatch, fault):
    owned, closed = [], []
    class Provider(DemoProvider):
        def __init__(self, options):
            owned.append("provider")
        def Close(self):
            closed.append("provider")
    def fail(*args, **kwargs):
        raise RuntimeError("SECRET_TOKEN")
    copyfile(ROOT / "examples/refactor-review/memory.conf", cli_conf / "memory.conf")
    with monkeypatch.context() as patch:
        patch.setitem(global_server.PROVIDERS, "demo", Provider)
        if fault == "config":
            (cli_conf / "model.conf").unlink()
        elif fault == "provider":
            model = cli_conf / "model.conf"
            model.write_text(model.read_text().replace('[profiles.fallback]\nprovider_id = "fake"', '[profiles.fallback]\nprovider_id = "other"') + '\n[providers.other]\nkind = "broken"\n')
            patch.setitem(global_server.PROVIDERS, "broken", fail)
        elif fault == "tools":
            patch.setattr(ToolMgr, "Initialize", fail)
        elif fault == "database":
            patch.setattr(global_server, "SQLiteStore", fail)
        elif fault == "memory":
            patch.setattr(MemoryMgr, "Initialize", fail)
        else:
            original = Thread.start
            def start(self):
                if self.name == "funagent-review":
                    fail()
                return original(self)
            patch.setattr(Thread, "start", start)
        with pytest.raises(DomainError) as error:
            global_server.Start(cli_conf)
        assert "SECRET_TOKEN" not in str(error.value)
        assert_closed()
        assert closed == owned
    copyfile(ROOT / "examples/refactor-cli/model.conf", cli_conf / "model.conf")
    global_server.Start(cli_conf)
    global_server.Stop()
    assert_closed()


@pytest.mark.parametrize("selected", [False, True])
def test_only_selected_optional_sdk_is_required(cli_conf, monkeypatch, selected):
    original_import = builtins.__import__
    def without_sdk(name, *args, **kwargs):
        if name in {"openai", "httpx"}:
            raise ImportError("optional driver intentionally absent")
        return original_import(name, *args, **kwargs)
    if selected:
        copyfile(ROOT / "examples/refactor-openai/model.conf", cli_conf / "model.conf")
    else:
        model = cli_conf / "model.conf"
        model.write_text(model.read_text() + '\n[providers.unused]\nkind = "openai"\n[profiles.unused]\nprovider_id = "unused"\nadapter_id = "demo"\nmodel = "unused"\ntimeout = 1.0\nmax_retries = 0\n')
    monkeypatch.setattr(builtins, "__import__", without_sdk)
    if selected:
        with pytest.raises(DomainError) as error:
            global_server.Start(cli_conf)
        assert error.value.code == "DRIVER_UNAVAILABLE"
    else:
        global_server.Start(cli_conf)
        global_server.Stop()
    assert_closed()


def test_startup_close_error_does_not_skip_other_cleanup(cli_conf, monkeypatch):
    original_store = global_server.SQLiteStore
    def store(path):
        instance = original_store(path)
        close = instance.Close
        def fail_close():
            close()
            raise RuntimeError("SECRET_CLOSE")
        instance.Close = fail_close
        return instance
    with monkeypatch.context() as patch:
        patch.setattr(global_server, "SQLiteStore", store)
        patch.setattr(DbMgr, "Initialize", lambda store: (_ for _ in ()).throw(RuntimeError("SECRET_BIND")))
        with pytest.raises(DomainError) as error:
            global_server.Start(cli_conf)
        assert error.value.code == "STARTUP_FAILED"
        assert "SECRET" not in str(error.value)
        assert_closed()
    global_server.Start(cli_conf)
    global_server.Stop()


def test_stop_signals_both_workers_before_waiting_and_keeps_borrowed_resources(cli_conf, monkeypatch):
    copyfile(ROOT / "examples/refactor-review/memory.conf", cli_conf / "memory.conf")
    main_entered, review_entered, release, closed, stopped = (Event() for _ in range(5))
    contexts, failures = {}, []
    class Provider(DemoProvider):
        def Send(self, request, context):
            text = request.payload.messages[-1].content or ""
            if context.purpose == "review" or (context.purpose == "main" and text == "block"):
                contexts[context.purpose] = context
                (review_entered if context.purpose == "review" else main_entered).set()
                assert release.wait(5)
            return super().Send(request, context)
        def Close(self):
            closed.set()
    monkeypatch.setitem(global_server.PROVIDERS, "demo", Provider)
    global_server.Start(cli_conf)
    manager = TaskMgr.GetInstance()
    session = SessionMgr.Create(SessionCreate())
    wait_task(manager, manager.Submit(TurnRequest(session.session_id, "hello")))
    assert review_entered.wait(3)
    task = manager.Submit(TurnRequest(session.session_id, "block"))
    assert main_entered.wait(3)
    def stop():
        try:
            global_server.Stop()
        except Exception as error:
            failures.append(error)
        finally:
            stopped.set()
    stopper = Thread(target=stop, name="test-stop")
    stopper.start()
    try:
        assert contexts["main"].cancel.wait(1)
        assert contexts["review"].cancel.wait(1), "review must be cancelled before joining main"
        with pytest.raises(DomainError) as error:
            manager.Submit(TurnRequest(session.session_id, "rejected"))
        assert error.value.code == "STOPPING"
        assert not stopped.is_set() and not closed.is_set()
        assert manager.Get(task).status == "running"
        for resource in (ConfMgr, DbMgr, ModelRuntime, MemoryMgr):
            resource.GetInstance()
    finally:
        release.set()
        stopper.join(5)
        assert not stopper.is_alive()
        global_server.Stop()
    assert not failures and closed.is_set()
    assert_closed()


@pytest.mark.parametrize("purpose", ["main", "review"])
def test_failed_worker_drain_retains_resources_until_retry(cli_conf, monkeypatch, purpose):
    entered, release, closed = Event(), Event(), Event()
    class Provider(DemoProvider):
        def Send(self, request, context):
            if context.purpose == purpose:
                entered.set()
                assert release.wait(5)
            return super().Send(request, context)
        def Close(self):
            closed.set()
    monkeypatch.setitem(global_server.PROVIDERS, "demo", Provider)
    if purpose == "review":
        copyfile(ROOT / "examples/refactor-review/memory.conf", cli_conf / "memory.conf")
    global_server.Start(cli_conf)
    manager = TaskMgr.GetInstance()
    session = SessionMgr.Create(SessionCreate())
    manager.Submit(TurnRequest(session.session_id, "hello"))
    assert entered.wait(3)
    try:
        with monkeypatch.context() as patch:
            worker = manager if purpose == "main" else ReviewScheduler.GetInstance()
            patch.setattr(worker, "Stop", lambda: (_ for _ in ()).throw(DomainError("DRAIN_FAILED", "worker still running")))
            with pytest.raises(DomainError):
                global_server.Stop()
            assert not closed.is_set(), "resources must remain owned while worker borrows them"
            outstanding = TaskMgr if purpose == "main" else ReviewScheduler
            for resource in (ConfMgr, DbMgr, ModelRuntime, MemoryMgr, outstanding):
                resource.GetInstance()
    finally:
        release.set()
        global_server.Stop()
    assert_closed()


def test_http_close_failure_retains_application_resources_for_retry(cli_conf, monkeypatch):
    from funagent.api.http import HttpServer
    serv = cli_conf / "serv.conf"
    serv.write_text(serv.read_text().replace('mode = "cli"', 'mode = "both"'))
    monkeypatch.setattr(global_server, "HttpServer", lambda host, port: HttpServer(host, 0))
    global_server.Start(cli_conf)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(global_server._http_server, "Close", lambda: (_ for _ in ()).throw(RuntimeError("SECRET_CLOSE")))
            with pytest.raises(DomainError) as error:
                global_server.Stop()
            assert error.value.code == "CLOSE_FAILED" and "SECRET" not in str(error.value)
            for resource in (ConfMgr, DbMgr, ModelRuntime, MemoryMgr):
                resource.GetInstance()
            with pytest.raises(DomainError) as error:
                session = SessionMgr.Create(SessionCreate())
                TaskMgr.GetInstance().Submit(TurnRequest(session.session_id, "stopping"))
            assert error.value.code == "STOPPING"
    finally:
        global_server.Stop()
    assert_closed()
