"""SDK diagnostics never forward remote payloads or exception wire data."""
import logging
from threading import Lock

_lock, _users, _previous = Lock(), 0, {}


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
