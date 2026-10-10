"""Build-artifact smoke in fresh environments, outside either original checkout."""
import argparse
from configparser import ConfigParser
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


INSTALLED_SMOKE = r'''import importlib.util
from io import StringIO
from pathlib import Path
import json
import socket
import sys
from threading import Event, enumerate as threads
import time
from urllib.request import Request, urlopen

import funagent
from funagent import global_server
from funagent.api.cli import CLIController
from funagent.conf import ConfMgr
from funagent.contracts import DomainError
from funagent.interaction import InteractionMgr
from funagent.memory import MemoryMgr
from funagent.model import ModelRuntime
from funagent.persistence import DbMgr
from funagent.review import ReviewScheduler
from funagent.task import TaskMgr
from funagent.tool import ToolMgr

conf, expected = map(Path, sys.argv[1:3])
assert Path(funagent.__file__).resolve().is_relative_to(expected.resolve())
assert importlib.util.find_spec("openai") is None
for legacy in ("agentcore", "gateway", "tools", "knowledge", "memory", "log"):
    assert importlib.util.find_spec(legacy) is None, legacy
original = (conf / "serv.conf").read_text()

def poll(read, ready):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        value = read()
        if ready(value):
            return value
        Event().wait(0.01)
    raise AssertionError("Smoke operation did not finish")

def command(line, answer=lambda prompt: "blue"):
    output = StringIO()
    CLIController.Execute(line, output, read_answer=answer)
    return output.getvalue().strip()

for mode in ("cli", "web", "both"):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    (conf / "serv.conf").write_text(original.replace('mode = "cli"', f'mode = "{mode}"').replace('port = 8080', f'port = {port}'))
    try:
        global_server.Start(conf)
        def http(method, route, data=None):
            body = json.dumps(data).encode() if data is not None else None
            request = Request(f"http://127.0.0.1:{port}" + route, data=body, method=method, headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        if mode == "cli":
            session = command("new installed cli")
            for text, expected_text, answer in (("hello", "Demo: hello", ""), ("tool: paired", "paired", ""), ("ask: color?", "blue", "blue"), ("read: note.txt", "installed workspace", "yes")):
                task = command(f"submit {session} {text}")
                result = command(f"wait {task}", lambda prompt: answer)
                assert "completed" in result and expected_text in result
            assert "paired" in command(f"history {session}")
            remembered = command(f"submit {session} remember: blue")
            assert "completed" in command(f"wait {remembered}")
        else:
            for asset in ("/", "/app.js", "/style.css"):
                with urlopen(f"http://127.0.0.1:{port}" + asset, timeout=3) as response:
                    assert response.status == 200 and response.read()
            session = http("POST", "/sessions", {"title": "installed " + mode})[1]["session_id"]
            def submit(text):
                status, value = http("POST", f"/sessions/{session}/tasks", {"text": text})
                assert status == 202
                return value["task_id"]
            def finish(task):
                return poll(lambda: http("GET", f"/tasks/{task}")[1], lambda value: value["status"] not in {"queued", "running"})
            for text, expected_text, answer in (("hello", "Demo: hello", None), ("tool: paired", "paired", None), ("ask: color?", "blue", "blue"), ("read: note.txt", "installed workspace", True)):
                task = submit(text)
                if answer is not None:
                    pending = poll(lambda: http("GET", f"/tasks/{task}/interactions")[1]["items"], bool)[0]
                    body = {"task_id": task, "run_id": pending["run_id"], "answer": answer}
                    assert http("POST", f"/interactions/{pending['interaction_id']}/answer", body)[0] == 200
                result = finish(task)
                assert result["status"] == "completed" and expected_text in result["result"]["final"]
            task = submit("ask: cancel me")
            poll(lambda: http("GET", f"/tasks/{task}/interactions")[1]["items"], bool)
            http("POST", f"/tasks/{task}/cancel", {})
            assert finish(task)["status"] == "cancelled"
            assert http("GET", f"/tasks/{task}/interactions")[1]["items"] == []
            if mode == "both":
                assert session in command("sessions")
                task = command(f"submit {session} shared cli")
                assert finish(task)["result"]["final"] == "Demo: shared cli"
            remembered = submit("remember: blue")
            assert finish(remembered)["status"] == "completed"
        poll(lambda: ReviewScheduler.GetInstance().GetStatus(session), lambda value: value["state"] == "completed" and value["pending_through"] is None)
        task = command(f"submit {session} memory: blue") if mode == "cli" else submit("memory: blue")
        result = command(f"wait {task}") if mode == "cli" else finish(task)["result"]["final"]
        assert "blue" in result and "Memory evidence" in result
    finally:
        global_server.Stop()
    for resource in (ConfMgr, DbMgr, ModelRuntime, ToolMgr, MemoryMgr, TaskMgr, ReviewScheduler, InteractionMgr):
        try:
            resource.GetInstance()
        except DomainError as error:
            assert error.code == "NOT_READY"
        else:
            raise AssertionError("Resource left initialized")
    assert not any(t.name.startswith("funagent-") for t in threads())
    print(f"{mode}: reply/tools/human/history/memory and cleanup passed")
print("Imported installed package:", funagent.__file__)
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--node")
    parser.add_argument("--playwright")
    parser.add_argument("--browser")
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    browser_args = (args.node, args.playwright, args.browser, args.artifact_dir)
    if any(browser_args) and not all(browser_args):
        parser.error("Browser smoke needs --node, --playwright, --browser and --artifact-dir")
    wheel, sdist = args.wheel.resolve(), args.sdist.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(name.startswith("funagent/") or ".dist-info/" in name for name in names)
        entry = next(name for name in names if name.endswith("/entry_points.txt"))
        entries = ConfigParser()
        entries.read_string(archive.read(entry).decode())
        assert dict(entries["console_scripts"]) == {"funagent": "funagent.global_server:main"}
        assert all("funagent/api/web/" + name in names for name in ("index.html", "app.js", "style.css"))
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PYTHONUTF8"] = "1"
    def run(command, cwd, **kwargs):
        return subprocess.run(command, cwd=cwd, env=env, check=True, timeout=60, **kwargs)
    with tempfile.TemporaryDirectory(prefix="funagent-release-") as directory:
        temporary = Path(directory)
        with tarfile.open(sdist) as archive:
            members = archive.getnames()
            assert not any("/src/agentcore/" in name or "/tests/" in name or "/sql/" in name for name in members)
            archive.extractall(temporary / "source", filter="data")
        source = next((temporary / "source").iterdir())
        for kind in ("wheel", "editable"):
            venv = temporary / (kind + "-env")
            run([args.uv, "venv", "--python", args.python, str(venv)], temporary)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = [args.uv, "pip", "install", "--python", str(python)]
            if args.offline:
                command.append("--offline")
            command += [str(wheel)] if kind == "wheel" else ["-e", str(source)]
            run(command, temporary)
            work = temporary / (kind + "-unrelated-cwd")
            work.mkdir()
            (work / "note.txt").write_text("installed workspace", encoding="utf8")
            conf = temporary / (kind + "-conf")
            shutil.copytree(source / "examples/refactor-review", conf)
            entry = venv / ("Scripts/funagent.exe" if os.name == "nt" else "bin/funagent")
            run([str(entry), "--conf-dir", str(conf), "--check-config"], work)
            run([str(entry), "--conf-dir", str(conf)], work, input="quit\n", text=True)
            missing = subprocess.run([str(entry), "--conf-dir", str(temporary / "missing")], cwd=work, env=env, capture_output=True, text=True, timeout=10)
            assert missing.returncode == 1 and "CONFIG_MISSING" in missing.stderr
            expected = venv if kind == "wheel" else source / "src"
            run([str(python), "-I", "-c", INSTALLED_SMOKE, str(conf), str(expected)], work)
            if kind == "wheel" and all(browser_args):
                run([str(python), "-I", str(source / "scripts/refactor_browser_smoke.py"), "--installed", "--node", args.node, "--playwright", args.playwright, "--browser", args.browser, "--artifact-dir", str(args.artifact_dir.resolve())], work)
            print(f"{kind}: fresh install, console entry and cli/web/both passed", flush=True)
    print("Wheel/sdist contents verified; temporary environments/databases removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
