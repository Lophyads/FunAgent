from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time

import pytest

from agentcore.interfaces import ModelOutputEvent
from agentcore.observability import EventEnvelope
from file_memory.store import FileMemoryStore, MemoryOperation, MemoryVersionConflict
from gateway.web import WebHost, WebServer
from gateway.web.events import WebEventHub
from gateway.web.read_model import WebError, sanitized
from tools.runtime import get_tool_services
from conftest import ScriptedTransport, scripted_provider_stack
from test_cli_gateway import seed_crashed_sessions
from test_interactive_tools import clarify_response


@pytest.fixture
def web_factory(config_factory, tmp_path, final_response):
    hosts = []
    def make(*responses, config=None, transport=None):
        config = config or config_factory()
        transport = transport or ScriptedTransport(*(responses or (final_response,)))
        resolver, modes, _ = scripted_provider_stack(config, {"primary": transport})
        host = WebHost(config, memory=FileMemoryStore(tmp_path / "memories"),
                       provider_resolver=resolver, api_modes=modes)
        hosts.append(host)
        host.start()
        return host, transport
    yield make
    for host in hosts:
        assert host.close(3).completed
        host.events.close()


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def send(host, text="hello", sid="s1", request_id="r1"):
    return host.submit({"session_id": sid, "text": text, "client_request_id": request_id})


def test_chat_dedup_metadata_and_read_does_not_attach(web_factory):
    host, transport = web_factory()
    assert host.read.messages("unattached", {})["messages"] == []
    assert host.gateway._attached == set()
    dto = send(host)
    wait_for(lambda: host.turn(dto["turn_id"])["result"])
    assert send(host) == dto and len(transport.requests) == 1
    with pytest.raises(WebError) as exc:
        send(host, text="different")
    assert exc.value.status == 409
    rows = host.read.messages("s1", {})["messages"]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert {row["metadata"]["turn_id"] for row in rows} == {dto["turn_id"]}
    before = host.read.messages("s1", {"before": str(rows[-1]["id"])})
    assert [r["id"] for r in before["messages"]] == [rows[0]["id"]]
    assert host.cancel(dto["turn_id"])["status"] == "completed"


def test_fifo_cancel_scope_and_two_sessions(web_factory, final_response):
    gate, started = threading.Event(), threading.Event()
    def blocked(request):
        started.set(); gate.wait(2); return final_response
    host, transport = web_factory(blocked, final_response)
    try:
        one = send(host, sid="one", request_id="one")
        assert started.wait(1)
        queued = send(host, sid="one", text="queued", request_id="queued")
        two = send(host, sid="two", request_id="two")
        wait_for(lambda: host.turn(two["turn_id"])["result"])
        host.cancel(queued["turn_id"])
        assert host.turn(one["turn_id"])["status"] not in {"cancelling", "interrupted"}
        gate.set()
        wait_for(lambda: host.turn(queued["turn_id"])["result"])
        assert host.turn(queued["turn_id"])["status"] == "interrupted"
        assert len(transport.requests) == 2
    finally:
        gate.set()


def test_recovery_is_registered_before_normal_fifo(web_factory, config_factory, final_response):
    config = config_factory()
    seed_crashed_sessions(config, "recover")
    host, transport = web_factory(final_response, final_response, config=config)
    dto = send(host, sid="recover")
    wait_for(lambda: host.turn(dto["turn_id"])["result"])
    events, _ = host.events.connect("recover")
    turns = events[0]["payload"]["turns"]
    assert len(turns) == 2 and all(t["status"] == "completed" for t in turns)
    assert len(transport.requests) == 2
    assert len(host.read.messages("recover", {})["messages"]) == 4


def test_clarification_answers_bypass_fifo_and_bind_session(web_factory, final_response):
    host, _ = web_factory(clarify_response(), final_response)
    dto = send(host)
    request = wait_for(lambda: host.interactions("s1")["interactions"])[0]
    assert request["turn_id"] == dto["turn_id"]
    with pytest.raises(WebError) as exc:
        host.resolve(request["request_id"], {"session_id":"wrong", "response":{"answers":{"q0":"A"}}})
    assert exc.value.status == 409
    host.resolve(request["request_id"], {"session_id":"s1", "response":{"answers":{"q0":"A"}}})
    wait_for(lambda: host.turn(dto["turn_id"])["result"])
    assert host.turn(dto["turn_id"])["status"] == "completed"
    assert not host.interactions()["interactions"]


