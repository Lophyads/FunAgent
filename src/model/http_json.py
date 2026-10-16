"""One JSON HTTP attempt per Send, sharing a drained client."""
import json
import os
from contextlib import contextmanager
from threading import Condition

from ..frame.contracts import ControlError, DomainError
from .protocols import ModelError, ProviderResponse
from .retry import RetryAfter


class HTTPJSONProvider:
    def __init__(self, options, *, client=None, transport=None):
        # Import only when this driver is selected; rerank is an optional extra.
        try:
            import httpx
        except ImportError:
            raise DomainError('DRIVER_UNAVAILABLE', 'HTTP JSON driver is unavailable') from None
        self._httpx = httpx
        try:
            endpoint = httpx.URL(options['endpoint'])
            if endpoint.scheme not in {'http', 'https'} or not endpoint.host or endpoint.userinfo or endpoint.fragment:
                raise ValueError
        except (KeyError, TypeError, ValueError, httpx.InvalidURL):
            raise DomainError('CONFIG_INVALID', 'Invalid model endpoint') from None
        headers = {}
        if 'credential_env' in options:
            name = options['credential_env']
            token = os.environ.get(name) if isinstance(name, str) and name else None
            if not token:
                raise DomainError('CONFIG_CREDENTIAL', 'Model credential environment variable is missing')
            headers['Authorization'] = 'Bearer ' + token
        self._endpoint, self._headers = endpoint, headers
        # Ownership of an injected client transfers to this Provider.
        self._client = client if client is not None else httpx.Client(transport=transport, timeout=30)
        self._condition = Condition()
        self._closing = self._closed = self._close_in_progress = False
        self._active_borrowers = 0
        self._retry_transports = ()
        self._owned_transports = tuple({id(item): item for item in
            (self._client._transport, *self._client._mounts.values()) if item is not None}.values())

    @contextmanager
    def _borrow(self, context):
        context.CheckCancelled()
        with self._condition:
            context.CheckCancelled()
            if self._closing or self._closed:
                raise DomainError('NOT_READY', 'Provider closed')
            self._active_borrowers += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_borrowers -= 1
                self._condition.notify_all()

    def Send(self, request, context):
        with self._borrow(context):
            try:
                context.CheckCancelled()
                response = self._client.post(self._endpoint, json=request.payload, headers=self._headers,
                    timeout=min(30, context.Remaining()), follow_redirects=False)
                context.CheckCancelled()
                if not 200 <= response.status_code < 300:
                    status = response.status_code
                    code = ('AUTH' if status in {401, 403} else 'RATE_LIMIT' if status == 429
                            else 'TIMEOUT' if status == 408 else 'TRANSPORT' if status >= 500 or status == 409
                            else 'INVALID_REQUEST')
                    raise ModelError(code, retry_after=RetryAfter(response.headers.get('retry-after')))
                payload = response.json()
                context.CheckCancelled()
                return ProviderResponse(payload)
            except (ControlError, ModelError):
                raise
            except (json.JSONDecodeError, UnicodeError):
                context.CheckCancelled()
                raise ModelError('PROTOCOL') from None
            except self._httpx.TimeoutException:
                context.CheckCancelled()
                raise ModelError('TIMEOUT') from None
            except self._httpx.TransportError:
                context.CheckCancelled()
                raise ModelError('TRANSPORT') from None
            except Exception:
                context.CheckCancelled()
                raise ModelError('PROVIDER_FATAL') from None

    def Close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            while self._active_borrowers or self._close_in_progress:
                self._condition.wait()
            if self._closed:
                return
            self._close_in_progress = True
        try:
            if self._retry_transports:
                failed = []
                for transport in self._retry_transports:
                    try:
                        transport.close()
                    except Exception:
                        failed.append(transport)
                self._retry_transports = tuple(failed)
                if failed:
                    raise DomainError('CLOSE_FAILED', 'Provider close failed')
            else:
                try:
                    self._client.close()
                except Exception:
                    # HTTPX marks itself closed before closing its transports.
                    self._retry_transports = self._owned_transports
                    raise
        except Exception:
            raise DomainError('CLOSE_FAILED', 'Provider close failed') from None
        else:
            with self._condition:
                self._closed = True
        finally:
            with self._condition:
                self._close_in_progress = False
                self._condition.notify_all()
