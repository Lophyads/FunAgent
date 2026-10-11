"""Offline regression coverage for application assembly and executable entries."""

from io import StringIO
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event, enumerate as threads
import time
from urllib.request import urlopen

import pytest

from src.api import DiagnosticsMgr
from src.api.cli_controller import CLIController
from src.app import application
from src.app.main import main
from src.frame import global_server
from src.frame.conf_mgr import ConfMgr
from src.frame.contracts import DomainError
from src.frame.event_mgr import EventMgr
from src.frame.interaction_mgr import InteractionMgr
from src.frame.review_scheduler import ReviewScheduler
from src.memory import MemoryMgr, MemoryOperationsMgr
from src.model import ModelRuntime
from src.persistence import DbMgr
from src.task import TaskMgr
from src.tool import ToolMgr


MANAGERS = (ConfMgr, DiagnosticsMgr, EventMgr, InteractionMgr, ReviewScheduler,
            MemoryMgr, MemoryOperationsMgr, ModelRuntime, DbMgr, TaskMgr, ToolMgr)


@pytest.fixture
def conf(tmp_path):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    configs = {
        "model": '''adapters = ["demo"]
[providers.offline]
kind = "demo"
[profiles.main]
provider_id = "offline"
adapter_id = "demo"
model = "offline"
timeout = 2
''',
        "tool": '''[purposes]
main = ["echo"]
''',
        "memory": '''[policies.recall]
profile = "main"
[policies.review]
enabled = false
''',
        "log": '''[options]
level = "ERROR"
console = false
''',
        "serv": f'''[options]
mode = "cli"
capacity = 4
host = "127.0.0.1"
port = {port}
workspace = "."
database = "application.sqlite3"
model_profile = "main"
task_timeout = 3
''',
    }
    for name, content in configs.items():
        (tmp_path / f"{name}.conf").write_text(content, encoding="utf8")
    yield tmp_path
    application.Stop()


def assert_stopped():
    assert application.GetHttpServer() is None
    assert not any(thread.name.startswith("funagent-") for thread in threads())
    for manager in MANAGERS:
        with pytest.raises(DomainError) as error:
            manager.GetInstance()
        assert error.value.code == "NOT_READY"


def command(line):
    output = StringIO()
    CLIController.Execute(line, output)
    return output.getvalue().strip()


def finish(task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = TaskMgr.GetInstance().Get(task_id)
        if task.status not in {"queued", "running"}:
            return task
        Event().wait(0.01)
    pytest.fail("Offline task did not finish")


@pytest.mark.parametrize("mode", ["cli", "web", "both"])
def test_new_and_legacy_entries_share_tasks_and_restart(conf, mode):
    file = conf / "serv.conf"
    file.write_text(file.read_text(encoding="utf8").replace('mode = "cli"', f'mode = "{mode}"'), encoding="utf8")
    assert application.Start(conf) is TaskMgr.GetInstance()
    with pytest.raises(DomainError) as error:
        global_server.Start(conf)
    assert error.value.code == "ALREADY_INITIALIZED"
    session = command("new module layout")
    task_id = command(f"submit {session} tool: shared binding")
    task = finish(task_id)
    assert task.status == "completed"
    assert task.result.final == "shared binding"
    if mode != "cli":
        server = application.GetHttpServer()
        with urlopen(server.url + "/diagnostics/status", timeout=3) as response:
            status = json.load(response)
        assert status["ready"] and status["mode"] == mode
        with urlopen(server.url + f"/tasks/{task_id}", timeout=3) as response:
            assert json.load(response)["result"]["final"] == "shared binding"
        with urlopen(server.url + "/", timeout=3) as response:
            assert response.status == 200 and b"<html" in response.read()
    global_server.Stop()
    assert_stopped()

    # Reverse the entries on restart and verify the durable session is reused.
    global_server.Start(conf)
    assert session in command("sessions")
    assert "shared binding" in command(f"history {session}")
    second = finish(command(f"submit {session} hello"))
    assert second.status == "completed" and second.result.final == "Demo: hello"
    application.Stop()
    assert_stopped()


def test_startup_failure_rolls_back_and_allows_restart(conf, monkeypatch):
    monkeypatch.delenv("FUNAGENT_LAYOUT_TEST", raising=False)
    (conf / ".env").write_text("FUNAGENT_LAYOUT_TEST=local-default\n", encoding="utf8")
    original = application.CreateModel

    def fail(*args, **kwargs):
        raise DomainError("DRIVER_UNAVAILABLE", "injected startup failure")

    monkeypatch.setattr(application, "CreateModel", fail)
    with pytest.raises(DomainError) as error:
        global_server.Start(conf)
    assert error.value.code == "DRIVER_UNAVAILABLE"
    assert_stopped()
    import os
    assert "FUNAGENT_LAYOUT_TEST" not in os.environ
    monkeypatch.setattr(application, "CreateModel", original)
    application.Start(conf)
    assert os.environ["FUNAGENT_LAYOUT_TEST"] == "local-default"
    assert finish(command(f'submit {command("new retry")} hello')).result.final == "Demo: hello"
    global_server.Stop()
    assert_stopped()
    assert "FUNAGENT_LAYOUT_TEST" not in os.environ


def test_check_config_constructs_no_drivers_and_releases_bindings(conf, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("Configuration validation constructed a driver")

    monkeypatch.setattr(application, "CreatePersistence", unexpected)
    assert global_server.main is main
    assert global_server.Initialize is application.Initialize
    assert global_server.Start is application.Start
    assert global_server.Stop is application.Stop
    assert main(["--conf-dir", str(conf), "--check-config"]) == 0
    assert capsys.readouterr().out == "Configuration valid.\n"
    assert not (conf / "application.sqlite3").exists()
    assert_stopped()


@pytest.mark.parametrize("validate_only", [False, True])
def test_python_module_entry(conf, validate_only):
    args = [sys.executable, "-m", "src", "--conf-dir", str(conf)]
    if validate_only:
        args.append("--check-config")
    result = subprocess.run(args, input="quit\n", capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=10)
    assert result.returncode == 0, result.stderr
    if validate_only:
        assert result.stdout == "Configuration valid.\n"
        assert not (conf / "application.sqlite3").exists()
    else:
        assert (conf / "application.sqlite3").is_file()


def test_cli_reports_invalid_configuration_and_can_retry(conf, capsys):
    (conf / "model.conf").unlink()
    assert main(["--conf-dir", str(conf), "--check-config"]) == 1
    assert "CONFIG_MISSING" in capsys.readouterr().err
    assert_stopped()