def test_memory_cas_exact_match_and_capacity(web_factory):
    host, _ = web_factory()
    host.memory.apply(MemoryOperation("add", "memory", "alpha"))
    host.memory.apply(MemoryOperation("add", "memory", "alpha beta"))
    version = host.memory_document("memory")["version"]
    host.edit_memory("memory", {"action":"replace", "old_text":"alpha", "content":"gamma", "expected_version":version})
    assert host.memory.read("memory").entries == ("gamma", "alpha beta")
    with pytest.raises(WebError) as exc:
        host.edit_memory("memory", {"action":"add", "content":"stale", "expected_version":version})
    assert exc.value.status == 409
    other = FileMemoryStore(host.memory.directory)
    version = other.read("memory").version
    host.memory.apply(MemoryOperation("add", "memory", "external"))
    with pytest.raises(MemoryVersionConflict):
        other.apply(MemoryOperation("add", "memory", "lost update"), expected_version=version)


def test_readonly_data_schema_filter_preview_and_config_secrecy(web_factory):
    host, _ = web_factory()
    dto = send(host, text="literal %_ " + "中" * 24000)
    wait_for(lambda: host.turn(dto["turn_id"])["result"])
    assert {t["name"] for t in host.read.schema()["tables"]} == {"sessions","messages","session_leases","delivery_obligations"}
    data = host.read.table("messages", {"column":"content", "op":"contains", "value":"%_"})
    assert data["total"] == 1
    row = data["rows"][0]
    assert len(row["values"]["content"]) == 512 and "content" in row["truncated"]
    detail = host.read.table("messages", {"id":str(row["pk"]["id"])}, detail=True)
    assert len(detail["values"]["content"].encode("utf-8")) <= 65536 and "content" in detail["truncated"]
    for table, query in [("messages;DROP TABLE sessions", {}), ("messages", {"sort":"id; DELETE"}),
                         ("messages", {"column":"id", "op":"sql"})]:
        with pytest.raises(WebError):
            host.read.table(table, query)
    with host.read.connection() as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM messages")
    config = host.read.catalog("config")
    assert config["system_prompt"] == "[redacted]" and config["routes"][0]["credential_ref"] == "[redacted]"
    assert sanitized({"env":{"FOO":"secret"},"headers":{"X":"secret"},"args":["secret"],
                      "url":"https://u:secret@host/path?token=secret"}) == {
                      "env":{"FOO":"[redacted]"},"headers":{"X":"[redacted]"},"args":"[redacted]","url":"https://host/path"}
    services = get_tool_services()
    before = dict(services._providers)
    host.read.catalog("connections")
    assert services._providers == before


def test_hub_replay_reset_cumulative_output_and_terminal_guard():
    hub = WebEventHub(capacity=2)
    early = EventEnvelope(event_id="e", name="turn.phase_started", occurred_at=time.time(),
        trace_id="turn", session_id="hash", turn_id="hash", phase="assemble_request", payload={"iteration":1})
    hub.diagnostic(early)
    hub.register("s", "turn")
    initial, cursor = hub.connect("s")
    assert initial[0]["payload"]["turns"][0]["phase"] == "assemble_request"
    for kind, text in [("model.started",""),("model.delta","a"),("model.delta","b"),("model.accepted","abc")]:
        hub.output(ModelOutputEvent(kind,"s","turn",1,"attempt","route",text))
    replay, _ = hub.connect("s",f"{hub.epoch}:{cursor}")
    assert replay[0]["kind"] == "reset" and replay[0]["payload"]["turns"][0]["attempts"]["attempt"]["text"] == "abc"
    _, cursor = hub.connect("s")
    hub.publish("turn.finished","s","turn",{"status":"completed","final_text":"abc"})
    events, _ = hub.wait("s",cursor,0)
    assert events[0]["kind"] == "turn.finished"
    hub.output(ModelOutputEvent("model.delta","s","turn",1,"attempt","route","late"))
    assert hub.turn("turn")["attempts"]["attempt"]["text"] == "abc"
    assert hub.connect("s","different:1")[0][0]["kind"] == "reset"
    hub.publish("turn.queued","other","other-turn",{})
    assert not hub.wait("s",hub._seq-1,0)[0]


