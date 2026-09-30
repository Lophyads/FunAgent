from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("httpx2")
from mcp.server import Server
from mcp.types import CallToolResult, InputRequiredResult, ListToolsResult, TextContent, Tool

from tools.config import McpServerConfig, WorkspaceConfig
from tools.lifecycle import ResourceUnavailableError
from tools.mcp_client import McpClient


def _event(event, **fields):
    if path := os.environ.get("MCP_TEST_EVENTS"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, "pid": os.getpid(), **fields}) + "\n")


def _test_server():
    schema = {"type": "object", "properties": {
        "text": {"type": "string"}, "seconds": {"type": "number"},
    }, "additionalProperties": False}
    tools = [Tool(name=name, description=name, inputSchema=schema)
             for name in ("echo", "slow", "error", "structured", "input", "drop")]

    async def list_tools(ctx, params):
        mode = os.environ.get("MCP_TEST_PAGING", "normal")
        cursor = params.cursor if params is not None else None
        _event("list", cursor=cursor)
        if cursor is None:
            return ListToolsResult(tools=tools[:2], nextCursor="second")
        if mode == "failure":
            raise RuntimeError("second page failed")
        if mode == "repeat":
            return ListToolsResult(tools=tools[2:], nextCursor="second")
        if mode == "endless":
            return ListToolsResult(tools=[], nextCursor=str(int(cursor) + 1) if cursor.isdigit() else "1")
        return ListToolsResult(tools=tools[2:])

    async def call_tool(ctx, params):
        args = params.arguments or {}
        _event("call", name=params.name, arguments=args)
        if params.name == "slow":
            try:
                await asyncio.sleep(args.get("seconds", 5))
            finally:
                _event("slow_cleanup")
        if params.name == "drop":
            os._exit(0)
        if params.name == "error":
            return CallToolResult(content=[TextContent(type="text", text="remote failed")], isError=True)
        if params.name == "input":
            return InputRequiredResult(inputRequests={}, requestState="opaque")
        if params.name == "structured":
            return CallToolResult(content=[], structuredContent={"value": args.get("text", "")})
        return CallToolResult(content=[TextContent(type="text", text=args.get("text", "ok"))])

    return Server("funagent-test", on_list_tools=list_tools, on_call_tool=call_tool)


