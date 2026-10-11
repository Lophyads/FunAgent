"""A selected backend retains its resources until actual borrowers return."""
from contextlib import contextmanager
from threading import Condition
import time
from ...frame.contracts import DomainError


class ManagedBackend:
    def __init__(self, settings):
        self._condition = Condition()
        self._active = 0
        self._closing = self._closed = False
        self._drain_timeout = settings.get("drain_timeout", 5)

    @contextmanager
    def Borrow(self, context):
        context.CheckCancelled()
        with self._condition:
            context.CheckCancelled()
            if self._closing:
                raise DomainError("NOT_READY", "Tool connection is closed")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def _close(self):
        pass

    def Close(self):
        with self._condition:
            if self._closed:
                return
            self._closing = True
            deadline = time.monotonic() + self._drain_timeout
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DomainError("CLOSE_FAILED", "Tool borrowers have not drained")
                self._condition.wait(min(remaining, 0.02))
            self._close()
            self._closed = True
