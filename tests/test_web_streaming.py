from __future__ import annotations

import threading
import time
from types import SimpleNamespace as NS

import pytest

from agentcore.interfaces import CancellationToken, TurnCancelled, TurnHostContext
from agentcore.provider_runtime import AttemptContext, NormalizedProviderError, OpenAIHttpTransport, WireRequest
from conftest import ScriptedTransport


class Stream:
    def __init__(self, chunks):
        self.chunks=chunks;self.closed=threading.Event()
    def __iter__(self):
        yield from self.chunks
    def close(self):
        self.closed.set()


def chunk(text=None,tools=None,finish=None,usage=None,index=0):
    return NS(choices=[] if usage else [NS(index=index,delta=NS(content=text,tool_calls=tools),finish_reason=finish)],usage=usage)


def tool(index,id=None,name=None,args=None):
    return NS(index=index,id=id,function=NS(name=name,arguments=args))


def transport_for(stream):
    calls=[]
    def create(**body):
        calls.append(body);return stream
    return OpenAIHttpTransport(api_key="unused",base_url=None,timeout_seconds=1,
        client=NS(chat=NS(completions=NS(create=create)))),calls


def context(cancel=None,deadline=None):
    return AttemptContext("a","s","t",1,"primary",1,deadline,CancellationToken(cancel or threading.Event()))


def test_sdk_stream_aggregates_text_tool_indices_and_usage():
    usage=NS(model_dump=lambda **kwargs:{"total_tokens":9})
    stream=Stream([chunk("hi "),chunk("there",[tool(1,"c2","echo",'{"x":'),tool(0,"c1","calc","{")]),
                   chunk(tools=[tool(0,args='}'),tool(1,args='2}')]),chunk(finish="tool_calls"),chunk(usage=usage)])
    transport,calls=transport_for(stream);text=[]
    wire=transport.stream(WireRequest({"model":"offline","messages":[]},2),context(),text.append)
    raw=wire.raw
    assert text==["hi ","there"] and raw.choices[0].message.content=="hi there"
    assert [(t.id,t.function.name,t.function.arguments) for t in raw.choices[0].message.tool_calls]==[("c1","calc","{}"),("c2","echo",'{"x":2}')]
    assert raw.usage is usage and raw.choices[0].finish_reason=="tool_calls" and stream.closed.is_set()
    assert calls[0]["stream"] and calls[0]["stream_options"]["include_usage"]


@pytest.mark.parametrize("finish",["stop","length","content_filter"])
def test_finish_reason_is_preserved(finish):
    transport,_=transport_for(Stream([chunk("text"),chunk(finish=finish)]))
    assert transport.stream(WireRequest({},1),context(),lambda text:None).raw.choices[0].finish_reason==finish


def test_incomplete_stream_is_not_accepted():
    transport,_=transport_for(Stream([chunk("partial")]))
    with pytest.raises(NormalizedProviderError) as exc:
        transport.stream(WireRequest({},1),context(),lambda text:None)
    assert exc.value.error_type=="incomplete_stream"


def test_explicit_unsupported_stream_downgrades_once_before_output():
    import httpx
    from openai import BadRequestError
    calls=[]
    raw=NS(choices=[NS(message=NS(content="plain",tool_calls=[]),finish_reason="stop")],usage=None)
    def create(**body):
        calls.append(body)
        if body.get("stream"):
            raise BadRequestError("unsupported", response=httpx.Response(400,request=httpx.Request("POST","https://offline.invalid")),
                                  body={"message":"stream is not supported"})
        return raw
    transport=OpenAIHttpTransport(api_key="unused",base_url=None,timeout_seconds=1,client=NS(chat=NS(completions=NS(create=create))))
    text=[];response=transport.stream(WireRequest({},1),context(),text.append)
    assert response.raw is raw and len(calls)==2 and text==[]


