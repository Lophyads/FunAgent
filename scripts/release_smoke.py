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
import os
import logging
import socket
import sys
from threading import Event, enumerate as threads
import time
from urllib.request import Request, urlopen

import src
from src.frame import global_server
from src.api.cli import CLIController
from src.frame.conf import ConfMgr
from src.frame.contracts import DomainError
from src.frame.interaction import InteractionMgr
from src.memory import MemoryMgr
from src.model import ModelRuntime
from src.persistence import DbMgr
from src.frame.review import ReviewScheduler
from src.task import TaskMgr
from src.tool import ToolMgr
from src.tool.contracts import ToolSelectionRequest
from src.frame.contracts import RunContext
from src.memory.deployment import SupabaseSQL
from src.frame.distribution import AvailableExamples, CopyExample, ExampleFiles

conf, expected = map(Path, sys.argv[1:3])
assert Path(src.__file__).resolve().is_relative_to(expected.resolve())
assert {"refactor-cli", "refactor-tools", "refactor-mcp", "refactor-supabase"} <= set(AvailableExamples())
exported = CopyExample("refactor-cli", Path.cwd() / "exported-config")
assert (exported / "model.conf").read_bytes() == ExampleFiles("refactor-cli")["model.conf"]
assert importlib.util.find_spec("openai") is None
assert importlib.util.find_spec("httpx") is None
assert "src.memory.supabase" not in sys.modules and "src.memory.embedding" not in sys.modules
deployment = SupabaseSQL()
assert "CREATE TABLE funagent_private.memory_records" in deployment
assert all("public.funagent_memory_" + name in deployment for name in ("list", "search", "add", "replace", "remove"))
# Only selecting the optional HTTP driver requires its dependency.
from src.memory.embedding import HTTPEmbedder
os.environ["FUNAGENT_MISSING_HTTP_TEST"] = "local-dummy"
try:
    HTTPEmbedder({"model": "local", "dimensions": 3, "endpoint": "https://unused.invalid/embeddings", "credential_env": "FUNAGENT_MISSING_HTTP_TEST"})
except DomainError as error:
    assert error.code == "DRIVER_UNAVAILABLE"
else:
    raise AssertionError("Selected optional driver did not reject its missing dependency")
finally:
    os.environ.pop("FUNAGENT_MISSING_HTTP_TEST")
for legacy in ("funagent", "agentcore", "gateway", "tools", "knowledge", "memory", "log"):
    assert importlib.util.find_spec(legacy) is None, legacy
original = (conf / "serv.conf").read_text()
(conf / ".env").write_text("FUNAGENT_INSTALLED_SECRET=installed-secret\nFUNAGENT_INSTALLED_PRIORITY=dotenv-default\n", encoding="utf8")
(Path.cwd() / ".env").write_text("FUNAGENT_INSTALLED_SECRET=wrong-cwd\n", encoding="utf8")
os.environ["FUNAGENT_INSTALLED_PRIORITY"] = "explicit-value"
(conf / "log.conf").write_text('[options]\nlevel = "INFO"\nfile = "installed.log"\nerror_file = "error.log"\nmax_bytes = 2048\nbackup_count = 2\n', encoding="utf8")

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
        selection = ToolMgr.GetInstance().Select(ToolSelectionRequest(toolsets=("demo",),
            enable=("read_workspace",), disable=("read_workspace",)), RunContext())
        assert selection.allowed_names == ("echo",) and selection.registry_id
        assert selection.definitions[0].parameters["properties"]["value"]["type"] == "string"
        assert os.environ["FUNAGENT_INSTALLED_SECRET"] == "installed-secret"
        assert os.environ["FUNAGENT_INSTALLED_PRIORITY"] == "explicit-value"
        logging.getLogger("funagent.smoke").warning("credential %s", os.environ["FUNAGENT_INSTALLED_SECRET"])
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
                    value = {"answers": [{"question_id": "answer", "value": answer}]} if pending["kind"] == "clarify" else answer
                    body = {"task_id": task, "run_id": pending["run_id"], "answer": value}
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
    assert "FUNAGENT_INSTALLED_SECRET" not in os.environ
    assert os.environ["FUNAGENT_INSTALLED_PRIORITY"] == "explicit-value"
    assert "installed-secret" not in (conf / "installed.log").read_text(encoding="utf8")
    print(f"{mode}: reply/tools/human/history/memory and cleanup passed")
