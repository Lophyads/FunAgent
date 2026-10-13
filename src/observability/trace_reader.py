"""Read saved versions; database status is a separate, explicit projection."""
from math import isfinite
from pathlib import Path
import re
from ..frame.contracts import DomainError
from .contracts import TraceId
from .trace_projection import Summary, TurnOverview, SessionSummary
from .trace_schema import Decode, Diagnostics, TraceFormatError


def Identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise DomainError('INVALID_REQUEST', 'Invalid trace query identifier')
    return value


class TraceReader:
    def __init__(self, directory, task_lookup=None):
        self.directory = Path(directory).resolve()
        self.task_lookup = task_lookup

    def _read_path(self, path, task_id=None):
        review = dict(task_id=task_id, trace_id=path.stem, path=str(path), collected=False,
                      task_status=None, recorded_task_status=None, session_id=None,
                      collection_state='not_collected', io_complete=False,
                      diagnostics=[], trace=None, summary=None, saved_seq=None)
        try:
            if path.is_symlink():
                raise TraceFormatError('invalid_trace_path')
            raw = path.read_text(encoding='utf8')
            review['collected'] = True
            trace = Decode(raw, path.stem)
            review.update(task_id=trace['task_id'], session_id=trace.get('session_id'), trace=trace,
                          task_status=trace.get('task_status'), recorded_task_status=trace.get('task_status'),
                          collection_state=trace['collection_state'], io_complete=trace['io_complete'],
                          diagnostics=Diagnostics(trace), summary=Summary(trace['root'], trace.get('orphan_nodes', [])),
                          saved_seq=trace['checkpoint']['applied_seq'], _saved_json=raw)
            if review['diagnostics']:
                review['collection_state'] = 'incomplete'
            if 'payload_missing' in review['diagnostics']:
                review['io_complete'] = False
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, TraceFormatError) as error:
            review['collection_state'] = 'incomplete'
            review['diagnostics'].append(error.code if isinstance(error, TraceFormatError) else 'trace_read_failed')
        if self.task_lookup is not None and review['task_id'] is not None:
            try:
                task = self.task_lookup(review['task_id'])
                review['task_status'] = task.status
                if review['recorded_task_status'] is not None and task.status != review['recorded_task_status']:
                    review['diagnostics'].append('task_status_reconciled')
            except Exception as error:
                review['diagnostics'].append('task_not_found' if isinstance(error, DomainError) and error.code == 'NOT_FOUND'
                                             else 'task_lookup_failed')
        return review

    def Read(self, task_id):
        return self._read_path(self.directory / (TraceId(Identifier(task_id)) + '.json'), task_id)

    def _list(self, *, session_id=None, since=None, until=None, limit=50, offset=0, view=None, unassigned=False):
        if session_id is not None:
            Identifier(session_id)
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
            raise DomainError('INVALID_REQUEST', 'Invalid trace page bounds')
        if view not in {None, 'overview'} or type(unassigned) is not bool or (unassigned and session_id is not None):
            raise DomainError('INVALID_REQUEST', 'Invalid trace view')
        for value in (since, until):
            if value is not None and (type(value) not in (int, float) or not isfinite(value)):
                raise DomainError('INVALID_REQUEST', 'Invalid trace time bounds')
        if since is not None and until is not None and since > until:
            raise DomainError('INVALID_REQUEST', 'Invalid trace time range')
        items, diagnostics = [], []
        try:
            paths = sorted(self.directory.glob('*.json'))
        except OSError:
            return dict(items=[], total=0, next_offset=None, diagnostics=['trace_directory_unreadable'])
        for path in paths:
            if not re.fullmatch('[0-9a-f]{32}', path.stem):
                continue
            review = self._read_path(path)
            if review['diagnostics']:
                diagnostics.append(dict(trace_id=review['trace_id'], codes=review['diagnostics']))
            trace = review['trace']
            started = (trace['root'] or {}).get('started_at') if trace else None
            if session_id is not None and review['session_id'] != session_id:
                continue
            if unassigned and review['session_id'] is not None:
                continue
            if since is not None and (started is None or started < since):
                continue
            if until is not None and (started is None or started > until):
                continue
            if view == 'overview' and trace is None:
                continue
            item = {key: value for key, value in review.items() if key not in {'trace', '_saved_json'}} | {'started_at': started}
            if view == 'overview':
                item['overview'] = TurnOverview(trace)
            items.append(item)
        items.sort(key=lambda item: (item['started_at'] or 0, item['trace_id']), reverse=True)
        return dict(items=items, diagnostics=diagnostics)

    def List(self, *, session_id=None, since=None, until=None, limit=50, offset=0, view=None, unassigned=False):
        listing = self._list(session_id=session_id, since=since, until=until, limit=limit, offset=offset,
                             view=view, unassigned=unassigned)
        items = listing['items']
        next_offset = offset + limit if offset + limit < len(items) else None
        result = dict(items=items[offset:offset + limit], total=len(items), next_offset=next_offset,
                      diagnostics=listing['diagnostics'])
        if view == 'overview':
            result['session_summary'] = SessionSummary(items)
            for index, item in enumerate(result['items'], offset):
                item['turn_number'] = len(items) - index
        return result

    def Sessions(self, *, limit=50, offset=0):
        listing = self._list(limit=limit, offset=offset, view='overview')
        groups = {}
        for item in listing['items']:
            groups.setdefault(item['session_id'], []).append(item)
        sessions = [dict(session_id=sid, **SessionSummary(items)) for sid, items in groups.items()]
        sessions.sort(key=lambda item: (item['latest_started_at'] or 0, item['session_id'] or ''), reverse=True)
        return dict(items=sessions[offset:offset + limit], total=len(sessions),
                    next_offset=offset + limit if offset + limit < len(sessions) else None,
                    diagnostics=listing['diagnostics'])

    @staticmethod
    def Markdown(review):
        from .trace_export import Markdown
        return Markdown(review)

    @staticmethod
    def JSON(review):
        return review.get('_saved_json')

    @staticmethod
    def View(review):
        return {key: value for key, value in review.items() if not key.startswith('_')}
