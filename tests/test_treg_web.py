"""Offline behavior tests for the fixed TinyFish adapter and registry boundary."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.frame.conf_mgr import _TomlToolConf
from src.frame.contracts import ControlError, DomainError, RunContext, ToolCall
from src.tool.backends import mcp, treg
from src.tool.contracts import Availability, BackendResult
from src.tool.factory import Create


def search_body():
    return {"results": [{"title": "Example", "url": "https://example.com/", "snippet": "A result"},
                        {"title": "Other", "url": "https://example.org/", "snippet": "Another result"}],
            "total_results": 8, "page": 0}


class FakeMCP:
    def __init__(self, settings):
        self.settings, self.calls, self.closed = settings, [], 0
        self.quote_change = lambda quote: None
        self.receipt_change = lambda receipt: None
        self.wire_change = lambda wire: None
        self.wait, self.entered = None, Event()

    def ListTools(self, context):
        context.CheckCancelled()
        return ()

    def ToolSpec(self, name):
        return object() if name in {"catalog_get", "call"} else None

    def Probe(self):
        return Availability(not self.closed, {"mcp"})

    def Close(self):
        self.closed += 1

    def Execute(self, name, args, context):
        self.calls.append((name, deepcopy(args)))
        endpoint = args["endpoint_id"]
        if name == "catalog_get":
            data = {"endpoint": {"id": endpoint, "method": "GET" if endpoint.endswith("search") else "POST",
                                 "cost": {"type": "free", "usd": 0, "value": 0, "currency": "USD"}},
                    "usd_per_call": 0}
            self.quote_change(data)
        else:
            self.entered.set()
            if self.wait is not None:
                while not self.wait.wait(0.005):
                    context.CheckCancelled()
            body = (search_body() if endpoint.endswith("search") else
                    {"results": [{"url": url, "final_url": url, "text": "正文" * 8000}
                                 for url in args["params"]["urls"]], "errors": []})
            data = {"endpoint_id": endpoint, "status": 200, "body": body, "cost_usd": 0}
            self.receipt_change(data)
        wire = {"structuredContent": data, "content": [], "isError": False}
        self.wire_change(wire)
        return BackendResult("ok", json.dumps(wire, ensure_ascii=False))


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("TREG_TOKEN", "offline-test-token")
    monkeypatch.setattr(mcp, "MCPBackend", FakeMCP)
    value = treg.TregWebBackend({"timeout": 2, "drain_timeout": 0.1})
    yield value
    value.Close()


def payload(result):
    return json.loads(result.content)


def test_missing_token_hides_tools_without_connecting(monkeypatch):
    monkeypatch.delenv("TREG_TOKEN", raising=False)
    monkeypatch.setattr(mcp, "MCPBackend", lambda settings: pytest.fail("must not connect"))
    value = treg.TregWebBackend({})
    assert not value.Probe().available
    result = value.Execute("web_search", {"query": "hello"}, RunContext())
    assert result.status == "error" and payload(result)["code"] == "CONFIG_CREDENTIAL"
    value.Close()
    value.Close()


def test_search_fixed_endpoint_and_existing_contract(backend):
    result = backend.Execute("web_search", {"query": "测试", "limit": 1}, RunContext())
    assert result.status == "ok" and result.effect == "none"
    data = payload(result)
    assert data["results"] == search_body()["results"][:1] and data["truncated"]
    assert data["artifact"]["kind"] == "provider_response"
    assert data["artifact"]["source"] == "treg://tinyfish.web.search"
    assert len(data["artifact"]["sha256"]) == 64
    assert backend._mcp.calls == [
        ("catalog_get", {"endpoint_id": "tinyfish.web.search"}),
        ("call", {"endpoint_id": "tinyfish.web.search", "method": "GET",
                  "params": {"query": "测试", "domain_type": "web", "page": 0}})]
    assert backend._mcp.settings["include"] == ["catalog_get", "call"]
    assert backend._mcp.settings["credential_env"] == "TREG_TOKEN"
    invalid = backend.Execute("call", {"endpoint_id": "other.paid.tool"}, RunContext())
    assert invalid.status == "invalid" and len(backend._mcp.calls) == 2


def test_fetch_markdown_fresh_cache_and_deduplicated_urls(backend):
    result = backend.Execute("web_extract", {"urls": ["https://example.com/", "https://example.com/"],
                                             "char_limit": 500}, RunContext())
    assert result.status == "ok"
    data = payload(result)
    assert len(data["pages"]) == 1
    assert len(data["pages"][0]["text"]) == 500 and data["pages"][0]["truncated"]
    name, request = backend._mcp.calls[-1]
    assert name == "call" and request["endpoint_id"] == "tinyfish.web.fetch" and request["method"] == "POST"
    assert request["params"]["urls"] == ["https://example.com/"]
    assert request["params"]["format"] == "markdown" and request["params"]["ttl"] == 0
    assert 0 < request["params"]["per_url_timeout_ms"] <= 2000
    assert data["pages"][0]["artifact"]["source"] == "treg://tinyfish.web.fetch"


@pytest.mark.parametrize("change", [
    lambda q: q["endpoint"]["cost"].update(type="paid"),
    lambda q: q["endpoint"]["cost"].update(usd=0.01),
    lambda q: q["endpoint"]["cost"].update(value=1),
    lambda q: q["endpoint"]["cost"].update(usd=False),
    lambda q: q["endpoint"].pop("cost"),
    lambda q: q.update(usd_per_call=0.01),
    lambda q: q.update(overflow_price_usd=0.002),
    lambda q: q["endpoint"].update(overflow_price_usd=0),
    lambda q: q["endpoint"].update(overflow_via="paid-relay"),
])
def test_price_or_overflow_change_stops_before_call(backend, change):
    backend._mcp.quote_change = change
    result = backend.Execute("web_search", {"query": "hello"}, RunContext())
    assert result.status == "denied" and payload(result)["code"] == "TREG_COST_POLICY"
    assert [name for name, _ in backend._mcp.calls] == ["catalog_get"]


def test_price_is_rechecked_on_every_invocation(backend):
    assert backend.Execute("web_search", {"query": "first"}, RunContext()).status == "ok"
    backend._mcp.quote_change = lambda q: q.update(usd_per_call=0.01)
    assert backend.Execute("web_search", {"query": "second"}, RunContext()).status == "denied"
    assert [name for name, _ in backend._mcp.calls] == ["catalog_get", "call", "catalog_get"]


@pytest.mark.parametrize("cost", [0.01, -1, True, float("nan")])
def test_unexpected_charge_without_retry_or_fallback(backend, cost):
    backend._mcp.receipt_change = lambda r: r.update(cost_usd=cost)
    result = backend.Execute("web_search", {"query": "hello"}, RunContext())
    assert result.status == "denied" and payload(result)["code"] == "TREG_COST_POLICY"
    assert len(backend._mcp.calls) == 2


def test_unmetered_receipt_does_not_invent_zero_charge(backend):
    backend._mcp.receipt_change = lambda r: r.pop("cost_usd")
    result = backend.Execute("web_search", {"query": "hello"}, RunContext())
    assert result.status == "ok" and "cost_usd" not in payload(result)["provider"]


def test_text_json_mcp_response(backend):
    def text_only(wire):
        wire["content"] = [{"type": "text", "text": json.dumps(wire.pop("structuredContent"))}]
    backend._mcp.wire_change = text_only
    assert backend.Execute("web_search", {"query": "hello"}, RunContext()).status == "ok"


@pytest.mark.parametrize("url", ["file:///tmp/a", "http://localhost/a", "http://localhost./a",
    "https://x.local/a", "http://127.0.0.1/", "http://198.18.0.2/", "http://10.0.0.1/",
    "http://[::1]/", "https://user:password@example.com/", "https://example.com/#fragment",
    "https://example.com:0/", "http://internal/a", "https://example.com/\n"])
def test_unsafe_url_denied_before_remote_requests(backend, url):
    result = backend.Execute("web_extract", {"urls": [url]}, RunContext())
    assert result.status == "denied" and payload(result)["code"] == "NETWORK_DENIED"
    assert not backend._mcp.calls


def test_target_dns_remains_remote(backend, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("target DNS must remain remote"))
    assert backend.Execute("web_extract", {"urls": ["https://example.com/"]}, RunContext()).status == "ok"


@pytest.mark.parametrize("change, code", [
    (lambda r: r.update(endpoint_id="treg.web.search"), "TREG_PROTOCOL"),
    (lambda r: r.update(status=429), "NETWORK_RATE_LIMIT"),
    (lambda r: r.update(status=401), "NETWORK_HTTP"),
    (lambda r: r.update(status=True), "TREG_PROTOCOL"),
    (lambda r: r.update(body="not-json"), "TREG_PROTOCOL"),
    (lambda r: r.update(served_via="overflow:relay"), "TREG_COST_POLICY"),
    (lambda r: r["body"].update(errors=[{"error": "timeout"}]), "NETWORK_SOURCE"),
    (lambda r: r["body"].update(results=[]), "TREG_PROTOCOL"),
    (lambda r: r["body"]["results"][0].update(text=""), "TREG_PROTOCOL"),
    (lambda r: r["body"]["results"][0].update(final_url="http://127.0.0.1/"), "NETWORK_DENIED"),
])
def test_provider_failure_or_partial_fetch_never_succeeds(backend, change, code):
    backend._mcp.receipt_change = change
    result = backend.Execute("web_extract", {"urls": ["https://example.com/"]}, RunContext())
    assert result.status != "ok" and payload(result)["code"] == code
    assert len(backend._mcp.calls) == 2


def test_response_byte_bound(backend):
    backend._max_bytes = 32
    result = backend.Execute("web_search", {"query": "hello"}, RunContext())
    assert result.status == "error" and payload(result)["code"] == "NETWORK_LIMIT"


def test_registry_exposes_two_tools_and_enforces_access(backend, monkeypatch):
    monkeypatch.setattr("src.tool.factory.CreateBackend", lambda settings: backend)
    tool = _TomlToolConf({"purposes": {"main": {"toolsets": ["web"]}},
        "backends": {"network": {"kind": "treg_web"}},
        "tools": {"web_search": {"backend_id": "network"}, "web_extract": {"backend_id": "network"}}})
    registry = Create(SimpleNamespace(Tool=lambda: tool))
    try:
        request = tool.Selection("main")
        assert registry.Select(request, RunContext()).allowed_names == ()
        context = RunContext(access={"network.read"})
        selection = registry.Select(request, context)
        assert set(selection.allowed_names) == {"web_search", "web_extract"}
        assert registry.Select(request, replace(context, purpose="recall")).allowed_names == ()
        assert registry.Execute(ToolCall("bad", "web_search", {"query": "hi", "endpoint_id": "paid"}),
                                selection, context).status == "invalid"
        assert not backend._mcp.calls
        assert registry.Execute(ToolCall("search", "web_search", {"query": "hi"}), selection, context).status == "ok"
    finally:
        registry.Close()


def test_backend_timeout_and_parent_cancellation(backend):
    backend._mcp.wait = Event()
    backend._timeout = 0.03
    result = backend.Execute("web_search", {"query": "hi"}, RunContext())
    assert result.status == "error" and payload(result)["code"] == "NETWORK_TIMEOUT"
    context = RunContext()
    context.cancel.set()
    with pytest.raises(ControlError, match="cancelled"):
        backend.Execute("web_search", {"query": "hi"}, context)


def test_close_waits_for_actual_borrower_and_is_retryable(backend):
    backend._mcp.wait = Event()
    results = []
    worker = Thread(target=lambda: results.append(backend.Execute("web_search", {"query": "hi"}, RunContext())))
    worker.start()
    assert backend._mcp.entered.wait(1)
    try:
        with pytest.raises(DomainError) as error:
            backend.Close()
        assert error.value.code == "CLOSE_FAILED" and backend._mcp.closed == 0
    finally:
        backend._mcp.wait.set()
        worker.join(1)
    assert not worker.is_alive() and results[0].status == "ok"
    backend.Close()
    backend.Close()
    assert backend._mcp.closed == 1 and not backend.Probe().available


@pytest.mark.parametrize("settings", [{"discover": True}, {"allow_private": True},
                                      {"endpoint": "https://other.example/mcp/"}])
def test_configuration_cannot_expand_treg_surface(settings):
    with pytest.raises(DomainError) as error:
        treg.TregWebBackend(settings)
    assert error.value.code == "CONFIG_INVALID"
