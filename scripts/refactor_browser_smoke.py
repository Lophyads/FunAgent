"""Run a real Chromium installed/source smoke with temporary SQLite/config and bounded cleanup.

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    parser.add_argument("--playwright", default="playwright")
    parser.add_argument("--browser", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--installed", action="store_true", help="Use the installed package without adding the checkout root")
    args = parser.parse_args()
    if not args.installed:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.app import application
    from src.model.demo import DemoProvider
    from src.model import ModelError
    from src.frame.contracts import ModelReply, ToolCall
    from src.model.protocols import ProviderResponse

    class SmokeProvider(DemoProvider):
        def Send(self, request, context):
            if context.purpose == "main" and request.payload.messages[-1].content.split("\n\nMemory evidence", 1)[0] == "clarify-many:":
                questions = [
                    {"id": "color", "prompt": "Color <b>as text</b>?", "kind": "single", "options": ["red", "blue"]},
                    {"id": "features", "prompt": "Features?", "kind": "multiple", "options": ["fast", "small", "quiet"]},
                    *({"id": key, "prompt": f"{key}?", "kind": "text"} for key in ("note", "four", "five")),
                ]
                return ProviderResponse(ModelReply(calls=(ToolCall("browser-many", "clarify", {"questions": questions}),), finish_reason="tool_calls"))
            if context.purpose == "main" and request.payload.messages[-1].content.startswith("fail:"):
                raise ModelError("AUTH")
            return super().Send(request, context)

    print(f"Application package: {Path(application.__file__).resolve()}", flush=True)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="funagent-browser-") as temporary:
        conf = Path(temporary) / "conf"
        shutil.copytree(root / "examples/refactor-review", conf)
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        (workspace / "note.txt").write_text("FunAgent actual workspace read", encoding="utf8")
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        serv = conf / "serv.conf"
        serv.write_text(serv.read_text().replace('mode = "cli"', 'mode = "web"').replace('port = 8080', f'port = {port}').replace('workspace = "."', f'workspace = "{workspace.as_posix()}"'), encoding="utf8")
        from src.model import factory as model_factory
        original = model_factory.PROVIDERS["demo"]
        model_factory.PROVIDERS["demo"] = SmokeProvider
        from src.api.http_server import _Handler
        original_post = _Handler.do_POST
        def controls(handler):
            if handler.path == '/test/expire-management':
                from src.memory import MemoryOperationsMgr
                operations = MemoryOperationsMgr.GetInstance()
                with operations._lock:
                    operations._pending = {key: (0, *value[1:]) for key, value in operations._pending.items()}
                handler._send(200, {'expired': True})
            elif handler.path == '/test/reset-display':
                from src.frame.event_mgr import EventMgr
                from src.frame.web_projection import WebProjection
                service = EventMgr.GetInstance()
                with service._condition:
                    service._web = WebProjection()
                    service._condition.notify_all()
                handler._send(200, {'reset': True})
            else:
                original_post(handler)
        _Handler.do_POST = controls
        try:
            application.Start(conf)
            command = [args.node, str(root / "scripts/refactor_browser_smoke.cjs"), args.playwright,
                       args.browser, f"http://127.0.0.1:{port}", str(args.artifact_dir.resolve())]
            result = subprocess.run(command, timeout=180).returncode
        finally:
            application.Stop()
            model_factory.PROVIDERS["demo"] = original
            _Handler.do_POST = original_post
        assert not any(t.name.startswith("funagent-") for t in threading.enumerate())
    print("Temporary config/databases and application workers cleaned up.", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
