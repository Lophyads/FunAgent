"""SDK diagnostics never forward remote payloads or exception wire data."""
import logging
import re
from threading import Lock

_lock, _users, _previous = Lock(), 0, {}


def ErrorFields(error):
    """Only bounded identifiers/statuses, never exception text, bodies or URLs."""
    import httpx

    fields = {}
    pending, seen = [error], set()
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            fields['http_status'] = current.response.status_code
        if isinstance(current, httpx.HTTPError):
            fields.setdefault('exception_type', type(current).__name__)
        # MCP JSON-RPC errors use numeric codes, unlike local DomainError codes.
        code = getattr(getattr(current, 'error', None), 'code', None)
        if type(code) is int:
            fields.setdefault('remote_code', code)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
        pending.extend(list(getattr(current, 'exceptions', ()))[:16])
    name = type(error).__name__
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', name):
        fields.setdefault('exception_type', name)
    return fields


class _SafeDiagnostic(logging.Handler):
    def emit(self, record):
        logging.getLogger("funagent.tool.protocol").log(record.levelno, "Tool protocol diagnostic")


class SDKDiagnostics:
    def __init__(self):
        global _users
        self._closed = False
        with _lock:
            if not _users:
                for name in ("mcp", "httpx", "httpcore"):
                    logger = logging.getLogger(name)
                    _previous[name] = (tuple(logger.handlers), logger.propagate)
                    logger.handlers, logger.propagate = [_SafeDiagnostic()], False
            _users += 1

    def Close(self):
        global _users
        with _lock:
            if self._closed:
                return
            self._closed = True
            _users -= 1
            if not _users:
                for name, (handlers, propagate) in _previous.items():
                    logger = logging.getLogger(name)
                    logger.handlers, logger.propagate = list(handlers), propagate
                _previous.clear()
