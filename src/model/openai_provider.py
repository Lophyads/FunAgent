"""Shared OpenAI SDK transport; protocol conversion belongs to adapters."""
import json
import os
import time
from collections.abc import Mapping
from contextlib import contextmanager
from threading import Condition, Event, Thread, Lock
from ..frame.contracts import ControlError, DomainError
from .protocols import ModelError, ProviderResponse, RemoteError, SafeModelIdentifier
from .retry import RetryAfter
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Usage, StreamHasOutput


class OpenAIProvider:
    def __init__(self, options, *, transport=None, get_access=None):
        try:
            import openai
            import httpx
        except ImportError:
            raise DomainError("DRIVER_UNAVAILABLE", "OpenAI SDK is unavailable") from None
        credential_env = options.get("credential_env", "OPENAI_API_KEY")
        self._get_access = get_access
        token = "siwc-request-credential" if get_access is not None else os.environ.get(credential_env)
        if not token:
            raise DomainError("CONFIG_CREDENTIAL", "Model credential environment variable is missing")
        self._openai = openai
        self._httpx = httpx
        self._closed = False
        self._closing = False
        self._active_borrowers = 0
        self._close_in_progress = False
        self._condition = Condition()
        self._connect_timeout = options.get("connect_timeout", 15.0 if get_access is not None else 30.0)
        self._read_timeout = options.get("read_timeout", 120.0 if get_access is not None else 30.0)
        client = httpx.Client(transport=transport, timeout=self._read_timeout)
        # HTTPX marks a client closed before closing transports. Retain their
        # ownership so a failed transport close can be retried without reopening
        # admission or incorrectly treating HTTPX's closed flag as success.
        self._owned_transports = tuple({id(item): item for item in
            (client._transport, *client._mounts.values()) if item is not None}.values())
        self._retry_transports = ()
        self._pending_streams = []
        try:
            self._client = openai.OpenAI(api_key=token, base_url=options.get("base_url"),
                                          http_client=client, timeout=30, max_retries=0)
        except Exception:
            client.close()
            raise DomainError("CONFIG_INVALID", "Invalid model connection configuration") from None

    def Send(self, request, context):
        with self._borrow(context):
            return self._send(request, context)

    def _create(self, request, context):
        if request.operation not in {"chat_completions", "responses"}:
            raise ModelError("UNSUPPORTED")
        if self._get_access is not None and (request.operation != "responses" or
                                              not request.payload.get("stream") or request.payload.get("store") is not False):
            raise ModelError("INVALID_REQUEST")
        headers = {}
        if self._get_access is not None:
            credential = self._get_access(context)
            headers["Authorization"] = "Bearer " + credential.bearer
        context.CheckCancelled()
        remaining = context.Remaining()
        timeout = self._httpx.Timeout(min(self._read_timeout, remaining),
                                     connect=min(self._connect_timeout, remaining))
        client = self._client.with_options(timeout=timeout)
        create = client.responses.create if request.operation == "responses" else client.chat.completions.create
        return create(**request.payload, **({"extra_headers": headers} if headers else {}))

    @contextmanager
    def _borrow(self, context):
        context.CheckCancelled()
        with self._condition:
            context.CheckCancelled()
            if self._closing or self._closed:
                raise DomainError("NOT_READY", "Provider closed")
            self._active_borrowers += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_borrowers -= 1
                self._condition.notify_all()

    @contextmanager
    def Stream(self, request, context):
        """Keep the borrow until iteration and actual response close finish."""
        with self._borrow(context), self._errors(context):
            context.CheckCancelled()
            stream = self._create(request, context)
            original_stream = stream.response.stream
            class ResponseLease(self._httpx.SyncByteStream):
                # HTTPX sets response.is_closed before actual close. Keep the
                # resource and success state independently, including EOF close.
                def __init__(lease):
                    lease.closed, lease.lock = False, Lock()
                def __iter__(lease):
                    yield from original_stream
                def close(lease):
                    with lease.lock:
                        if not lease.closed:
                            original_stream.close()
                            lease.closed = True
            lease = ResponseLease()
            stream.response.stream = lease
            stopped = Event()

            def watch():
                while not stopped.wait(0.02):
                    if context.cancel.is_set() or context.Remaining() <= 0:
                        # Closing the HTTP response interrupts an active socket
                        # read; ownership stays here until that read unwinds.
                        try:
                            stream.close()
                        except Exception:
                            try:
                                lease.close()
                            except Exception:
                                pass
                        return

            watcher = None
            try:
                watcher = Thread(target=watch, name="funagent-stream-control")
                watcher.start()
                def packets():
                    first_output = False
                    mgr = ObservationMgr.GetInstance()
                    for packet in stream:
                        received = packet.model_dump(warnings=False)
                        if Usage(received):
                            mgr.Update(usage=lambda: Usage(received))
                        if not first_output and StreamHasOutput(received):
                            first_output = True
                            mgr.Update(first_output_at=time.time())
                        try:
                            context.CheckCancelled()
                        except ControlError:
                            mgr.Update(output=lambda: {'received_unprocessed': received})
                            raise
                        yield ProviderResponse(received)
                    context.CheckCancelled()
                yield packets()
            except ModelError as error:
                error.http_status = error.http_status or stream.response.status_code
                error.request_id = error.request_id or SafeModelIdentifier(stream.response.headers.get('x-request-id'))
                raise
            finally:
                stopped.set()
                if watcher is not None and watcher.ident is not None:
                    watcher.join()
                try:
                    stream.close()
                finally:
                    if not lease.closed:
                        try:
                            lease.close()
                        except Exception:
                            with self._condition:
                                self._pending_streams.append(lease)
                            raise DomainError('CLOSE_FAILED', 'Response stream close failed') from None

    def _send(self, request, context):
        if self._closed:
            raise DomainError("NOT_READY", "Provider closed")
        context.CheckCancelled()
        with self._errors(context):
            response = self._create(request, context)
            mgr = ObservationMgr.GetInstance()
            mgr.Update(output=lambda: response.model_dump(warnings=False),
                       usage=lambda: Usage(response.model_dump(warnings=False)), response_received=True)
            context.CheckCancelled()
            # Malformed SDK objects are validated by the adapter. Serializer
            # warnings can contain raw provider payloads, so do not emit them.
            return ProviderResponse(response.model_dump(warnings=False))

    @contextmanager
    def _errors(self, context):
        try:
            yield
        except ControlError:
            raise
        except ModelError:
            raise
        except DomainError:
            raise
        except (json.JSONDecodeError, UnicodeError):
            context.CheckCancelled()
            raise ModelError("PROTOCOL") from None
        except self._httpx.TimeoutException:
            context.CheckCancelled()
            raise ModelError("TIMEOUT") from None
        except self._httpx.TransportError:
            context.CheckCancelled()
            raise ModelError("TRANSPORT") from None
        except self._openai.APITimeoutError:
            context.CheckCancelled()
            raise ModelError("TIMEOUT") from None
        except self._openai.APIConnectionError as error:
            context.CheckCancelled()
            if isinstance(error.__cause__, ControlError):
                raise error.__cause__ from None
            raise ModelError("TRANSPORT") from None
        except self._openai.APIStatusError as error:
            context.CheckCancelled()
            raise RemoteError(error.status_code, error.code, getattr(error, 'request_id', None),
                              RetryAfter(error.response.headers.get("retry-after")),
                              siwc=self._get_access is not None) from None
        except self._openai.APIError as error:
            body = error.body if isinstance(error.body, dict) else {}
            raise RemoteError(remote_code=body.get('code'), siwc=self._get_access is not None) from None
        except Exception:
            context.CheckCancelled()
            raise ModelError("PROVIDER_FATAL") from None

    def Close(self):
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            while self._active_borrowers or self._close_in_progress:
                self._condition.wait()
            if self._closed:
                return
            self._close_in_progress = True
        client_close_attempted = False
        try:
            failed_streams = []
            for lease in self._pending_streams:
                try:
                    lease.close()
                except Exception:
                    failed_streams.append(lease)
            self._pending_streams = failed_streams
            if failed_streams:
                raise DomainError('CLOSE_FAILED', 'Response stream close failed')
            if self._retry_transports:
                failed = []
                for transport in self._retry_transports:
                    try:
                        transport.close()
                    except Exception:
                        failed.append(transport)
                self._retry_transports = tuple(failed)
                if failed:
                    raise DomainError("CLOSE_FAILED", "Provider close failed")
            client_close_attempted = True
            self._client.close()
        except Exception:
            if client_close_attempted and not self._retry_transports:
                self._retry_transports = self._owned_transports
            raise DomainError("CLOSE_FAILED", "Provider close failed") from None
        else:
            with self._condition:
                self._closed = True
        finally:
            with self._condition:
                self._close_in_progress = False
                self._condition.notify_all()
