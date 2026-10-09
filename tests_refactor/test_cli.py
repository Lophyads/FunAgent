from io import StringIO
from funagent.api.cli import CLIController
from fakes import ScriptedProvider, calls, final


def test_cli_submit_query_complete_reply_history(task_app):
    mgr, _, session, _ = task_app(ScriptedProvider(calls("chat","c1"), final("chat","完整回复\n第二行")))
    output = StringIO()
    CLIController.Execute(f"submit {session.session_id} hello", output)
    task_id = output.getvalue().strip()
    output = StringIO()
    CLIController.Execute(f"wait {task_id}", output)
    assert "completed" in output.getvalue() and "完整回复\n第二行" in output.getvalue()
    output = StringIO()
    CLIController.Execute(f"history {session.session_id}", output)
    assert "hello" in output.getvalue() and "完整回复\n第二行" in output.getvalue()
    output = StringIO()
    CLIController.Execute(f"get {task_id}", output)
    assert "完整回复\n第二行" in output.getvalue()


def test_cli_create_list_missing_and_bad_commands(task_app):
    task_app(ScriptedProvider())
    output=StringIO()
    CLIController.Execute("new 我的会话", output)
    session_id = output.getvalue().strip()
    output=StringIO()
    CLIController.Execute("sessions", output)
    assert session_id in output.getvalue() and "我的会话" in output.getvalue()
    commands=iter(["get missing", "submit", "invalid", "quit"])
    output=StringIO()
    CLIController.Run(lambda prompt: next(commands), output)
    assert "NOT_FOUND" in output.getvalue() and "INVALID_REQUEST" in output.getvalue()


import pytest
from funagent.conf import ConfMgr
from funagent.contracts import DomainError, SessionCreate, TurnRequest
from funagent.global_server import Start, Stop, main
from funagent.model import ModelRuntime
from funagent.persistence import DbMgr
from funagent.task import TaskMgr
from funagent.session import SessionMgr
from test_task_flow import wait_task


def test_cli_production_start_offline_no_tools_and_tools(cli_conf):
    try:
        Start(cli_conf)
        output = StringIO()
        CLIController.Execute("new smoke", output)
        session_id = output.getvalue().strip()
        for text, expected in (("hello", "Demo: hello"), ("tool: echo me", "echo me")):
            output = StringIO()
            CLIController.Execute(f"submit {session_id} {text}", output)
            task = wait_task(TaskMgr.GetInstance(), output.getvalue().strip())
            assert task.status == "completed" and task.result.final == expected
        assert len([m for m in SessionMgr.ReadHistory(session_id).items if m.role == "tool"]) == 1
    finally:
        Stop()
    for resource in (ConfMgr, DbMgr, ModelRuntime, TaskMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()


def test_main_is_runnable_without_check_flag(cli_conf, monkeypatch, capsys):
    commands=iter(["new main smoke", "sessions", "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(commands))
    assert main(["--conf-dir", str(cli_conf)]) == 0
    assert "main smoke" in capsys.readouterr().out


def test_failed_start_cleans_up_then_can_start_again(cli_conf, monkeypatch):
    from funagent import global_server
    from threading import Thread
    with monkeypatch.context() as patch:
        patch.setattr(Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("SECRET_TOKEN")))
        with pytest.raises(DomainError) as error:
            Start(cli_conf)
        assert "SECRET_TOKEN" not in str(error.value)
    for resource in (ConfMgr, DbMgr, ModelRuntime, TaskMgr):
        with pytest.raises(DomainError):
            resource.GetInstance()
    Start(cli_conf)
    Stop()


def test_real_provider_missing_credential_cleans_up(cli_conf, monkeypatch):
    from pathlib import Path
    model = (Path(__file__).parents[1] / "examples/refactor-openai/model.conf").read_text()
    (cli_conf / "model.conf").write_text(model)
    p=cli_conf/"memory.conf";p.write_text(p.read_text().replace('"fallback"','"main"'))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DomainError) as error:
        Start(cli_conf)
    assert error.value.code == "CONFIG_CREDENTIAL"
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()



def test_shutdown_persistence_fault_still_releases_resources_after_drain(cli_conf, monkeypatch):
    from threading import Event
    Start(cli_conf)
    store=DbMgr.GetInstance()
    original=store.FinishTurn
    failed=Event()
    def fail(*args):
        failed.set()
        raise DomainError("STORAGE","injected")
    monkeypatch.setattr(store,"FinishTurn",fail)
    session=SessionMgr.Create(SessionCreate())
    task_id=TaskMgr.GetInstance().Submit(TurnRequest(session.session_id,"hello"))
    assert failed.wait(3)
    try:
        with pytest.raises(DomainError):
            Stop()
        for resource in (ConfMgr,DbMgr,ModelRuntime,TaskMgr):
            with pytest.raises(DomainError):
                resource.GetInstance()
    finally:
        monkeypatch.setattr(store,"FinishTurn",original)
        Stop()



def test_real_sdk_http_turn_persists_pairs_and_complete_reply(cli_conf, monkeypatch):
    import httpx
    import json
    from pathlib import Path
    from funagent import global_server
    from funagent.model.openai_chat import OpenAIProvider
    from test_provider_contract import completion
    model=(Path(__file__).parents[1]/"examples/refactor-openai/model.conf").read_text()
    (cli_conf/"model.conf").write_text(model)
    p=cli_conf/"memory.conf";p.write_text(p.read_text().replace('"fallback"','"main"'))
    monkeypatch.setenv("OPENAI_API_KEY","test-token")
    captured=[]
    def transport(request):
        payload=json.loads(request.content)
        captured.append(payload)
        if len(captured)==1:
            invocation=[{"id":"real-c1","type":"function","function":{"name":"echo","arguments":'{"value":"真实协议"}'}}]
            return httpx.Response(200,json=completion(None,invocation,"tool_calls"))
        assert [m["role"] for m in payload["messages"]]==["system","user","assistant","tool"]
        assert payload["messages"][-1]["tool_call_id"]=="real-c1"
        return httpx.Response(200,json=completion("完整真实协议回复"))
    monkeypatch.setitem(global_server.PROVIDERS,"openai", lambda settings: OpenAIProvider(settings,transport=httpx.MockTransport(transport)))
    try:
        Start(cli_conf)
        session=SessionMgr.Create(SessionCreate())
        mgr=TaskMgr.GetInstance()
        task=wait_task(mgr,mgr.Submit(TurnRequest(session.session_id,"hello")))
        assert task.status=="completed" and task.result.final=="完整真实协议回复"
        history=SessionMgr.ReadHistory(session.session_id).items
        assert [m.role for m in history]==["user","assistant","tool","assistant"]
        assert history[2].call_id=="real-c1" and history[2].content=="真实协议"
        assert task.result.usage["total_tokens"]==10
        assert len(captured)==2
    finally:
        Stop()
