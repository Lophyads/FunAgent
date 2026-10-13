"""Read-only trace routes; neither submission nor model/tool ports are involved."""
from dataclasses import dataclass
import re

from ..frame.contracts import DomainError
from .diagnostics_mgr import DiagnosticsMgr


@dataclass(frozen=True)
class TraceDownload:
    data: bytes
    content_type: str


def Reader():
    return DiagnosticsMgr.GetInstance().Traces()


def Handle(method, route, query):
    if method != 'GET':
        raise DomainError('METHOD_NOT_ALLOWED', 'Trace review is read only')
    if route in {'/traces', '/trace-sessions'}:
        allowed = {'limit', 'offset'} if route == '/trace-sessions' else {'session_id', 'since', 'until', 'limit', 'offset', 'view', 'unassigned'}
        if query.keys() - allowed or any(len(values) != 1 for values in query.values()):
            raise DomainError('INVALID_REQUEST', 'Invalid trace query')
        values = {key: items[0] for key, items in query.items()}
        if 'unassigned' in values and values['unassigned'] != '1':
            raise DomainError('INVALID_REQUEST', 'Invalid unassigned filter')
        if 'view' in values and values['view'] != 'overview':
            raise DomainError('INVALID_REQUEST', 'Invalid trace view')
        try:
            options = {key: (int(value) if key in {'limit', 'offset'} else float(value) if key in {'since', 'until'} else value)
                       for key, value in values.items()}
        except ValueError:
            raise DomainError('INVALID_REQUEST', 'Invalid trace query') from None
        if 'unassigned' in options:
            options['unassigned'] = True
        return 200, Reader().Sessions(**options) if route == '/trace-sessions' else Reader().List(**options)
    match = re.fullmatch(r'/tasks/([A-Za-z0-9_-]{1,128})/trace(?:/(json|markdown))?', route)
    if match is None:
        raise DomainError('NOT_FOUND', 'Trace route not found')
    if query:
        raise DomainError('INVALID_REQUEST', 'Unexpected trace query')
    reader = Reader()
    review = reader.Read(match[1])
    if match[2] == 'json':
        data = reader.JSON(review)
        if data is None:
            raise DomainError('NOT_FOUND', 'No readable saved trace')
        return 200, TraceDownload(data.encode('utf8'), 'application/json; charset=utf-8')
    if match[2] == 'markdown':
        return 200, TraceDownload(reader.Markdown(review).encode('utf8'), 'text/markdown; charset=utf-8')
    return 200, reader.View(review)
