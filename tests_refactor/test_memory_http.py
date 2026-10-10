"""Real urllib wire IO to a controlled local knowledge endpoint, no credentials."""
from io import BytesIO
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import time

import pytest

from funagent.contracts import ControlError, DomainError, RunContext, SessionCreate, TurnRequest
from funagent.memory import CallbackMemoryStore, MemoryQuery, MemoryMgr, MemoryDraft
from funagent.memory.http_query import HTTPMemoryQuery
from funagent.conf import ConfMgr
from funagent.global_server import Start, Stop
from funagent.session import SessionMgr
from funagent.task import TaskMgr
from test_task_flow import wait_task


@pytest.fixture
def knowledge_endpoint():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf8"))))
            body = json.dumps({"records": [{"record_id": "doc-1", "type": "document", "content": "外部真实返回证据",
                                          "citation": "https://kb.example/docs/1"}]}, ensure_ascii=False).encode("utf8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, name="test-knowledge")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/query", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def test_real_http_query_search_contract_and_cancel_before_network(knowledge_endpoint):
    endpoint, requests = knowledge_endpoint
    store = CallbackMemoryStore("kb", HTTPMemoryQuery({"endpoint": endpoint, "timeout": 1}))
    try:
        result = store.Search(MemoryQuery("中文问题", ("kb",), "semantic", ("document",), 3), RunContext(deadline=time.monotonic() + 3))
        assert result.records[0].ref.source_id == "kb" and result.records[0].content == "外部真实返回证据"
        assert requests == [("/query", {"text": "中文问题", "types": ["document"], "limit": 3})]
        cancel = Event(); cancel.set()
        with pytest.raises(ControlError):
            store.Search(MemoryQuery(), RunContext(cancel=cancel))
        assert len(requests) == 1
    finally:
        store.Close()


@pytest.mark.parametrize("model_driver", ["demo", "openai"])
def test_production_cli_turn_uses_real_http_semantic_and_sqlite_profile(cli_conf, knowledge_endpoint, monkeypatch, model_driver):
    endpoint, requests = knowledge_endpoint
    file = cli_conf / "memory.conf"
    file.write_text(file.read_text().replace('enabled = false', 'enabled = true').replace('sources = ["facts"]', 'sources = ["facts", "kb"]', 1) + f'''
[sources.kb]
kind = "semantic"
store = "callback"
callback_id = "http_json"
endpoint = "{endpoint}"
capabilities = ["search"]
read_access = "memory.kb.read"
timeout = 1.0
''', encoding="utf8")
    model_requests = []
    if model_driver == "openai":
        from pathlib import Path
        import httpx
        from funagent import global_server
        from funagent.model.openai_chat import OpenAIProvider
        from test_provider_contract import completion
        (cli_conf / "model.conf").write_text((Path(__file__).parents[1] / "examples/refactor-openai/model.conf").read_text(), encoding="utf8")
        monkeypatch.setenv("OPENAI_API_KEY", "test-token")
        def transport(request):
            payload = json.loads(request.content)
            model_requests.append(payload)
            if len(model_requests) == 1:
                invocation = [{"id": "recall-call", "type": "function", "function": {"name": "memory_search", "arguments": '{"text":"中文"}'}}]
                return httpx.Response(200, json=completion(None, invocation, "tool_calls"))
            if len(model_requests) == 2:
                return httpx.Response(200, json=completion("MODEL_GENERATED_UNTRUSTED"))
            assert "外部真实返回证据" in payload["messages"][0]["content"]
            assert "用户喜欢中文" in payload["messages"][0]["content"]
            assert "MODEL_GENERATED_UNTRUSTED" not in payload["messages"][0]["content"]
            assert [m["role"] for m in payload["messages"]] == ["system", "user"]
            return httpx.Response(200, json=completion("用户喜欢中文；外部真实返回证据 https://kb.example/docs/1"))
        monkeypatch.setitem(global_server.PROVIDERS, "openai", lambda settings: OpenAIProvider(settings, transport=httpx.MockTransport(transport)))
    Start(cli_conf)
    MemoryMgr.GetInstance().Write(MemoryDraft("facts", "用户喜欢中文", "language", ("manual:1",), "profile"),
                                  RunContext(access=frozenset({"memory.facts.write"})))
    session = SessionMgr.Create(SessionCreate())
    manager = TaskMgr.GetInstance()
    task = wait_task(manager, manager.Submit(TurnRequest(session.session_id, "memory: 中文")))
    assert task.status == "completed" and "外部真实返回证据" in task.result.final and "用户喜欢中文" in task.result.final
    assert "https://kb.example/docs/1" in task.result.final
    assert len(requests) == 1
    if model_driver == "openai":
        assert len(model_requests) == 3
    assert [m.role for m in SessionMgr.ReadHistory(session.session_id).items] == ["user", "assistant"]


@pytest.mark.parametrize("body", [b"not json", b'{}', b'{"records":null}', b'x' * 1048577], ids=["invalid-json", "missing-records", "wrong-shape", "oversize"])
def test_http_query_protocol_errors_do_not_leak_response(body):
    query = HTTPMemoryQuery({"endpoint": "http://kb.example/query"}, transport=lambda *args, **kwargs: BytesIO(body))
    with pytest.raises(DomainError) as error:
        query(MemoryQuery(), RunContext())
    assert error.value.code == "MEMORY_PROTOCOL"
    assert "not json" not in str(error.value)


def test_http_query_binds_credential_once_and_uses_remaining_budget(monkeypatch):
    captured = []
    monkeypatch.setenv("KB_TEST_KEY", "FIRST_TOKEN")
    def transport(request, timeout):
        captured.append((request, timeout))
        return BytesIO(b'{"records":[]}')
    query = HTTPMemoryQuery({"endpoint": "https://kb.example/query", "credential_env": "KB_TEST_KEY", "timeout": 5}, transport=transport)
    monkeypatch.setenv("KB_TEST_KEY", "CHANGED_TOKEN")
    assert query(MemoryQuery("q"), RunContext(deadline=time.monotonic() + 0.5)) == []
    request, timeout = captured[0]
    assert request.get_header("Authorization") == "Bearer FIRST_TOKEN"
    assert 0 < timeout <= 0.5


def test_http_query_transport_error_and_missing_credential_are_public(monkeypatch):
    monkeypatch.delenv("KB_MISSING_KEY", raising=False)
    with pytest.raises(DomainError) as error:
        HTTPMemoryQuery({"endpoint": "https://kb.example/query", "credential_env": "KB_MISSING_KEY"})
    assert error.value.code == "CONFIG_CREDENTIAL"
    def fail(*args, **kwargs):
        raise OSError("SECRET_TOKEN")
    query = HTTPMemoryQuery({"endpoint": "https://kb.example/query"}, transport=fail)
    with pytest.raises(DomainError) as error:
        query(MemoryQuery(), RunContext())
    assert error.value.code == "MEMORY_SOURCE" and "SECRET_TOKEN" not in str(error.value)
