from __future__ import annotations

import base64
import socket
import subprocess
from types import SimpleNamespace

import pytest

from gateway.web.server import RequestHandler
from tools import network_http, web_tool


@pytest.fixture(autouse=True)
def no_machine_proxy(monkeypatch):
    monkeypatch.setattr(network_http, "getproxies", lambda: {})
    monkeypatch.setattr(network_http, "proxy_bypass", lambda host: False)
    monkeypatch.delenv("DDGS_PROXY", raising=False)


@pytest.mark.parametrize("failure", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_sse_disconnect_does_not_log_error_or_write_second_response(monkeypatch, caplog, failure):
    handler = object.__new__(RequestHandler)
    handler.path = "/api/events?session_id=s1"
    handler.headers = {}
    handler.close_connection = False
    handler.server = SimpleNamespace(host=SimpleNamespace(events=SimpleNamespace(
        connect=lambda *args: ([], 0), closed=False,
    )))
    monkeypatch.setattr(handler, "_validate", lambda method: {})
    for name in ("send_response", "send_header", "end_headers"):
        monkeypatch.setattr(handler, name, lambda *args: None)

    def disconnected(data):
        raise failure("client disconnected")

    def unexpected_response(*args):
        pytest.fail("a disconnected SSE client must not receive a JSON error response")

    handler.wfile = SimpleNamespace(write=disconnected)
    monkeypatch.setattr(handler, "_json", unexpected_response)
    handler._handle("GET")
    assert handler.close_connection
    assert "web.request_failed" not in caplog.text


@pytest.mark.parametrize("failed", [False, True])
def test_search_worker_utf8_proxy_and_diagnostics(monkeypatch, context_factory, failed):
    proxy = "http://alice:secret@127.0.0.1:7897"
    monkeypatch.setenv("DDGS_PROXY", proxy)
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    monkeypatch.setenv("TEST_PRIVATE_KEY", "must not enter the child environment")
    original_popen = subprocess.Popen
    worker_code = f'''
import runpy,sys,types
assert sys.stdout.encoding == "utf-8", sys.stdout.encoding
class FakeDDGS:
    def __init__(self, *, proxy):
        assert proxy == {proxy!r}
    def text(self, query, max_results):
        assert query == "\\u4e2d\\u6587 query"
        if {failed!r}:
            raise RuntimeError("timeout via http://alice:secret@127.0.0.1:7897 \\u8fde\\u63a5\\u5931\\u8d25")
        return [{{"title":"\\u201c\\u4e2d\\u6587\\u201d \\U0001f600",
                 "href":"https://example.com/", "body":"\\u63cf\\u8ff0"}}]
sys.modules["ddgs"] = types.SimpleNamespace(DDGS=FakeDDGS)
runpy.run_module("tools._ddgs_worker", run_name="__main__")
'''

    def launch_worker(command, **kwargs):
        assert command[1:3] == ["-X", "utf8"]
        assert "TEST_PRIVATE_KEY" not in kwargs["env"]
        assert "PYTHONIOENCODING" not in kwargs["env"]
        return original_popen([*command[:-2], "-c", worker_code], **kwargs)

    monkeypatch.setattr(web_tool.subprocess, "Popen", launch_worker)
    with context_factory() as context:
        if failed:
            with pytest.raises(RuntimeError, match="timeout via") as raised:
                web_tool._search_subprocess("中文 query", 1, context, 10)
            assert "连接失败" in str(raised.value)
            assert "secret" not in str(raised.value)
        else:
            result = web_tool._search_subprocess("中文 query", 1, context, 10)
            assert result["results"][0]["title"] == "“中文” 😀"


def test_search_uses_system_proxy_and_explicit_override(monkeypatch):
    monkeypatch.setattr(network_http, "getproxies", lambda: {"https": "http://127.0.0.1:7897"})
    assert network_http.search_proxy() == "http://127.0.0.1:7897"
    monkeypatch.setenv("DDGS_PROXY", "socks5h://127.0.0.1:9150")
    assert network_http.search_proxy() == "socks5h://127.0.0.1:9150"
    monkeypatch.setattr(network_http, "proxy_bypass", lambda host: True)
    assert network_http.outbound_proxy("https://example.com") is None


def test_proxy_connect_pins_ip_and_keeps_origin_hostname(monkeypatch):
    calls = []
    stream = object()

    class Tunnel:
        def __init__(self, host, port, timeout):
            calls.append(("proxy", host, port))
            self.sock = stream

        def set_tunnel(self, host, port, headers):
            calls.append(("tunnel", host, port, headers))

        def connect(self):
            pass

        def close(self):
            assert self.sock is None

    monkeypatch.setattr(network_http, "getproxies", lambda: {"https": "http://alice:p%40ss@127.0.0.1:7897"})
    monkeypatch.setattr(network_http.http.client, "HTTPConnection", Tunnel)
    connection = network_http.open_web_connection("https://example.com/page", "93.184.215.14")
    try:
        assert connection.host == "example.com"
        assert connection._create_connection(("example.com", 443)) is stream
    finally:
        connection.close()
    assert calls[:1] == [("proxy", "127.0.0.1", 7897)]
    assert calls[1][1:3] == ("93.184.215.14", 443)
    authorization = "Basic " + base64.b64encode(b"alice:p@ss").decode("ascii")
    assert calls[1][3] == {"Proxy-Authorization": authorization}


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.2", "169.254.169.254", "198.18.0.149"])
def test_private_dns_and_fake_ip_still_blocked_with_proxy(monkeypatch, address):
    monkeypatch.setattr(network_http, "getproxies", lambda: {"https": "http://127.0.0.1:7897"})
    monkeypatch.setattr(network_http.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
    ])
    expected = "Fake-IP" if address.startswith("198.18.") else "not allowed"
    with pytest.raises(ValueError, match=expected):
        network_http.validate_web_url("https://example.com/")


def test_proxied_fetch_checks_redirect_before_opening_second_connection(monkeypatch):
    connections = []

    class Connection:
        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return SimpleNamespace(status=302, getheader=lambda key: "http://127.0.0.1/metadata")

        def close(self):
            pass

    def connect(url, address):
        connections.append((url, address))
        return Connection()

    monkeypatch.setattr(web_tool, "open_web_connection", connect)
    with pytest.raises(ValueError, match="not allowed"):
        web_tool._fetch_http("https://8.8.8.8/start", allow_private=False, max_bytes=1000, use_proxy=True)
    assert connections == [("https://8.8.8.8/start", "8.8.8.8")]
