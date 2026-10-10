"""Run a real Chromium P6 smoke with temporary SQLite/config and bounded cleanup.

Requires Node and Playwright separately from the production Python package.
"""
import argparse
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from funagent import global_server
from funagent.model.demo import DemoProvider
from funagent.model import ModelError


class SmokeProvider(DemoProvider):
    def Send(self, request, context):
        if context.purpose == "main" and request.payload.messages[-1].content.startswith("fail:"):
            raise ModelError("AUTH")
        return super().Send(request, context)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    parser.add_argument("--playwright", default="playwright")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="funagent-p6-") as temporary:
        conf = Path(temporary) / "conf"
        shutil.copytree(root / "examples/refactor-review", conf)
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("P6 actual workspace read", encoding="utf8")
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        serv = conf / "serv.conf"
        serv.write_text(serv.read_text().replace('mode = "cli"', 'mode = "web"').replace('port = 8080', f'port = {port}').replace('workspace = "."', f'workspace = "{workspace.as_posix()}"'), encoding="utf8")
        original = global_server.PROVIDERS["demo"]
        global_server.PROVIDERS["demo"] = SmokeProvider
        try:
            global_server.Start(conf)
            command = [args.node, str(root / "scripts/refactor_browser_smoke.cjs"), args.playwright,
                       args.browser, f"http://127.0.0.1:{port}", str(args.artifact_dir.resolve())]
            result = subprocess.run(command, timeout=60).returncode
        finally:
            global_server.Stop()
            global_server.PROVIDERS["demo"] = original
        assert not any(t.name.startswith("funagent-") for t in threading.enumerate())
    print("Temporary config/databases and application workers cleaned up.", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
