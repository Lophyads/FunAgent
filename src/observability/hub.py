"""Bounded producer snapshots and queue; only the writer projects trees or does IO."""
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import logging
from math import isfinite
from threading import Condition, Thread, Event
import time

from ..frame.contracts import DomainError, freeze, thaw
from .contracts import DEFAULTS, TraceId, UsageSnapshot
from .redaction import Redactor, PayloadSnapshot
from .trace_projection import Summary


logger = logging.getLogger('funagent.observation')
_LIFECYCLE = {'start_task', 'start', 'complete', 'end_task', 'seal'}
_USAGE = {'total_tokens', 'input_tokens', 'output_tokens', 'prompt_tokens', 'completion_tokens',
          'cached_tokens', 'reasoning_tokens', 'audio_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'}
_USAGE_DETAILS = {'prompt_tokens_details', 'completion_tokens_details', 'input_tokens_details', 'output_tokens_details'}


def _usage(value, depth=0):
    if not isinstance(value, Mapping) or depth > 1:
        return {}
    result = {}
    for key in _USAGE:
        count = value.get(key)
        if type(count) in (int, float) and isfinite(count) and count >= 0:
            result[key] = count
    for key in _USAGE_DETAILS:
        if key in value:
            result[key] = _usage(value[key], depth + 1)
    return result


@dataclass
class _Sequence:
    seq: int = 0
    dropped: int = 0
    ranges: list = field(default_factory=list)
    diagnostics: set = field(default_factory=set)
    payload_bytes: int = 0
    final: int | None = None
    task_status: str | None = None