print("Imported installed package:", src.__file__)
'''


INSTALLED_CONTRACTS = r'''from pathlib import Path
import sys
import src
import pytest
from src.memory.deployment import SupabaseSQL
assert Path(src.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert "CREATE TABLE funagent_private.memory_records" in SupabaseSQL()
raise SystemExit(pytest.main(sys.argv[2:]))
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--tdd08-contracts", action="store_true", help="Install memory HTTP extras and run contracts against the installed package")
    parser.add_argument("--tdd09-contracts", action="store_true", help="Install MCP/Web extras and run real tool IO contracts against the installed package")
    parser.add_argument("--tdd10-contracts", action="store_true", help="Run durable review trigger/executor contracts against the installed package")
    parser.add_argument("--tdd11-contracts", action="store_true", help="Run installed submission, management, diagnostics/log/export contracts")
    parser.add_argument("--tdd12-contracts", action="store_true", help="Run the complete offline behavioral suite against each installed distribution")
    parser.add_argument("--contract-artifact-dir", type=Path, help="Directory for installed memory contract JUnit reports")
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
        assert all(name.startswith("src/") or ".dist-info/" in name for name in names)
        assert not any("__pycache__/" in name or name.endswith(".pyc") for name in names)
        assert "src/frame/__init__.py" in names
        assert all(name in names for name in ("src/frame/environment.py", "src/frame/logging.py", "src/frame/lifecycle.py",
                                             "src/persistence/factory.py", "src/model/factory.py", "src/memory/factory.py", "src/tool/factory.py"))
        assert all(name in names for name in ("src/memory/planner.py", "src/memory/search.py", "src/memory/prompt.py", "src/memory/prepare.py"))
        assert not any(Path(name).name.startswith(".env") or any(suffix in Path(name).name for suffix in (".sqlite", ".db", ".log")) for name in names)
        metadata = next(name for name in names if name.endswith("/METADATA"))
        assert "Requires-Dist: python-dotenv" in archive.read(metadata).decode()
        entry = next(name for name in names if name.endswith("/entry_points.txt"))
        entries = ConfigParser()
        entries.read_string(archive.read(entry).decode())
        assert dict(entries["console_scripts"]) == {"funagent": "src.frame.global_server:main"}
        assets = ('index.html','api.js','app.js','store.js','style.css','tokens.css','overview.css','favicon.svg',
                  'components/chat.js','components/ui.js','pages/overview.js','pages/overview-model.js',
                  'pages/memory.js','pages/data.js','pages/catalog.js')
        assert all("src/api/web/" + name in names for name in assets)
        assert {"src/frame/events.py", "src/model/streaming.py"} <= set(names)
        assert {"src/memory/embedding.py", "src/memory/supabase.py", "src/memory/transport.py", "src/memory/deployment.py"} <= set(names)
        migrations = [name for name in names if name.startswith("src/memory/assets/supabase/migrations/") and name.endswith(".sql")]
        assert migrations
        assert any(b"CREATE TABLE funagent_private.memory_records" in archive.read(name) for name in migrations)
        assert all(f'Provides-Extra: {extra}' in archive.read(metadata).decode() for extra in ("embedding", "supabase", "supabase-test"))
        assert {"src/tool/backends/mcp.py", "src/tool/backends/terminal.py", "src/tool/backends/process_worker.py",
                "src/tool/backends/computer.py", "src/tool/backends/web.py", "src/memory/mcp_query.py",
                "src/tool/builtins/file_tool.py", "src/tool/builtins/computer_use_tool.py"} <= set(names)
        assert all(f'Provides-Extra: {extra}' in archive.read(metadata).decode() for extra in ("mcp", "web"))
        assert {"src/memory/operations.py", "src/api/management.py", "src/api/diagnostics.py",
                "src/api/logs.py", "src/api/export.py", "src/api/validation.py"} <= set(names)
        assert b'confirmAction' in archive.read('src/api/web/api.js')
        assert "src/frame/distribution.py" in names
        assert all(f"src/frame/assets/examples/{example}/{name}.conf" in names
                   for example in ("refactor-cli", "refactor-tools", "refactor-mcp", "refactor-supabase")
                   for name in ("model", "tool", "memory", "log", "serv"))
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(name, None)
    env["PYTHONUTF8"] = "1"
    def run(command, cwd, **kwargs):
        return subprocess.run(command, cwd=cwd, env=env, check=True, timeout=kwargs.pop("timeout", 60), **kwargs)
    with tempfile.TemporaryDirectory(prefix="funagent-release-") as directory:
        temporary = Path(directory)
        with tarfile.open(sdist) as archive:
            members = archive.getnames()
            prefix = members[0].split('/')[0]
            required = {f'{prefix}/examples/{example}/{name}.conf'
                        for example in ('refactor-cli', 'refactor-review', 'refactor-openai', 'refactor-web', 'refactor-supabase', 'refactor-tools', 'refactor-mcp')
                        for name in ('model', 'tool', 'memory', 'log', 'serv')}
            assert required <= set(members), 'sdist is missing required example configurations'
            assert not any(Path(name).name.startswith(".env") or any(suffix in Path(name).name for suffix in (".sqlite", ".db", ".log")) for name in members)
            assert not any("/src/agentcore/" in name or "/tests/" in name for name in members)
            assert f'{prefix}/sql/supabase/README.md' in members
            assert all(f'{prefix}/{name}' in members for name in ("hatch_build.py", "docs/migration-backlog.md",
                       "docs/refactor-migration-plan.md", "docs/tdd-refactor-migration-plan.md",
                       "architecture/lean-design.md", "architecture/module-api-design.md"))
            assert all(f'{prefix}/{name}' in members for name in migrations)
            archive.extractall(temporary / "source", filter="data")
        source = next((temporary / "source").iterdir())
        constraints = temporary / "locked-constraints.txt"
        export = [args.uv, "export", "--locked", "--all-extras", "--no-emit-project", "--no-hashes", "--format", "requirements.txt", "--output-file", str(constraints)]
        if args.offline:
            export.append("--offline")
        run(export, source, capture_output=True)
        for kind in ("wheel", "sdist"):
            venv = temporary / (kind + "-env")
            run([args.uv, "venv", "--python", args.python, str(venv)], temporary)
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
            if args.offline:
                command.append("--offline")
            command += [str(wheel if kind == "wheel" else sdist)]
            run(command, temporary)
            work = temporary / (kind + "-unrelated-cwd")
            work.mkdir()
            (work / "note.txt").write_text("installed workspace", encoding="utf8")
            conf = temporary / (kind + "-conf")
            shutil.copytree(source / "examples/refactor-review", conf)
            entry = venv / ("Scripts/funagent.exe" if os.name == "nt" else "bin/funagent")
            run([str(entry), "--conf-dir", str(conf), "--check-config"], work)
            run([str(entry), "--conf-dir", str(conf)], work, input="quit\n", text=True)
            run([str(python), "-I", "-m", "src", "--conf-dir", str(conf), "--check-config"], work)
            run([str(python), "-I", "-m", "src", "--conf-dir", str(conf)], work, input="quit\n", text=True)
            missing = subprocess.run([str(entry), "--conf-dir", str(temporary / "missing")], cwd=work, env=env, capture_output=True, text=True, timeout=10)
            assert missing.returncode == 1 and "CONFIG_MISSING" in missing.stderr
            expected = venv
            run([str(python), "-I", "-c", INSTALLED_SMOKE, str(conf), str(expected)], work)
            if all(browser_args):
                run([str(python), "-I", str(source / "scripts/refactor_browser_smoke.py"), "--installed", "--node", args.node, "--playwright", args.playwright, "--browser", args.browser, "--artifact-dir", str(args.artifact_dir.resolve() / kind)], work, timeout=180)
            print(f"{kind}: fresh install, console entry and cli/web/both passed", flush=True)
            if args.tdd12_contracts:
                extra_command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
                if args.offline:
                    extra_command.append("--offline")
                extra_command += [str(wheel if kind == "wheel" else sdist) + "[openai,embedding,supabase,mcp,web,dev]"]
                run(extra_command, temporary)
                contracts = temporary / (kind + "-all-contracts")
                shutil.copytree(source / "tests_refactor", contracts / "tests_refactor", ignore=shutil.ignore_patterns("__pycache__"))
                shutil.copytree(source / "examples", contracts / "examples")
                shutil.copytree(source / "sql", contracts / "sql")
                shutil.copyfile(source / "pyproject.toml", contracts / "pyproject.toml")
                (contracts / "pytest.ini").write_text("[pytest]\naddopts = -m 'not supabase_integration'\nmarkers = supabase_integration: isolated backend job\n", encoding="utf8")
                result_args = []
                if args.contract_artifact_dir:
                    args.contract_artifact_dir.mkdir(parents=True, exist_ok=True)
                    result_args = ["--junitxml=" + str(args.contract_artifact_dir.resolve() / (kind + "-tdd12-contracts.xml"))]
                run([str(python), "-I", "-c", INSTALLED_CONTRACTS, str(expected), "tests_refactor",
                     "--ignore=tests_refactor/test_distribution.py", "-q", *result_args], contracts, timeout=300)
                print(f"{kind}: complete installed offline behavioral suite passed", flush=True)
            if args.tdd08_contracts:
                extra_command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
                if args.offline:
                    extra_command.append("--offline")
                extra_command += [str(wheel if kind == "wheel" else sdist) + "[embedding,supabase,dev]"]
                run(extra_command, temporary)
                contracts = temporary / (kind + "-contracts")
                tests = contracts / "tests_refactor"
                tests.mkdir(parents=True)
                for name in ("test_embedding.py", "test_memory_supabase.py", "test_tdd08_startup.py", "conftest.py", "resources.py", "fakes.py", "supabase_resources.py"):
                    shutil.copyfile(source / "tests_refactor" / name, tests / name)
                for example in ("refactor", "refactor-cli", "refactor-mcp"):
                    shutil.copytree(source / "examples" / example, contracts / "examples" / example)
                (contracts / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf8")
                result_args = []
                if args.contract_artifact_dir:
                    args.contract_artifact_dir.mkdir(parents=True, exist_ok=True)
                    result_args = ["--junitxml=" + str(args.contract_artifact_dir.resolve() / (kind + "-tdd08-contracts.xml"))]
                run([str(python), "-I", "-c", INSTALLED_CONTRACTS, str(expected), str(tests), *result_args], contracts)
                print(f"{kind}: installed Embedding/Supabase/startup contracts passed", flush=True)
            if args.tdd09_contracts:
                extra_command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
                if args.offline:
                    extra_command.append("--offline")
                extra_command += [str(wheel if kind == "wheel" else sdist) + "[mcp,web,dev]"]
                run(extra_command, temporary)
                contracts = temporary / (kind + "-tool-contracts")
                tests = contracts / "tests_refactor"
                tests.mkdir(parents=True)
                selected = ("test_builtin_io.py", "test_process_backend.py", "test_web_backend.py", "test_computer_backend.py",
                            "test_mcp_backend.py", "test_tdd09_integration.py")
                for name in (*selected, "mcp_fixture_server.py", "test_interaction.py", "test_tool_discovery.py", "test_release.py",
                             "test_task_flow.py", "conftest.py", "resources.py", "fakes.py", "supabase_resources.py"):
                    shutil.copyfile(source / "tests_refactor" / name, tests / name)
                for example in ("refactor", "refactor-cli", "refactor-mcp"):
                    shutil.copytree(source / "examples" / example, contracts / "examples" / example)
                (contracts / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf8")
                result_args = []
                if args.contract_artifact_dir:
                    args.contract_artifact_dir.mkdir(parents=True, exist_ok=True)
                    result_args = ["--junitxml=" + str(args.contract_artifact_dir.resolve() / (kind + "-tdd09-contracts.xml"))]
                run([str(python), "-I", "-c", INSTALLED_CONTRACTS, str(expected),
                     *(str(tests / name) for name in selected), *result_args], contracts)
                print(f"{kind}: installed MCP/13 tools/real IO/startup contracts passed", flush=True)
            if args.tdd10_contracts:
                extra_command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
                if args.offline:
                    extra_command.append("--offline")
                extra_command += [str(wheel if kind == "wheel" else sdist) + "[openai,dev]"]
                run(extra_command, temporary)
                contracts = temporary / (kind + "-review-contracts")
                tests = contracts / "tests_refactor"
                tests.mkdir(parents=True)
                selected = ("test_review.py", "test_review_trigger.py")
                for name in (*selected, "test_task_flow.py", "test_memory_prepare.py", "test_provider_contract.py",
                             "memory_config.py", "conftest.py", "resources.py", "fakes.py", "supabase_resources.py"):
                    shutil.copyfile(source / "tests_refactor" / name, tests / name)
                for example in ("refactor", "refactor-cli"):
                    shutil.copytree(source / "examples" / example, contracts / "examples" / example)
                (contracts / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf8")
                result_args = []
                if args.contract_artifact_dir:
                    args.contract_artifact_dir.mkdir(parents=True, exist_ok=True)
                    result_args = ["--junitxml=" + str(args.contract_artifact_dir.resolve() / (kind + "-tdd10-contracts.xml"))]
                run([str(python), "-I", "-c", INSTALLED_CONTRACTS, str(expected),
                     *(str(tests / name) for name in selected), *result_args], contracts)
                print(f"{kind}: installed N-turn/no-facts/anchors/restart review contracts passed", flush=True)
            if args.tdd11_contracts:
                extra_command = [args.uv, "pip", "install", "--python", str(python), "--constraint", str(constraints)]
                if args.offline:
                    extra_command.append("--offline")
                extra_command += [str(wheel if kind == "wheel" else sdist) + "[dev]"]
                run(extra_command, temporary)
                contracts = temporary / (kind + "-operations-contracts")
                tests = contracts / "tests_refactor"
                tests.mkdir(parents=True)
                selected = ("test_submission.py", "test_memory_operations.py", "test_operations_cli.py")
                for name in (*selected, "test_task_flow.py", "test_http.py", "test_tool_backends.py", "test_tool_discovery.py",
                             "test_interaction.py", "conftest.py", "resources.py", "fakes.py", "supabase_resources.py"):
                    shutil.copyfile(source / "tests_refactor" / name, tests / name)
                for example in ("refactor", "refactor-cli"):
                    shutil.copytree(source / "examples" / example, contracts / "examples" / example)
                (contracts / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf8")
                result_args = []
                if args.contract_artifact_dir:
                    args.contract_artifact_dir.mkdir(parents=True, exist_ok=True)
                    result_args = ["--junitxml=" + str(args.contract_artifact_dir.resolve() / (kind + "-tdd11-contracts.xml"))]
                run([str(python), "-I", "-c", INSTALLED_CONTRACTS, str(expected),
                     *(str(tests / name) for name in selected), *result_args], contracts)
                print(f"{kind}: installed submission/confirmation/diagnostics/log/export contracts passed", flush=True)
    print("Wheel/sdist contents verified; temporary environments/databases removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
