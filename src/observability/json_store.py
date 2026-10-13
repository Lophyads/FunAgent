"""Ordinary JSON snapshot persistence, owned exclusively by the hub writer."""
from pathlib import Path
import json
import os
import re
import time

from .trace_projection import TraceNodes, Summary
from .trace_schema import Decode, Diagnostics, TraceFormatError


class JsonStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.last_serialization_ms = None

    def Save(self, trace):
        trace_id = trace['trace_id']
        if not re.fullmatch('[0-9a-f]{32}', trace_id):
            raise ValueError('Invalid trace ID')
        self.directory.mkdir(parents=True, exist_ok=True)
        final = self.directory / (trace_id + '.json')
        temporary = self.directory / (trace_id + '.json.tmp')
        with temporary.open('w', encoding='utf8', newline='\n') as output:
            serialization_started = time.perf_counter()
            json.dump(trace, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write('\n')
            self.last_serialization_ms = (time.perf_counter() - serialization_started) * 1000
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final)

    def Recover(self, task_lookup=None):
        """Run on startup in the writer, before any new task may publish records."""
        health = dict(recovered=0, promoted=0, diagnostics=[])
        def diagnose(trace_id, code):
            health['diagnostics'].append(dict(trace_id=trace_id, code=code))
        def load(path, trace_id):
            try:
                if path.is_symlink():
                    raise TraceFormatError('invalid_trace_path')
                return Decode(path.read_text(encoding='utf8'), trace_id), None
            except FileNotFoundError:
                return None, None
            except (OSError, UnicodeError, TraceFormatError) as error:
                code = error.code if isinstance(error, TraceFormatError) else 'trace_read_failed'
                diagnose(trace_id, code)
                return None, code
        def sequence(trace):
            checkpoint = trace['checkpoint']
            return checkpoint['applied_seq'], checkpoint['published_seq'], checkpoint.get('final_seq') or 0
        try:
            identifiers = {path.name[:32] for path in self.directory.glob('*.json*')
                           if re.fullmatch('[0-9a-f]{32}\\.json(?:\\.tmp)?', path.name)}
        except OSError:
            diagnose(None, 'trace_directory_unreadable')
            return health
        for trace_id in sorted(identifiers):
            final = self.directory / (trace_id + '.json')
            temporary = self.directory / (trace_id + '.json.tmp')
            trace, final_error = load(final, trace_id)
            candidate, _ = load(temporary, trace_id)
            # A future schema is authoritative even if this version cannot read it.
            if final_error in {'unsupported_schema', 'invalid_trace_path', 'trace_read_failed'}:
                continue
            if candidate is not None and 'payload_missing' in Diagnostics(candidate):
                diagnose(trace_id, 'temporary_payload_missing')
                candidate = None
            if candidate is not None:
                try:
                    if trace is None or sequence(candidate) > sequence(trace):
                        os.replace(temporary, final)
                        trace = candidate
                        health['promoted'] += 1
                    else:
                        temporary.unlink()  # Only discard a verified obsolete temporary snapshot.
                except OSError:
                    diagnose(trace_id, 'temporary_recovery_failed')
                    continue
            if trace is None:
                continue
            changed = False
            recorded_status = trace.get('task_status')
            if task_lookup is not None:
                try:
                    status = task_lookup(trace['task_id']).status
                    if status != recorded_status:
                        trace['task_status'] = status
                        changed = True
                except Exception:
                    diagnose(trace_id, 'task_lookup_failed')
            unclosed = []
            for node in TraceNodes(trace):
                if node.get('ended_at') is None and node['status'] != 'interrupted':
                    node['status'] = 'interrupted'
                    unclosed.append(node['span_id'])
            codes = set(Diagnostics(trace))
            if unclosed or trace['collection_state'] == 'pending':
                codes.add('interrupted')
            if unclosed or codes or trace['collection_state'] == 'pending':
                changed |= trace['collection_state'] != 'incomplete' or set(trace['checkpoint'].get('diagnostics', [])) != codes or bool(unclosed)
                trace['collection_state'] = 'incomplete'
                trace['checkpoint']['diagnostics'] = sorted(codes)
            if 'payload_missing' in codes:
                changed |= trace['io_complete']
                trace['io_complete'] = False
            if changed:
                trace['checkpoint']['recovery'] = dict(recovered_at=time.time(), recorded_task_status=recorded_status,
                                                       unclosed_spans=unclosed)
                trace['summary'] = Summary(trace['root'], trace.get('orphan_nodes', []))
                try:
                    self.Save(trace)
                    health['recovered'] += 1
                except Exception:
                    diagnose(trace_id, 'recovery_save_failed')
        return health