def test_streamed_tool_arguments_execute_only_after_complete_response(config_factory):
    from agentcore import AgentCore, ModelRoute, ProviderRuntime, StaticProviderResolver
    from tools.runtime import initialize_tool_system
    from tools.config import ToolSystemConfig
    config=config_factory(routes=[ModelRoute(name="primary",provider="test",model="model",api_mode="chat_completions",credential_ref="env:UNUSED")])
    initialize_tool_system(ToolSystemConfig.from_agent_config(config))
    complete=threading.Event();calls=[]
    class ToolStream(Stream):
        def __iter__(self):
            yield chunk(tools=[tool(0,"c1","calculator",'{"expression":"1')])
            yield chunk(tools=[tool(0,args='+2"}')])
            complete.set();yield chunk(finish="tool_calls")
    streams=[ToolStream([]),Stream([chunk("3"),chunk(finish="stop")])]
    def create(**body):
        if calls:
            assert complete.is_set()
            assert body["messages"][-1]["role"]=="tool"
            assert '3' in body["messages"][-1]["content"]
        calls.append(body);return streams.pop(0)
    transport=OpenAIHttpTransport(api_key="unused",base_url=None,timeout_seconds=1,client=NS(chat=NS(completions=NS(create=create))))
    resolver=StaticProviderResolver({"primary":ProviderRuntime("test","model","offline","chat_completions",frozenset({"chat_completions","tools"}),transport)})
    with AgentCore(config,provider_resolver=resolver) as agent:
        result=agent.run_turn_blocking("calculate",host_context=TurnHostContext(on_output=lambda e:None))
    assert result.status=="completed" and result.final_text=="3" and len(result.tool_outcomes)==1


def test_cancel_closes_blocked_stream_and_emits_no_late_text():
    cancel=threading.Event();started=threading.Event()
    class Blocked(Stream):
        def __iter__(self):
            started.set();self.closed.wait(2);yield chunk("late")
    stream=Blocked([]);transport,_=transport_for(stream);text=[];errors=[]
    def run():
        try:transport.stream(WireRequest({},2),context(cancel),text.append)
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start();assert started.wait(1);cancel.set();thread.join(1)
    assert not thread.is_alive() and isinstance(errors[0],TurnCancelled) and text==[] and stream.closed.is_set()


def test_deadline_closes_blocked_stream():
    class Blocked(Stream):
        def __iter__(self):
            self.closed.wait(2);yield chunk("late")
    stream=Blocked([]);transport,_=transport_for(stream)
    from agentcore.turn_state import RunBudgetExceeded
    with pytest.raises(RunBudgetExceeded):
        transport.stream(WireRequest({},2),context(deadline=time.monotonic()+0.05),lambda text:None)
    assert stream.closed.is_set()


def test_callback_isolation_and_nonstream_transport_compatibility(config_factory,agent_factory,final_response):
    calls=[]
    def failing(event):
        calls.append(event.kind);raise ValueError("UI is unavailable")
    transport=ScriptedTransport(final_response)
    with agent_factory(config_factory(),transports={"primary":transport}) as agent:
        result=agent.run_turn_blocking("hello",host_context=TurnHostContext(on_output=failing))
    assert result.status=="completed" and len(transport.requests)==1 and calls==["model.started","model.accepted"]


def test_stream_retry_attempts_do_not_mix_and_none_uses_send(config_factory,agent_factory,final_response):
    class Streaming(ScriptedTransport):
        streams=0
        def stream(self,request,context,on_delta):
            self.streams+=1
            on_delta("discard" if self.streams==1 else "done")
            if self.streams==1:raise NormalizedProviderError("temporary",retryable=True,allow_fallback=True)
            return self.send(request,context)
    from agentcore.models import RetryPolicy
    transport=Streaming(final_response,final_response);events=[]
    config=config_factory(retry=RetryPolicy(max_attempts=2,base_delay_seconds=0,max_delay_seconds=0,jitter_ratio=0))
    with agent_factory(config,transports={"primary":transport}) as agent:
        first=agent.run_turn_blocking("stream",host_context=TurnHostContext(on_output=events.append))
        second=agent.run_turn_blocking("plain")
    assert first.status==second.status=="completed" and transport.streams==2
    assert [e.kind for e in events]==["model.started","model.delta","model.discarded","model.started","model.delta","model.accepted"]
    assert events[0].attempt_id!=events[3].attempt_id