def _run_test_server():
    _event("started")
    if os.environ.get("MCP_TEST_HANG"):
        time.sleep(30)
        return
    server = _test_server()
    if "--http" in sys.argv:
        import uvicorn
        from starlette.responses import JSONResponse
        app = server.streamable_http_app()

        async def authenticated(scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/mcp"):
                headers = dict(scope["headers"])
                if headers.get(b"authorization") != b"Bearer test-token":
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await app(scope, receive, send)

        uvicorn.run(authenticated, host="127.0.0.1", port=int(sys.argv[-1]), log_level="error")
    else:
        from mcp.server.stdio import stdio_server

        async def main():
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
        try:
            asyncio.run(main())
        finally:
            _event("closed")


def stdio_config(tmp_path, **updates):
    config = {
        "command": sys.executable, "args": [str(Path(__file__).resolve())],
        "env": {"MCP_TEST_EVENTS": str(tmp_path / "events.jsonl")},
        "connect_timeout_seconds": 10, "requires_approval": False,
    }
    config.update(updates)
    return McpServerConfig(**config)


def events(tmp_path):
    path = tmp_path / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "condition did not become true"


@contextmanager
def opened_client(tmp_path, servers):
    client = McpClient(servers, workspace=WorkspaceConfig(default_cwd=tmp_path, allowed_root=tmp_path))
    try:
        yield client
    finally:
        client.close(deadline=time.monotonic() + 10)


@pytest.fixture
def http_endpoint(tmp_path):
    pytest.importorskip("uvicorn")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = {**os.environ, "MCP_TEST_EVENTS": str(tmp_path / "events.jsonl")}
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--http", str(port)],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    def listening():
        if process.poll() is not None:
            raise RuntimeError("test HTTP server exited")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except OSError:
            return False
    try:
        wait_until(listening, timeout=10)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_stdio_protocol_discovery_call_and_shutdown(tmp_path):
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        tools = client.discover()["local"]
        assert {t.name for t in tools} == {"echo", "slow", "error", "structured", "input", "drop"}
        assert client.is_available("local")
        result = client.call_tool("local", "echo", {"text": "hello"}, check_active=lambda: None)
        assert result.content[0].text == "hello"
        client.close_session(None, deadline=time.monotonic() + 1)
        client.reap_idle(now=time.monotonic(), deadline=time.monotonic() + 1)
        assert client.is_available("local")
    assert not client._thread.is_alive()
    assert [e["event"] for e in events(tmp_path)].count("closed") == 1
    client.close(deadline=time.monotonic() + 1)


def test_http_protocol_and_headers(tmp_path, http_endpoint, monkeypatch):
    import httpx2
    http_clients = []
    original_enter = httpx2.AsyncClient.__aenter__
    async def record_enter(self):
        http_clients.append(self)
        return await original_enter(self)
    monkeypatch.setattr(httpx2.AsyncClient, "__aenter__", record_enter)
    monkeypatch.setenv("MCP_TEST_AUTH", "Bearer test-token")
    config = McpServerConfig(url=http_endpoint, headers={"Authorization": "env:MCP_TEST_AUTH"})
    with opened_client(tmp_path, {"remote": config}) as client:
        assert len(client.discover()["remote"]) == 6
        result = client.call_tool("remote", "echo", {"text": "http"}, check_active=lambda: None)
        assert result.content[0].text == "http"
        assert client._connections["remote"].client.cache is None
    assert not client._thread.is_alive()
    assert http_clients and all(c.is_closed for c in http_clients)


@pytest.mark.parametrize("mode", ["failure", "repeat", "endless"])
def test_failed_pagination_never_publishes_partial_directory(tmp_path, mode):
    config = stdio_config(tmp_path, env={"MCP_TEST_PAGING": mode})
    with opened_client(tmp_path, {"broken": config}) as client:
        assert client.discover() == {}
        assert not client.is_available("broken")


def test_optional_failure_is_isolated_and_required_failure_raises(tmp_path):
    broken = McpServerConfig(command="funagent-nonexistent-command")
    with opened_client(tmp_path, {"broken": broken, "working": stdio_config(tmp_path)}) as client:
        assert set(client.discover()) == {"working"}
    with opened_client(tmp_path, {"broken": broken.model_copy(update={"required": True})}) as client:
        with pytest.raises(RuntimeError, match="required MCP servers"):
            client.discover()


def test_timeout_and_input_continuation_do_not_replay(tmp_path):
    with opened_client(tmp_path, {"local": stdio_config(tmp_path, tool_timeout_seconds=0.2)}) as client:
        client.discover()
        with pytest.raises(TimeoutError):
            client.call_tool("local", "slow", {"seconds": 2}, check_active=lambda: None)
        with pytest.raises(RuntimeError, match="input continuation"):
            client.call_tool("local", "input", {}, check_active=lambda: None)
        # Cancellation leaves the reusable connection intact.
        assert client.call_tool("local", "echo", {}, check_active=lambda: None).content[0].text == "ok"
    calls = [e["name"] for e in events(tmp_path) if e["event"] == "call"]
    assert calls == ["slow", "input", "echo"]


def test_context_revocation_cancels_rpc(tmp_path):
    revoked = threading.Event()
    def check_active():
        if revoked.is_set():
            raise ResourceUnavailableError("revoked")
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        client.discover()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(client.call_tool, "local", "slow", {"seconds": 10}, check_active=check_active)
            wait_until(lambda: any(e["event"] == "call" for e in events(tmp_path)))
            revoked.set()
            with pytest.raises(ResourceUnavailableError, match="revoked"):
                future.result(timeout=2)
        wait_until(lambda: not client._inflight)
    assert len([e for e in events(tmp_path) if e["event"] == "call"]) == 1


def test_stdio_disconnect_is_not_replayed(tmp_path):
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        client.discover()
        with pytest.raises(RuntimeError, match="MCP call failed"):
            client.call_tool("local", "drop", {}, check_active=lambda: None)
        assert not client.is_available("local")
        with pytest.raises(ResourceUnavailableError):
            client.call_tool("local", "echo", {}, check_active=lambda: None)
    assert len([e for e in events(tmp_path) if e["event"] == "call"]) == 1


def test_failed_startup_and_missing_credentials(tmp_path, monkeypatch):
    config = stdio_config(tmp_path, connect_timeout_seconds=0.1, env={"MCP_TEST_HANG": "1"})
    with opened_client(tmp_path, {"hung": config}) as client:
        assert client.discover() == {}
    assert not client._thread.is_alive()
    monkeypatch.delenv("MCP_TEST_MISSING_SECRET", raising=False)
    with pytest.raises(ValueError, match="missing MCP environment"):
        McpClient({"local": stdio_config(tmp_path, env={"TOKEN": "env:MCP_TEST_MISSING_SECRET"})},
                  workspace=WorkspaceConfig())


def test_cancelled_rpc_holds_lock_until_actual_cleanup(tmp_path, monkeypatch):
    revoked = threading.Event()
    cleaning = threading.Event()
    release = threading.Event()
    entered = []
    def check_active():
        if revoked.is_set():
            raise ResourceUnavailableError("revoked")
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        client.discover()
        session = client._connections["local"].client.session
        original = session.call_tool
        async def delayed_cleanup(name, arguments, **kwargs):
            entered.append(name)
            try:
                return await original(name, arguments, **kwargs)
            finally:
                if name == "slow":
                    cleaning.set()
                    while not release.is_set():
                        await asyncio.sleep(0.01)
        monkeypatch.setattr(session, "call_tool", delayed_cleanup)
        with ThreadPoolExecutor(2) as pool:
            try:
                first = pool.submit(client.call_tool, "local", "slow", {"seconds": 10}, check_active=check_active)
                wait_until(lambda: any(e["event"] == "call" for e in events(tmp_path)))
                revoked.set()
                with pytest.raises(ResourceUnavailableError):
                    first.result(timeout=2)
                assert cleaning.wait(2)
                second = pool.submit(client.call_tool, "local", "echo", {}, check_active=lambda: None)
                wait_until(lambda: len(client._inflight) == 2)
                assert entered == ["slow"]
                release.set()
                assert second.result(timeout=2).content[0].text == "ok"
                assert entered == ["slow", "echo"]
            finally:
                release.set()


def test_close_timeout_retains_actual_cleanup_and_can_be_retried(tmp_path, monkeypatch):
    cleaning = threading.Event()
    release = threading.Event()
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        client.discover()
        session = client._connections["local"].client.session
        original = session.call_tool
        async def delayed_cleanup(name, arguments, **kwargs):
            try:
                return await original(name, arguments, **kwargs)
            finally:
                cleaning.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
        monkeypatch.setattr(session, "call_tool", delayed_cleanup)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.call_tool, "local", "slow", {"seconds": 10}, check_active=lambda: None)
            wait_until(lambda: any(e["event"] == "call" for e in events(tmp_path)))
            try:
                with pytest.raises(TimeoutError):
                    client.close(deadline=time.monotonic() + 0.05)
                assert cleaning.is_set()
                close_future = client._close_future
                assert client._thread.is_alive() and not close_future.done()
                with pytest.raises(ResourceUnavailableError):
                    client.call_tool("local", "echo", {}, check_active=lambda: None)
                release.set()
                client.close(deadline=time.monotonic() + 3)
                assert client._close_future is close_future
                assert not client._thread.is_alive()
                with pytest.raises(Exception):
                    pending.result(timeout=1)
            finally:
                release.set()


