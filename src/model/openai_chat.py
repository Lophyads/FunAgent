"""Chat Completions wire adapter and one SDK attempt, with implicit retry disabled."""
import json
import os
import time
from collections.abc import Mapping
from contextlib import contextmanager
from threading import Condition, Event, Thread, Lock

from ..frame.contracts import ControlError, DomainError, ModelReply, ModelRequest, ToolCall, thaw
from .protocols import Adapter, ModelError, ProviderRequest, ProviderResponse
from .retry import RetryAfter
from .validation import ValidateReply
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Usage, StreamHasOutput


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError




class OpenAIChatAdapter(Adapter[ModelRequest, ModelReply]):
    @staticmethod
    def Encode(request, profile):
        messages = []
        for message in request.messages:
            wire = {"role": message.role, "content": message.content}
            if message.calls:
                wire["tool_calls"] = [{"id": c.call_id, "type": "function", "function": {
                    "name": c.name, "arguments": json.dumps({} if c.argument_error else thaw(c.args),
                                                             ensure_ascii=False, allow_nan=False)}} for c in message.calls]
            if message.role == "tool":
                wire["tool_call_id"] = message.call_id
            messages.append(wire)
        payload = {"model": profile.model, "messages": messages, "stream": profile.streaming}
        if profile.streaming:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {"name": t.name,
                "description": t.description, "parameters": thaw(t.parameters)}} for t in request.tools]
        return ProviderRequest(payload)

    @staticmethod
    def Decode(response):
        try:
            data = response.payload
            if len(data["choices"]) != 1:
                raise ValueError
            choice = data["choices"][0]
            message = choice["message"]
            if message["role"] != "assistant":
                raise ValueError
            calls = []
            raw_calls = message.get("tool_calls")
            if raw_calls is not None and not isinstance(raw_calls, list):
                raise ValueError
            for call in raw_calls or ():
                if call["type"] != "function":
                    raise ValueError
                function = call["function"]
                try:
                    args = json.loads(function.get("arguments"), object_pairs_hook=_unique_object,
                                      parse_constant=_reject_constant)
                    if not isinstance(args, dict):
                        raise ValueError
                    parsed = ToolCall(call["id"], function["name"], args)
                except (TypeError, ValueError, RecursionError):
                    parsed = ToolCall(call["id"], function["name"], {}, "invalid_arguments")
                calls.append(parsed)
            usage = data.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError
            usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}
            return ValidateReply(ModelReply(message.get("content"), tuple(calls), choice["finish_reason"], usage))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError("PROTOCOL") from None

    @staticmethod
    def NewStream():
        from .chat_accumulator import ChatAccumulator
        return ChatAccumulator(OpenAIChatAdapter)


class OpenAIProvider:
    def __init__(self, options, *, transport=None):
        try:
            import openai
            import httpx
        except ImportError:
            raise DomainError("DRIVER_UNAVAILABLE", "OpenAI SDK is unavailable") from None
        credential_env = options.get("credential_env", "OPENAI_API_KEY")
        token = os.environ.get(credential_env)
        if not token:
            raise DomainError("CONFIG_CREDENTIAL", "Model credential environment variable is missing")
        self._openai = openai
        self._httpx = httpx
        self._closed = False
        self._closing = False
        self._active_borrowers = 0
        self._close_in_progress = False
        self._condition = Condition()
        client = httpx.Client(transport=transport, timeout=30)
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
            client = self._client.with_options(timeout=min(30, context.Remaining()))
            context.CheckCancelled()
            stream = client.chat.completions.create(**request.payload)
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
                        if isinstance(received, Mapping) and received.get('usage') is not None:
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
            client = self._client.with_options(timeout=min(30, context.Remaining()))
            context.CheckCancelled()
            response = client.chat.completions.create(**request.payload)
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
            status = error.status_code
            code = ("AUTH" if status in {401,403} else "RATE_LIMIT" if status == 429
                    else "MODEL_NOT_FOUND" if status == 404 and error.code in
                        (None, "model_not_found", "deployment_not_found", "endpoint_not_found", "DeploymentNotFound")
                    else "TIMEOUT" if status == 408 else "TRANSPORT" if status >= 500 or status == 409
                    else "INVALID_REQUEST")
            raise ModelError(code, retry_after=RetryAfter(error.response.headers.get("retry-after"))) from None
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
