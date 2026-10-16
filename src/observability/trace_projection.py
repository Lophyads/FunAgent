"""One set of derived totals for the writer and every review surface."""
from math import isfinite


def Nodes(root):
    pending = [root] if root else []
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node['children']))


def TraceNodes(trace):
    yield from Nodes(trace['root'])
    for root in trace.get('orphan_nodes', []):
        yield from Nodes(root)


def TurnOverview(trace):
    """Small opt-in UI projection; never includes model/tool/retrieval bodies."""
    nodes = {node['span_id']: node for node in TraceNodes(trace)}
    main = {}
    for node in nodes.values():
        if node['name'] == 'agent.run' and node.get('purpose', 'main') == 'main':
            main.update((child['span_id'], child) for child in Nodes(node))
    # Retain physical operations whose original parent was lost.
    for root in trace.get('orphan_nodes', []):
        main.update((child['span_id'], child) for child in Nodes(root)
                    if child.get('purpose', 'main') == 'main')
    loops = [n for n in main.values() if n['name'] == 'agent.iteration']
    tools = [n for n in main.values() if n['name'] == 'tool.call' or n['kind'] == 'tool']
    retrievals = [n for n in nodes.values() if n['name'] == 'memory.search']
    reranks = [n for n in nodes.values() if n['name'] == 'model.attempt' and n['kind'] == 'rerank']
    root = trace.get('root') or {}
    saved = trace.get('payloads', {}).get(root.get('input_ref'), {}).get('data')
    text = next((saved[key].strip() for key in ('text', 'content')
                 if isinstance(saved, dict) and isinstance(saved.get(key), str) and saved[key].strip()), None)
    duration = root.get('duration_ms')
    valid_duration = type(duration) in (int, float) and isfinite(duration) and duration >= 0
    success = lambda values: sum(n['status'] in {'ok', 'completed', 'succeeded'} for n in values)
    return dict(user_preview=text[:160] if text else None, user_preview_truncated=bool(text and len(text) > 160),
                duration_ms=duration if valid_duration else None, ended=root.get('ended_at') is not None,
                retrieval=dict(success=success(retrievals), total=len(retrievals)),
                rerank=dict(success=sum(n['status'] == 'accepted' for n in reranks), total=len(reranks)),
                loops=dict(completed=sum(n['status'] in {'completed', 'succeeded'} for n in loops), total=len(loops)),
                tools=dict(success=success(tools), total=len(tools)))


def SessionSummary(items):
    """Statistics cover the full filtered set, before pagination."""
    durations = [item['overview']['duration_ms'] for item in items
                 if item.get('overview', {}).get('ended') and item['overview']['duration_ms'] is not None]
    started = [item['started_at'] for item in items if item.get('started_at') is not None]
    return dict(turn_count=len(items), duration_sample_count=len(durations),
                average_duration_ms=sum(durations) / len(durations) if durations else None,
                latest_started_at=max(started) if started else None)


def Summary(root, orphan_nodes=()):
    known = main = unknown = 0
    waits = {}
    rerank = dict(count=0, duration_ms=0, unknown_duration_count=0)
    for node in TraceNodes(dict(root=root, orphan_nodes=orphan_nodes)):
        if node['name'] in {'task.queue_wait', 'model.retry_wait', 'interaction.wait'}:
            wait = waits.setdefault(node['name'], dict(count=0, duration_ms=0, unknown_duration_count=0))
            wait['count'] += 1
            duration = node.get('duration_ms')
            if type(duration) in (int, float) and isfinite(duration) and duration >= 0:
                wait['duration_ms'] += duration
            else:
                wait['unknown_duration_count'] += 1
        if node['name'] == 'model.attempt' and node['kind'] == 'rerank':
            rerank['count'] += 1
            duration = node.get('duration_ms')
            if type(duration) in (int, float) and isfinite(duration) and duration >= 0:
                rerank['duration_ms'] += duration
            else:
                rerank['unknown_duration_count'] += 1
        if node['kind'] not in {'generation', 'embedding', 'rerank'}:
            continue
        usage = node.get('usage') or {}
        if node.get('usage_status') == 'unknown' or not usage:
            unknown += 1
            continue
        total = usage.get('total_tokens')
        if total is None:
            inputs = [usage[key] for key in ('input_tokens', 'prompt_tokens') if key in usage]
            outputs = [usage[key] for key in ('output_tokens', 'completion_tokens') if key in usage]
            if not inputs and not outputs:
                unknown += 1
                continue
            total = (inputs[0] if inputs else 0) + (outputs[0] if outputs else 0)
        if type(total) not in (int, float) or not isfinite(total) or total < 0:
            unknown += 1
            continue
        known += total
        if node['kind'] == 'generation' and node.get('purpose', 'main') == 'main':
            main += total
    return dict(known_total_tokens=known, main_model_tokens=main, unknown_usage_calls=unknown,
                duration_ms=root.get('duration_ms') if root else None, waits=waits, rerank=rerank)
