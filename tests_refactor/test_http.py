"""Real loopback HTTP exercises the shared application APIs, never a fake server."""
from contextlib import contextmanager
from io import StringIO
from threading import Event, current_thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import pytest

from funagent.api.http import HTTPController, HttpServer
from funagent.api.cli import CLIController
from funagent.contracts import DomainError
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from funagent import global_server
from fakes import ScriptedProvider, ChatAdapter, final
from test_task_flow import wait_task


@contextmanager
def serving():
    server = HttpServer("127.0.0.1", 0)
    try:
        server.Start()
        yield server
    finally:
        server.Close()


def request(server, method, target, body=None, *, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": "application/json", **(headers or {})}
    req = Request(server.url + target, data=data, headers=headers, method=method)
    try:
        response = urlopen(req, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        content = response.read()
        payload = json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content.decode()
        return response.status, payload, dict(response.headers)


def test_controller_creates_lists_pages_without_exposing_prompt_or_workspace(task_app):
    task_app(ScriptedProvider())
    status, session = HTTPController.Handle("POST", "/sessions", {"title": "浏览器会话"})
    assert status == 201 and set(session) == {"session_id", "title", "created_at"}
    assert session["title"] == "浏览器会话"
    status, page = HTTPController.Handle("GET", "/sessions?limit=1")
    assert status == 200 and len(page["items"]) == 1 and page["next_cursor"]
    _, rest = HTTPController.Handle("GET", "/sessions?limit=1&cursor=" + page["next_cursor"])
    assert rest["items"][0]["session_id"] == session["session_id"]


def test_submit_returns_202_before_model_finishes_and_executes_on_main_worker(cli_conf, monkeypatch):
    entered, release = Event(), Event()
    threads = []
    def block(request, context):
        threads.append(current_thread().name)
        entered.set()
        assert release.wait(5)
        from funagent.model import ProviderResponse
        return ProviderResponse(final("chat", "完整回复\n第二行"))
    provider = ScriptedProvider(block)
    monkeypatch.setitem(global_server.PROVIDERS, "demo", lambda settings: provider)
    monkeypatch.setitem(global_server.ADAPTERS, "demo", ChatAdapter)
    mgr = global_server.Start(cli_conf)
    try:
        with serving() as server:
            status, session, _ = request(server, "POST", "/sessions", {"title": "web"})
            assert status == 201
            identifier = session["session_id"]
            status, submitted, _ = request(server, "POST", f"/sessions/{identifier}/tasks", {"text": "hello"})
            assert status == 202 and set(submitted) == {"task_id"}
            task_id = submitted["task_id"]
            assert entered.wait(3) and mgr.Get(task_id).status == "running"
            status, snapshot, _ = request(server, "GET", "/tasks/" + task_id)
            assert status == 200 and snapshot["status"] == "running" and snapshot["result"] is None
            assert threads == ["funagent-main"]
            # Closing all HTTP connections and the listener does not cancel work.
        assert mgr.Get(task_id).status == "running"
        release.set()
        assert wait_task(mgr, task_id).result.final == "完整回复\n第二行"
        with serving() as reconnected:
            status, snapshot, _ = request(reconnected, "GET", "/tasks/" + task_id)
            assert status == 200 and snapshot["result"]["final"] == "完整回复\n第二行"
            assert set(snapshot["result"]) == {"final", "reason", "error_code"}
            status, history, _ = request(reconnected, "GET", f"/sessions/{identifier}/messages")
            assert status == 200 and [m["role"] for m in history["items"]] == ["user", "assistant"]
            assert history["items"][-1]["content"] == "完整回复\n第二行"
            out = StringIO()
            CLIController.Execute(f"get {task_id}", out)
            assert "完整回复" in out.getvalue()
    finally:
        release.set()


@pytest.mark.parametrize("target,body", [
    ("/sessions", {"title": "x", "workspace": "C:/"}),
    ("/sessions", {"title": "x", "prompt": "forged"}),
    ("/sessions", {"title": 3}),
    ("/sessions", []),
    ("/sessions", {"title": ""}),
    ("/sessions", {"title": "x" * 201}),
    ("/sessions/{session}/tasks", {"text": "hello", "access": ["admin"]}),
    ("/sessions/{session}/tasks", {"text": "hello", "record_scope": "ephemeral"}),
    ("/sessions/{session}/tasks", {"text": "hello", "handler": "danger"}),
    ("/sessions/{session}/tasks", {"text": "hello", "profile": "review"}),
    ("/sessions/{session}/tasks", {"text": ""}),
    ("/sessions/{session}/tasks", {"text": "x" * 16001}),
])
def test_invalid_or_forged_business_fields_are_rejected(task_app, target, body):
    mgr, _, session, _ = task_app(ScriptedProvider())
    with serving() as server:
        status, value, _ = request(server, "POST", target.format(session=session.session_id), body)
        assert status == 400 and value["error"]["code"] == "INVALID_REQUEST"
    assert SessionMgr.ReadHistory(session.session_id).items == ()


@pytest.mark.parametrize("target,code", [("/tasks/missing", "NOT_FOUND"), ("/sessions/missing/messages", "NOT_FOUND"),
    ("/tasks/missing/interactions", "NOT_FOUND"), ("/sessions?limit=0", "INVALID_REQUEST"),
    ("/sessions?limit=1&limit=2", "INVALID_REQUEST"), ("/sessions?cursor=invalid", "INVALID_REQUEST"),
    ("/sessions?access=admin", "INVALID_REQUEST")])
def test_stable_missing_object_and_pagination_errors(task_app, target, code):
    task_app(ScriptedProvider())
    with serving() as server:
        status, value, _ = request(server, "GET", target)
        assert status == (404 if code == "NOT_FOUND" else 400)
        assert value["error"]["code"] == code


@pytest.mark.parametrize("failure", [RuntimeError("SECRET_TOKEN prompt=private"), DomainError("STORAGE", "SECRET_TOKEN"), DomainError("SECRET_TOKEN", "private")])
def test_unexpected_errors_are_sanitized_and_listener_remains_usable(task_app, monkeypatch, failure):
    task_app(ScriptedProvider())
    with serving() as server:
        with monkeypatch.context() as patch:
            patch.setattr(SessionMgr, "List", lambda *args: (_ for _ in ()).throw(failure))
            status, value, _ = request(server, "GET", "/sessions")
            assert status == 500 and "SECRET_TOKEN" not in json.dumps(value) and "private" not in json.dumps(value)
        assert request(server, "GET", "/sessions")[0] == 200


def test_bad_json_oversized_body_and_cross_origin_requests_are_rejected(task_app):
    task_app(ScriptedProvider())
    with serving() as server:
        for raw, headers, expected in [(b"{", {}, 400), (b'null', {}, 400),
            (b'{"title":"a","title":"b"}', {}, 400), (b"x" * 65537, {}, 413),
            (b'{}', {"Content-Type": "text/plain"}, 415),
            (b'{}', {"Origin": "https://untrusted.example"}, 403)]:
            status, value, _ = request(server, "POST", "/sessions", raw=raw, headers=headers)
            assert status == expected and set(value) == {"error"}
        status, _, headers = request(server, "GET", "/")
        assert status == 200 and "Content-Security-Policy" in headers and headers["Cache-Control"] == "no-store"


def test_capacity_and_stopping_have_public_errors(task_app):
    entered, release = Event(), Event()
    def block(request, context):
        entered.set()
        assert release.wait(5)
        from funagent.model import ProviderResponse
        return ProviderResponse(final("chat"))
    mgr, _, session, _ = task_app(ScriptedProvider(block), capacity=1)
    try:
        with serving() as server:
            target = f"/sessions/{session.session_id}/tasks"
            task_id = request(server, "POST", target, {"text": "one"})[1]["task_id"]
            assert entered.wait(3)
            status, value, _ = request(server, "POST", target, {"text": "two"})
            assert status == 429 and value["error"]["code"] == "CAPACITY"
            release.set()
            wait_task(mgr, task_id)
            mgr.Stop()
            status, value, _ = request(server, "POST", target, {"text": "late"})
            assert status == 503 and value["error"]["code"] == "STOPPING"
    finally:
        release.set()


def test_too_many_query_fields_and_unsupported_methods_are_public_request_errors(task_app):
    task_app(ScriptedProvider())
    with serving() as server:
        target = "/sessions?" + "&".join(f"q{i}=x" for i in range(11))
        status, value, _ = request(server, "GET", target)
        assert status == 400 and value["error"]["code"] == "INVALID_REQUEST"
        status, value, _ = request(server, "PUT", "/sessions", {})
        assert status == 405 and value["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_same_origin_json_and_missing_cancel_have_stable_results(task_app):
    task_app(ScriptedProvider())
    with serving() as server:
        assert request(server, "POST", "/sessions", {"title": "same origin"}, headers={"Origin": server.url})[0] == 201
        status, value, _ = request(server, "POST", "/tasks/missing/cancel", {})
        assert status == 404 and value["error"]["code"] == "NOT_FOUND"
