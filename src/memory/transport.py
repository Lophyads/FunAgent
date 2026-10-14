"""Bounded JSON IO; close waits for actual calls and rejects new borrowers."""
from contextlib import contextmanager
from math import isfinite
from threading import Condition, Lock
from urllib.parse import urlsplit
import json

from ..frame.contracts import DomainError


def ValidateConnection(endpoint, timeout):
    try:
        parsed = urlsplit(endpoint)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0 and
                 not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid or type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0:
        raise DomainError("CONFIG_INVALID", "Invalid memory connection configuration")


class JSONHTTP:
    def __init__(self, endpoint, headers, timeout=30, *, transport=None, protocol="MEMORY_PROTOCOL", source="MEMORY_SOURCE"):
        ValidateConnection(endpoint, timeout)
        try:
            import httpx
        except ImportError:
            raise DomainError("DRIVER_UNAVAILABLE", "Selected memory HTTP dependency is unavailable") from None
        self._endpoint, self._timeout = endpoint, timeout
        self._protocol, self._source = protocol, source
        self._httpx = httpx
        self._condition = Condition()
        self._active, self._closing, self._closed = 0, False, False
        self._close_attempted = False
        self._pending = []
        self._client = httpx.Client(headers=headers, transport=transport, trust_env=False)
        self._transports = tuple({id(t): t for t in (self._client._transport, *self._client._mounts.values()) if t is not None}.values())

    @contextmanager
    def Borrow(self, context):
        context.CheckCancelled()
        with self._condition:
            context.CheckCancelled()
            if self._closing:
                raise DomainError("NOT_READY", "Memory connection is closed")
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def Post(self, path, payload, context):
        return self.Request("POST", path, context, json=payload)

    def Request(self, method, path, context, *, response_mode="json", status_codes=None, **kwargs):
        """Bounded JSON responses, also for eval uploads and status queries."""
        with self.Borrow(context):
            try:
                context.CheckCancelled()
                with self._response(method, path, context, **kwargs) as response:
                    parts, size = [], 0
                    for part in response.iter_bytes():
                        context.CheckCancelled()
                        size += len(part)
                        if size > 2 * 1024 * 1024:
                            raise DomainError(self._protocol, "Memory response is too large")
                        parts.append(part)
                    context.CheckCancelled()
                    body = b"".join(parts)
                    if response.status_code >= 400:
                        code = "CONFIG_CREDENTIAL" if response.status_code in {401, 403} else self._source
                        if self._source == "MEMORY_SOURCE":
                            if response.status_code == 403:
                                code = "DENIED"
                            try:
                                code = {"FA409": "CONFLICT", "FA404": "NOT_FOUND", "FA400": "INVALID_REQUEST",
                                        "42501": "DENIED"}.get(json.loads(body).get("code"), code)
                            except (ValueError, AttributeError):
                                pass
                        raise DomainError((status_codes or {}).get(response.status_code, code), "Memory request failed")
                    if response_mode == "bytes":
                        return body
                    return json.loads(body)
            except DomainError:
                raise
            except (ValueError, UnicodeError):
                raise DomainError(self._protocol, "Invalid memory response") from None
            except Exception:
                context.CheckCancelled()
                raise DomainError(self._source, "Memory connection failed") from None

    @contextmanager
    def _response(self, method, path, context, **kwargs):
        lease = None
        try:
            with self._client.stream(method, self._endpoint + path, **kwargs,
                                     timeout=min(self._timeout, context.Remaining())) as response:
                original = response.stream
                class Lease(self._httpx.SyncByteStream):
                    def __init__(lease):
                        lease.closed, lease.lock = False, Lock()
                    def __iter__(lease):
                        yield from original
                    def close(lease):
                        with lease.lock:
                            if not lease.closed:
                                original.close()
                                lease.closed = True
                lease = Lease()
                response.stream = lease
                yield response
        finally:
            if lease is not None and not lease.closed:
                try:
                    lease.close()
                except Exception:
                    with self._condition:
                        self._pending.append(lease)

    def Close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            while self._active:
                self._condition.wait(0.02)
            if self._closed:
                return
            pending = []
            for lease in self._pending:
                try:
                    lease.close()
                except Exception:
                    pending.append(lease)
            self._pending = pending
            if pending:
                raise DomainError(self._source, "Memory response could not close")
            if not self._close_attempted:
                self._close_attempted = True
                try:
                    self._client.close()
                except Exception:
                    raise DomainError("MEMORY_SOURCE", "Memory connection could not close") from None
                self._transports = ()
                self._closed = True
                return
            # Retain failing transports: HTTPX marks itself closed before IO cleanup.
            remaining = []
            for transport in self._transports:
                try:
                    transport.close()
                except Exception:
                    remaining.append(transport)
            self._transports = tuple(remaining)
            if remaining:
                raise DomainError("MEMORY_SOURCE", "Memory connection could not close")
            self._closed = True