class ObservabilityHub:
    def __init__(self, store, *, redact_text=None, **options):
        settings = dict(DEFAULTS, **options)
        self.store = store
        self._capacity = settings['queue_capacity']
        self._max_bytes = settings['queue_max_bytes']
        self._payload_budget = settings['max_trace_payload_bytes']
        self._interval = settings['flush_interval_ms'] / 1000
        self._timeout = settings['shutdown_timeout']
        self._redactor = Redactor(settings['max_payload_bytes'], redact_text)
        self._metadata_redactor = Redactor(65536, redact_text)
        self._condition = Condition()
        self._queue, self._sequences = deque(), {}
        self._bytes, self._closing = 0, False
        self._recovery = None
        self._health = dict(queued=0, persisted=0, dropped=0, pending_bytes=0,
                            peak_queue=0, peak_bytes=0, last_error=None, diagnostics={},
                            recovery=None, last_flush_ms=None, max_persistence_lag_ms=0)
        # Projection maps belong to this thread only, never to producers.
        self._traces, self._dirty = {}, set()
        self._writer = Thread(target=self._work, name='funagent-observation-writer', daemon=True)
        self._writer.start()

    def Prepare(self, fields):
        result = {}
        for key, value in fields.items():
            if key not in {'input', 'output'}:
                continue
            try:
                result[key] = value if isinstance(value, PayloadSnapshot) else self._redactor.Snapshot(value() if callable(value) else value)
            except Exception:
                result[key] = PayloadSnapshot(None, None, 0, True, 'projection_failed')
                self.Diagnose('projection_failed')
        extras = {}
        for key, value in fields.items():
            if key in {'input', 'output'}:
                continue
            try:
                value = value() if callable(value) else value
                if isinstance(value, UsageSnapshot):
                    extras['usage_status'] = value.status
                    value = value.raw
                if key in {'usage', 'usage_raw'}:
                    value = _usage(value)
                extras[key] = value
            except Exception:
                extras['payload_missing'] = True
                self.Diagnose('projection_failed')
        safe = self._metadata_redactor.Snapshot(extras)
        result.update(thaw(safe.data) if isinstance(safe.data, Mapping) else {})
        if safe.truncated:
            result['payload_missing'] = True
        return freeze(result)

    def _gap(self, state, seq):
        state.dropped += 1
        if state.ranges and state.ranges[-1][1] == seq - 1:
            state.ranges[-1][1] = seq
        elif len(state.ranges) < 64:
            state.ranges.append([seq, seq])
        else:
            state.diagnostics.add('drop_ranges_truncated')
        self._health['dropped'] += 1

    def Publish(self, record):
        # Bounded fields are copied before taking the short queue lock.
        fields = dict(record.fields)
        payloads = {k: fields.pop(k) for k in ('input', 'output') if k in fields}
        wire_fields = thaw(fields)
        with self._condition:
            if self._closing:
                self._health['last_error'] = 'recorder_closed'
                return
            state = self._sequences.get(record.target.task_id)
            if state is None:
                if len(self._sequences) >= 1024:
                    self._health['last_error'] = 'trace_capacity'
                    return
                state = self._sequences[record.target.task_id] = _Sequence()
            state.seq += 1
            seq = state.seq
            bounded = {}
            for key, snapshot in payloads.items():
                if state.payload_bytes + snapshot.captured_bytes > self._payload_budget:
                    snapshot = snapshot.Omit('trace_payload_budget')
                state.payload_bytes += snapshot.captured_bytes
                bounded[key] = snapshot.Wire()
            event = dict(task_id=record.target.task_id, trace_id=record.target.trace_id,
                         span_id=record.target.span_id, operation=record.operation,
                         occurred_at=record.occurred_at, seq=seq, fields=wire_fields, payloads=bounded)
            size = len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode('utf8'))
            lifecycle = record.operation in _LIFECYCLE
            # Reserve one quarter of queue capacity for lifecycle and terminal facts.
            limit = self._capacity if lifecycle else self._capacity - self._capacity // 4
            byte_limit = self._max_bytes if lifecycle else self._max_bytes - self._max_bytes // 4
            if self._bytes + size > byte_limit and bounded:
                event['payloads'] = {k: PayloadSnapshot(None, p['original_bytes'], 0, True, 'queue_payload_budget').Wire()
                                     for k, p in bounded.items()}
                size = len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode('utf8'))
            if len(self._queue) >= limit or self._bytes + size > byte_limit or (seq > 16384 and not lifecycle):
                self._gap(state, seq)
            else:
                self._queue.append((event, size))
                self._bytes += size
                self._health['queued'] += 1
                self._health['peak_queue'] = max(self._health['peak_queue'], len(self._queue))
                self._health['peak_bytes'] = max(self._health['peak_bytes'], self._bytes)
            if record.operation == 'seal':
                # Even a dropped seal informs the writer of the final gap.
                state.final = seq
            elif record.operation == 'end_task':
                state.task_status = wire_fields.get('task_status')
            self._condition.notify_all()

    def Diagnose(self, code, task_id=None):
        with self._condition:
            diagnostics = self._health['diagnostics']
            diagnostics[code] = diagnostics.get(code, 0) + 1
            if task_id in self._sequences:
                self._sequences[task_id].diagnostics.add(code)
            self._condition.notify_all()

    def Health(self):
        with self._condition:
            return dict(self._health, diagnostics=dict(self._health['diagnostics']),
                        pending_bytes=self._bytes, queue_size=len(self._queue),
                        writer_alive=self._writer.is_alive(), active_traces=len(self._sequences),
                        last_serialization_ms=getattr(self.store, 'last_serialization_ms', None))

    def Recover(self, task_lookup=None):
        done = Event()
        with self._condition:
            if self._closing or self._sequences or self._recovery is not None:
                raise DomainError('CONFLICT', 'Recovery requires an idle startup writer')
            self._recovery = (task_lookup, done)
            self._condition.notify_all()
        if not done.wait(self._timeout):
            self.Diagnose('recovery_timeout')

    def _trace(self, task_id, trace_id):
        return self._traces.setdefault(task_id, dict(schema_version=1, trace_id=trace_id, task_id=task_id,
            session_id=None, task_status=None, collection_state='pending', io_complete=True,
            root=None, events=[], payloads={}, summary={}, checkpoint={}, _nodes={}, _applied=0, _sealed=False))

    def _apply(self, event):
        task_id, span_id, op = event['task_id'], event['span_id'], event['operation']
        trace = self._trace(task_id, event['trace_id'])
        fields = event['fields']
        for key, payload in event['payloads'].items():
            payload_id = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf8')).hexdigest()[:24]
            trace['payloads'][payload_id] = payload
            fields[key + '_ref'] = payload_id
            trace['io_complete'] &= not payload['truncated']
        if fields.get('payload_missing'):
            trace['io_complete'] = False
        if op in {'start_task', 'start'}:
            node = dict(span_id=span_id, parent_span_id=fields.get('parent_span_id'),
                        name=fields.get('name'), kind=fields.get('kind', 'span'),
                        status='running', started_at=event['occurred_at'], ended_at=None,
                        duration_ms=None, children=[])
            node.update(fields)
            trace['_nodes'][span_id] = node
            if op == 'start_task':
                trace['root'] = node
                trace['session_id'] = fields.get('session_id')
                trace['task_status'] = 'queued'
            else:
                parent = trace['_nodes'].get(node['parent_span_id'])
                if parent is not None:
                    parent['children'].append(node)
                else:
                    self.Diagnose('parent_missing', task_id)
        elif op in {'update', 'complete'}:
            node = trace['_nodes'].get(span_id)
            if node:
                updates = dict(fields)
                if 'usage' in updates and not updates['usage'] and node.get('usage'):
                    updates.pop('usage')
                    updates.pop('usage_status', None)
                node.update(updates)
                if 'usage' in updates:
                    node['usage_status'] = updates.get('usage_status', 'known' if updates['usage'] else 'unknown')
            else:
                self.Diagnose('node_missing', task_id)
        elif op == 'end_task':
            trace['task_status'] = fields['task_status']
        elif op == 'seal':
            trace['_sealed'] = True
        if len(trace['events']) < 16384:
            trace['events'].append(dict(event_id=f"{event['trace_id']}:{event['seq']}",
                span_id=span_id, seq=event['seq'], operation=op,
                occurred_at=event['occurred_at'], **fields))
        else:
            self.Diagnose('event_history_limit', task_id)
        trace['_applied'] = event['seq']
        self._dirty.add(task_id)

    def _snapshot(self, task_id):
        trace = self._traces[task_id]
        with self._condition:
            state = self._sequences[task_id]
            final, seq, dropped = state.final, state.seq, state.dropped
            ranges = [list(pair) for pair in state.ranges]
            diagnostics = sorted(state.diagnostics)
        terminal = trace['task_status'] not in {None, 'queued', 'running'}
        ended = trace['root'] is not None and trace['root'].get('ended_at') is not None
        complete = terminal and ended and trace['_sealed'] and trace['_applied'] == final
        trace['collection_state'] = ('incomplete' if dropped or diagnostics or (self._closing and not complete)
                                     else 'complete' if complete else 'pending')
        trace['checkpoint'] = dict(applied_seq=trace['_applied'], published_seq=seq, final_seq=final,
            dropped_events=dropped, dropped_ranges=ranges, diagnostics=diagnostics, saved_at=time.time())
        trace['orphan_nodes'] = [node for node in trace['_nodes'].values()
                                if node is not trace['root'] and node.get('parent_span_id') not in trace['_nodes']]
        trace['summary'] = Summary(trace['root'], trace['orphan_nodes'])
        return {key: value for key, value in trace.items() if not key.startswith('_')}

    def _flush(self):
        for task_id in tuple(self._dirty):
            try:
                snapshot = self._snapshot(task_id)
                started = time.monotonic()
                self.store.Save(snapshot)
            except Exception:
                with self._condition:
                    if self._health['last_error'] != 'json_save_failed':
                        logger.warning('Observation JSON save failed')
                    self._health['last_error'] = 'json_save_failed'
                continue
            self._dirty.discard(task_id)
            with self._condition:
                self._health['persisted'] += 1
                self._health['last_flush_ms'] = (time.monotonic() - started) * 1000
                if snapshot['events']:
                    lag = max(0, time.time() - snapshot['events'][-1]['occurred_at']) * 1000
                    self._health['max_persistence_lag_ms'] = max(self._health['max_persistence_lag_ms'], lag)
                state = self._sequences[task_id]
                # No producers remain for a sealed trace. Its complete history
                # lives in the JSON file rather than an unbounded memory cache.
                current = snapshot['checkpoint']['published_seq'] == state.seq and snapshot['checkpoint']['dropped_events'] == state.dropped
                if (state.final is not None or self._closing) and current and not any(e['task_id'] == task_id for e, _ in self._queue):
                    self._sequences.pop(task_id, None)
                    self._traces.pop(task_id, None)
                elif not current:
                    self._dirty.add(task_id)

    def _work(self):
        next_flush = time.monotonic() + self._interval
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closing or self._recovery,
                                         timeout=max(0, next_flush - time.monotonic()))
                recovery, self._recovery = self._recovery, None
                batch = list(self._queue)
                self._queue.clear()
                self._bytes = 0
            if recovery is not None:
                task_lookup, done = recovery
                try:
                    result = self.store.Recover(task_lookup)
                    with self._condition:
                        self._health['recovery'] = result
                    if result['diagnostics']:
                        self.Diagnose('recovery_diagnostics')
                except Exception:
                    self.Diagnose('recovery_failed')
                finally:
                    done.set()
            for event, _ in batch:
                try:
                    self._apply(event)
                except Exception:
                    self.Diagnose('writer_projection_failed', event['task_id'])
                    self._dirty.add(event['task_id'])
            # Sequence gaps / dropped seals dirty a trace even without a new event.
            with self._condition:
                for task_id, state in self._sequences.items():
                    if task_id not in self._traces and (state.final is not None or self._closing):
                        # No node or event is invented when even the root was lost.
                        trace = self._trace(task_id, TraceId(task_id))
                        trace['task_status'] = state.task_status
                        trace['io_complete'] = False
                        self._dirty.add(task_id)
                for task_id in self._traces:
                    checkpoint = self._traces[task_id]['checkpoint']
                    state = self._sequences[task_id]
                    if (state.final is not None or state.dropped != checkpoint.get('dropped_events', 0)
                            or state.diagnostics != set(checkpoint.get('diagnostics', ()))):
                        self._dirty.add(task_id)
            if self._closing or time.monotonic() >= next_flush or any(e['operation'] == 'seal' for e, _ in batch):
                self._flush()
                next_flush = time.monotonic() + self._interval
            with self._condition:
                if self._closing and not self._queue and not self._dirty:
                    return
                if self._closing and self._dirty:
                    # A failed save remains retryable; never spin or discard it.
                    self._condition.wait(timeout=self._interval)

    def Close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._writer.join(self._timeout)
        if self._writer.is_alive():
            raise DomainError('CLOSE_FAILED', 'Observation writer drain timed out')