def test_rpc_own_timeout_is_not_mistaken_for_poll_timeout(tmp_path, monkeypatch):
    with opened_client(tmp_path, {"local": stdio_config(tmp_path, tool_timeout_seconds=3)}) as client:
        client.discover()
        async def fail(*args, **kwargs):
            raise TimeoutError("SDK timeout")
        monkeypatch.setattr(client._connections["local"].client.session, "call_tool", fail)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="TimeoutError"):
            client.call_tool("local", "echo", {}, check_active=lambda: None)
        assert time.monotonic() - started < 1


def test_connection_context_is_exited_by_its_owner_task(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    ownership = []
    @asynccontextmanager
    async def fake_connection():
        owner = asyncio.current_task()
        ownership.append(("enter", owner))
        try:
            yield SimpleNamespace(server_capabilities=SimpleNamespace(tools=None))
        finally:
            ownership.append(("exit", asyncio.current_task()))
    async def open_fake(self, connection, stack):
        return await stack.enter_async_context(fake_connection())
    monkeypatch.setattr(McpClient, "_open_client", open_fake)
    with opened_client(tmp_path, {"empty": McpServerConfig(command="unused")}) as client:
        assert client.discover() == {"empty": ()}
        assert client.is_available("empty")
    assert [step for step, _ in ownership] == ["enter", "exit"]
    assert ownership[0][1] is ownership[1][1]


def test_different_servers_can_wait_for_io_concurrently(tmp_path):
    directories = [tmp_path / sid for sid in ("one", "two")]
    for directory in directories:
        directory.mkdir()
    configs = {sid: stdio_config(directory) for sid, directory in zip(("one", "two"), directories)}
    with opened_client(tmp_path, configs) as client:
        client.discover()
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(client.call_tool, sid, "slow", {"seconds": 1}, check_active=lambda: None)
                       for sid in ("one", "two")]
            wait_until(lambda: all(any(e["event"] == "call" for e in events(d)) for d in directories))
            assert not any(e["event"] == "slow_cleanup" for d in directories for e in events(d))
            assert all(f.result(timeout=3).content[0].text == "ok" for f in futures)


def test_loop_start_failure_has_no_orphaned_provider(tmp_path, monkeypatch):
    original = threading.Thread.start
    def start(self):
        if self.name == "mcp-loop":
            raise RuntimeError("thread unavailable")
        original(self)
    monkeypatch.setattr(threading.Thread, "start", start)
    with opened_client(tmp_path, {"local": stdio_config(tmp_path)}) as client:
        with pytest.raises(RuntimeError, match="thread unavailable"):
            client.discover()
    assert client._closed


if __name__ == "__main__":
    _run_test_server()