def test_hub_snapshot_boundary_and_byte_limit():
    hub = WebEventHub(max_bytes=200,capacity=100)
    hub.register("s","t")
    snapshot,cursor = hub.connect("s")
    hub.output(ModelOutputEvent("model.delta","s","t",1,"a","route","x"*500))
    events,_ = hub.wait("s",cursor,0)
    assert events[0]["kind"] == "reset"
    assert events[0]["payload"]["turns"][0]["attempts"]["a"]["text"] == "x"*500
    assert hub._bytes <= 200
    hub.close()
    assert hub.closed


def test_expired_interaction_reconciliation_keeps_cancelling_and_terminal_states():
    hub=WebEventHub()
    hub.register("s","t")
    hub.publish("interaction.pending","s","t",{})
    assert hub.turn("t")["status"]=="waiting"
    hub.reconcile_interactions("s",())
    assert hub.turn("t")["status"]=="running"
    hub.publish("turn.cancel_requested","s","t",{})
    hub.reconcile_interactions("s",())
    assert hub.turn("t")["status"]=="cancelling"


def test_http_static_write_validation_and_sse(web_factory):
    host,_ = web_factory()
    server = WebServer(host,0)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(method,path,body=None,headers=None):
        conn = http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
        conn.request(method,path,json.dumps(body) if body is not None else None,
                     {"Content-Type":"application/json",**(headers or {})})
        response=conn.getresponse();data=response.read();conn.close();return response.status,data,response.headers
    try:
        assert request("GET","/api/status")[0] == 200
        status,data,headers=request("GET","/app.js")
        assert status==200 and "javascript" in headers["Content-Type"] and b"createStore" in data
        assert request("GET","/%2e%2e/host.py")[0] == 404
        assert request("GET","/api/status",headers={"Host":"attacker.example"})[0]==403
        assert request("POST","/api/turns",{},headers={"Origin":"https://evil.example"})[0]==403
        assert request("POST","/api/turns",{},headers={"Content-Type":"text/plain"})[0]==422
        status,data,_=request("POST","/api/turns",{"session_id":"http","text":"hello","client_request_id":"http"})
        assert status==202;tid=json.loads(data)["turn_id"]
        wait_for(lambda: host.turn(tid)["result"])
        conn=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=3)
        conn.request("GET","/api/events?session_id=http")
        response=conn.getresponse();assert response.status==200
        lines=[response.readline().decode() for _ in range(4)]
        event=json.loads(lines[2].removeprefix("data: "))
        assert event["kind"]=="snapshot" and event["payload"]["turns"][0]["status"]=="completed"
        conn.close()
    finally:
        server.stop();server.server_close();thread.join(2)


@pytest.mark.parametrize("failure",["interrupt","bind_error"])
def test_web_cli_cleans_started_resources_on_interrupt_or_bind_error(config_factory,monkeypatch,failure):
    from types import SimpleNamespace
    from agentcore import cli
    import gateway.web as web
    from file_memory.feature import FileMemoryConfig
    from tools.lifecycle import CleanupReport
    config=config_factory();calls=[];budgets=[]
    monkeypatch.setattr(cli,"load_config",lambda path:config)
    monkeypatch.setattr(cli,"load_file_memory_config",lambda path:FileMemoryConfig())
    monkeypatch.setattr(cli,"configure_logging",lambda cfg:SimpleNamespace(close=lambda timeout:calls.append("logging")))
    original=cli.shutdown_tool_system
    def close_tools(timeout):
        calls.append("tools");budgets.append(timeout);return original(timeout=timeout)
    monkeypatch.setattr(cli,"shutdown_tool_system",close_tools)
    class Host:
        accepting=True
        def __init__(self,*args,**kwargs):self.events=SimpleNamespace(close=lambda:calls.append("events"))
        def start(self):calls.append("start")
        def close(self,timeout):calls.append("gateway");budgets.append(timeout);return CleanupReport()
    class Server:
        server_port=7777
        def __init__(self,*args):
            if failure=="bind_error":raise OSError("port busy")
        def serve_forever(self):raise KeyboardInterrupt()
        def server_close(self):calls.append("http")
    monkeypatch.setattr(web,"WebHost",Host);monkeypatch.setattr(web,"WebServer",Server)
    args=SimpleNamespace(port=7777,config="unused")
    if failure=="bind_error":
        with pytest.raises(OSError):cli._web(args)
    else:assert cli._web(args)==130
    assert calls==["start","gateway","tools","events"]+(["http"] if failure=="interrupt" else [])+["logging"]
    assert budgets[1]<=budgets[0]<=config.timeouts.finalize_seconds
