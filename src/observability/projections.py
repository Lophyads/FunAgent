"""Small business-field projections, independent of writer assembly."""
from itertools import islice
from collections.abc import Mapping


def ToolCall(call):
    return dict(call_id=call.call_id, name=call.name, arguments=call.args, argument_error=call.argument_error)


def ToolOutput(result):
    return dict(call_id=result.call_id, status=result.status, content=result.content, effect=result.effect)


def Messages(messages):
    return [dict(role=m.role, content=m.content, message_id=m.message_id, call_id=m.call_id,
                 status=m.status, effect=m.effect, calls=[ToolCall(c) for c in islice(m.calls, 129)])
            for m in islice(messages, 129)]


def Request(request):
    return dict(messages=Messages(request.messages), tools=Definitions(request.tools))


def Definitions(definitions):
    return [dict(name=t.name, description=t.description, parameters=t.parameters) for t in islice(definitions, 129)]


def Reply(reply):
    return dict(content=reply.content, calls=[ToolCall(c) for c in islice(reply.calls, 129)],
                finish_reason=reply.finish_reason)


def Wire(value):
    if isinstance(value, Mapping):
        # Payload size and credential handling are bounded by the hub.
        return value
    if hasattr(value, 'messages'):
        return Request(value)
    if hasattr(value, 'calls'):
        return Reply(value)
    return None


def Usage(value):
    if isinstance(value, Mapping):
        return value.get('usage') or {}
    return getattr(value, 'usage', {}) or {}


def StreamHasOutput(payload):
    """Role-only and empty packets do not count as first semantic output."""
    try:
        return any(choice.get('delta', {}).get('content') or any(
            call.get('function', {}).get('arguments') for call in choice.get('delta', {}).get('tool_calls', ()))
            for choice in payload.get('choices', ()))
    except (TypeError, AttributeError):
        return False


def RunOutput(result):
    return dict(status=result.status, final=result.final, reason=result.reason, error_code=result.error_code,
                iterations=result.iterations, attempts=result.attempts, profile_id=result.profile_id,
                tool_results=[ToolOutput(t) for t in islice(result.tool_results, 129)])


def Query(query):
    return {key: getattr(query, key) for key in ('text', 'source_ids', 'kind', 'types', 'limit',
            'include_deleted', 'subject', 'event_from', 'event_to', 'origin')}


def Memory(result):
    from ..memory.prompt import RecordData
    return dict(records=[RecordData(r) for r in islice(result.records, 129)],
                source_status=result.source_status, warnings=result.warnings,
                status=getattr(result, 'status', None),
                queries=[Query(q) for q in islice(getattr(result, 'queries', ()), 129)])


def Plan(plan):
    return {key: getattr(plan, key) for key in ('need_memory', 'need_knowledge', 'memory_query',
                                             'knowledge_query', 'variant', 'degraded', 'warnings')}


def TaskOutput(task):
    result = task.result
    tools = result.tool_results if result else ()
    return dict(status=task.status, final=result.final if result else None,
                reason=result.reason if result else None, error_code=result.error_code if result else None,
                tool_results=[dict(call_id=item.call_id, status=item.status, content=item.content, effect=item.effect)
                              for item in islice(tools, 129)],
                tool_results_omitted=max(0, len(tools) - 128))
