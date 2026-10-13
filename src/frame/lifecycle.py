"""Startup registers ownership immediately; shutdown drains before releasing IO."""
from dataclasses import dataclass
from collections.abc import Callable

from .contracts import DomainError


@dataclass
class _Owned:
    resource: object
    close: Callable
    worker: bool = False


class Lifecycle:
    def __init__(self):
        self._owned = {}
        self._http = None

    def Own(self, resource, close=None, *, worker=False):
        key = id(resource)
        if key not in self._owned:
            self._owned[key] = _Owned(resource, close or (lambda: resource.Close()), worker)
        return resource

    def TrackBinding(self, manager, resource, *, worker=False):
        def close():
            try:
                bound = manager.GetInstance()
            except DomainError as error:
                if error.code != "NOT_READY":
                    raise
                bound = None
            if bound is resource:
                manager.Close()
            else:
                closer = getattr(resource, "Stop" if worker else "CloseResource", None)
                if closer is None and not worker:
                    closer = getattr(resource, "Close", None)
                if closer is not None:
                    closer()
        self._owned[id(resource)] = _Owned(resource, close, worker)
        return resource

    def Bind(self, manager, resource, *, worker=False):
        self.TrackBinding(manager, resource, worker=worker)
        manager.Initialize(resource)
        return resource

    def Forget(self, resource):
        self._owned.pop(id(resource), None)

    def HTTP(self, resource):
        self.Own(resource)
        self._http = resource
        return resource

    def Stop(self):
        workers = [entry for entry in self._owned.values() if entry.worker]
        error = None
        # Signal all workers before any join, including unpublished workers.
        for entry in workers:
            try:
                entry.resource.RequestStop()
            except Exception:
                error = DomainError("CLOSE_FAILED", "Worker cancellation failed")
        for entry in self._owned.values():
            if not entry.worker and callable(getattr(entry.resource, 'RequestStop', None)):
                try:
                    entry.resource.RequestStop()
                except Exception:
                    error = DomainError('CLOSE_FAILED', 'Subscription shutdown failed')
        if self._http is not None:
            try:
                self._owned[id(self._http)].close()
            except Exception:
                raise DomainError("CLOSE_FAILED", "HTTP server close failed") from None
            self.Forget(self._http)
            self._http = None
        for entry in workers:
            try:
                entry.close()
            except Exception as failure:
                error = failure if isinstance(failure, DomainError) else DomainError("CLOSE_FAILED", "Worker drain failed")
            if entry.resource.IsStopped():
                self.Forget(entry.resource)
        if any(entry.worker for entry in self._owned.values()):
            raise DomainError("CLOSE_FAILED", "Application worker is still running") from None
        for key, entry in reversed(tuple(self._owned.items())):
            try:
                entry.close()
            except Exception as failure:
                error = DomainError("CLOSE_FAILED", "Application resource close failed")
                # An explicit drain failure means the resource still owns IO.
                # Keep it reachable so a later Stop can finish its cleanup.
                if isinstance(failure, DomainError) and failure.code == "CLOSE_FAILED":
                    continue
                self._owned.pop(key, None)
            else:
                self._owned.pop(key, None)
        if error is not None:
            raise error

    def Empty(self):
        return not self._owned
