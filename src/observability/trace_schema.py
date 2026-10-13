"""Validate saved snapshots without drivers, threads or IO."""
import json
import re
from math import isfinite
from .contracts import TraceId
from .trace_projection import TraceNodes


class TraceFormatError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _require(condition):
    if not condition:
        raise ValueError('invalid trace shape')


def _number(text):
    value = float(text)
    _require(isfinite(value))
    return value


def Decode(text, expected_id):
    try:
        trace = json.loads(text, object_pairs_hook=_object, parse_float=_number,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise TraceFormatError('invalid_json') from None
    if not isinstance(trace, dict):
        raise TraceFormatError('invalid_trace')
    if type(trace.get('schema_version')) is not int or trace['schema_version'] != 1:
        raise TraceFormatError('unsupported_schema')
    try:
        _require(trace['trace_id'] == expected_id)
        _require(isinstance(trace['task_id'], str) and trace['task_id'])
        _require(TraceId(trace['task_id']) == expected_id)
        _require(trace.get('session_id') is None or isinstance(trace['session_id'], str))
        _require(trace['collection_state'] in {'pending', 'complete', 'incomplete'})
        _require(type(trace['io_complete']) is bool)
        _require(trace.get('task_status') is None or isinstance(trace['task_status'], str))
        _require(isinstance(trace['events'], list) and isinstance(trace['payloads'], dict))
        _require(isinstance(trace['summary'], dict) and isinstance(trace['checkpoint'], dict))
        checkpoint = trace['checkpoint']
        for key in ('applied_seq', 'published_seq'):
            _require(type(checkpoint[key]) is int and checkpoint[key] >= 0)
        _require(checkpoint['applied_seq'] <= checkpoint['published_seq'])
        final = checkpoint.get('final_seq')
        _require(final is None or (type(final) is int and 0 <= final <= checkpoint['published_seq']))
        _require(type(checkpoint.get('dropped_events', 0)) is int and checkpoint.get('dropped_events', 0) >= 0)
        _require(isinstance(checkpoint.get('diagnostics', []), list))
        _require(all(isinstance(code, str) for code in checkpoint.get('diagnostics', [])))
        ids = set()
        _require(isinstance(trace.get('orphan_nodes', []), list))
        pending = [(trace['root'], None)] if trace['root'] is not None else []
        orphan_parents = []
        for orphan in trace.get('orphan_nodes', []):
            _require(isinstance(orphan, dict))
            parent = orphan.get('parent_span_id')
            _require(isinstance(parent, str))
            orphan_parents.append(parent)
            pending.append((orphan, parent))
        while pending:
            node, parent = pending.pop()
            _require(isinstance(node, dict) and isinstance(node['children'], list))
            _require(isinstance(node['span_id'], str) and re.fullmatch('[0-9a-f]{16}', node['span_id']))
            _require(node['span_id'] not in ids and node['parent_span_id'] == parent)
            ids.add(node['span_id'])
            for key in ('name', 'kind', 'status'):
                _require(isinstance(node[key], str))
            for key in ('started_at', 'ended_at', 'duration_ms', 'first_output_at'):
                value = node.get(key)
                _require(value is None or (type(value) in (int, float) and isfinite(value)))
            _require(node.get('usage') is None or isinstance(node['usage'], dict))
            if node.get('usage'):
                for key in ('total_tokens', 'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens'):
                    value = node['usage'].get(key)
                    _require(value is None or (type(value) in (int, float) and isfinite(value) and value >= 0))
            pending.extend((child, node['span_id']) for child in node['children'])
        _require(not ids.intersection(orphan_parents))
        previous = 0
        for event in trace['events']:
            _require(isinstance(event, dict) and type(event['seq']) is int)
            _require(previous < event['seq'] <= checkpoint['applied_seq'])
            _require(isinstance(event['operation'], str) and isinstance(event['span_id'], str))
            _require(type(event['occurred_at']) in (int, float) and isfinite(event['occurred_at']))
            previous = event['seq']
        for payload in trace['payloads'].values():
            _require(isinstance(payload, dict) and 'data' in payload)
            _require(type(payload['truncated']) is bool)
    except (KeyError, TypeError, ValueError):
        raise TraceFormatError('invalid_trace') from None
    return trace


def Diagnostics(trace):
    codes = set(trace['checkpoint'].get('diagnostics', []))
    if trace.get('orphan_nodes'):
        codes.add('parent_missing')
    for item in list(TraceNodes(trace)) + trace['events']:
        for key in ('input_ref', 'output_ref'):
            if key in item and (not isinstance(item[key], str) or item[key] not in trace['payloads']):
                codes.add('payload_missing')
    checkpoint = trace['checkpoint']
    if checkpoint.get('dropped_events'):
        codes.add('events_dropped')
    if trace['collection_state'] == 'complete' and (
            not trace['root'] or any(node.get('ended_at') is None for node in TraceNodes(trace))
            or checkpoint.get('final_seq') is None or checkpoint['applied_seq'] != checkpoint['final_seq']
            or checkpoint['applied_seq'] != checkpoint['published_seq']
            or not any(event['operation'] == 'seal' and event['seq'] == checkpoint['final_seq'] for event in trace['events'])):
        codes.add('collection_inconsistent')
    return sorted(codes)
