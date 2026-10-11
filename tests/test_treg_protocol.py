"""Real SDK/Streamable HTTP tests against an authenticated local MCP service."""
import json
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread
import time

import pytest

from src.frame.contracts import ControlError, DomainError, RunContext
from src.tool.backends import treg


@pytest.fixture
def protocol(monkeypatch, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    audit = tmp_path / "audit.jsonl"
    with (tmp_path / "server.log").open("w", encoding="utf8") as log:
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).parent / "support" / "treg_mcp_server.py"),
                                    str(port), str(audit)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 8
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail((tmp_path / "server.log").read_text(encoding="utf8"))
                    time.sleep(0.03)
            monkeypatch.setattr(treg, "MCP_ENDPOINT", f"http://127.0.0.1:{port}/mcp/")
            monkeypatch.setenv("TREG_TOKEN", "protocol-test-token")
            yield audit
        finally:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()] if path.exists() else []


def test_authenticated_sdk_search_fetch_and_session_close(protocol):
    backend = treg.TregWebBackend({"timeout": 4})
    client = backend._mcp
    try:
        assert backend.Probe().available
        search = backend.Execute("web_search", {"query": "Python"}, RunContext())
        fetch = backend.Execute("web_extract", {"urls": ["https://example.com/"]}, RunContext())
        assert search.status == "ok" and json.loads(search.content)["results"][0]["title"] == "Python"
        assert fetch.status == "ok" and "正文" in json.loads(fetch.content)["pages"][0]["text"]
        events = records(protocol)
        assert [row["name"] for row in events] == ["catalog_get", "call", "catalog_get", "call"]
        assert {row["endpoint_id"] for row in events} == {"tinyfish.web.search", "tinyfish.web.fetch"}
        assert set(client._specs) == {"catalog_get", "call"}
    finally:
        backend.Close()
    assert not client._thread.is_alive()


def test_cancellation_reaches_mcp_and_drains_session(protocol):
    backend = treg.TregWebBackend({"timeout": 4})
    context, errors = RunContext(), []
    def invoke():
        try:
            backend.Execute("web_search", {"query": "__slow__"}, context)
        except ControlError as error:
            errors.append(error.code)
    worker = Thread(target=invoke)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not any(row["name"] == "call" for row in records(protocol)):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        context.cancel.set()
        worker.join(2)
        assert not worker.is_alive() and errors == ["CANCELLED"]
        deadline = time.monotonic() + 2
        while not any(row["name"] == "cancelled" for row in records(protocol)):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        context.cancel.set()
        worker.join(2)
        backend.Close()
    assert not backend._mcp._thread.is_alive()


def test_bad_token_fails_during_connection_without_tool_calls(protocol, monkeypatch):
    monkeypatch.setenv("TREG_TOKEN", "wrong-offline-token")
    with pytest.raises(DomainError) as error:
        treg.TregWebBackend({"timeout": 2})
    assert error.value.code == "MCP_CONNECTION"
    assert records(protocol) == []
