"""One selected SDK session, owned by a draining protocol thread."""
import asyncio
import concurrent.futures
from contextlib import AsyncExitStack
from datetime import timedelta
import json
import os
from threading import Condition, Event, Thread
import time
from urllib.parse import urlsplit

from ...frame.contracts import ControlError, DomainError, RunContext, thaw
from ..contracts import Availability, BackendResult, RemoteToolSpec, ToolDecision
from ...frame.schema import ToolValidator
from .diagnostics import SDKDiagnostics


class MCPBackend:
    def __init__(self, settings):
        # Only the selected driver imports its optional SDK.
        from mcp import ClientSession
        from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams
        import anyio

        class Session(ClientSession):
            async def send_request(session, *args, **kwargs):
                identifier = session._request_id
                try:
                    return await super().send_request(*args, **kwargs)
                except asyncio.CancelledError:
                    with anyio.CancelScope(shield=True):
                        with anyio.move_on_after(0.5):
                            await session.send_notification(ClientNotification(CancelledNotification(
                                params=CancelledNotificationParams(requestId=identifier, reason="Request interrupted"))))
                    raise

        self._session_type = Session
        self._settings = thaw(settings)
        if settings.get("command_env"):
            self._settings["command"] = os.environ.get(settings["command_env"])
            if not self._settings["command"]:
                raise DomainError("CONFIG_CREDENTIAL", "MCP command environment is missing")
        if "args_env" in settings:
            self._settings["args"] = [os.environ.get(name) for name in settings["args_env"]]
            if any(value is None for value in self._settings["args"]):
                raise DomainError("CONFIG_CREDENTIAL", "MCP argument environment is missing")
        self._timeout = settings.get("timeout", 10)
        self._drain_timeout = settings.get("drain_timeout", 5)
        self._transport = settings.get("transport", "stdio")
        if self._transport not in {"stdio", "streamable_http", "sse"}:
            raise DomainError("CONFIG_INVALID", "Invalid MCP transport")
        if self._transport == "stdio":
            if not isinstance(self._settings.get("command"), str) or not self._settings["command"].strip():
                raise DomainError("CONFIG_INVALID", "MCP command is required")
            if any(not isinstance(a, str) for a in self._settings.get("args", ())):
                raise DomainError("CONFIG_INVALID", "Invalid MCP arguments")
        else:
            parsed = urlsplit(settings.get("endpoint", ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise DomainError("CONFIG_INVALID", "Invalid MCP endpoint")
        self._credentials = {}
        for name, variable in settings.get("env", {}).items():
            value = os.environ.get(variable)
            if not value:
                raise DomainError("CONFIG_CREDENTIAL", "MCP credential is missing")
            self._credentials[name] = value
        self._headers = {}
        if settings.get("credential_env"):
            token = os.environ.get(settings["credential_env"])
            if not token:
                raise DomainError("CONFIG_CREDENTIAL", "MCP credential is missing")
            self._headers["Authorization"] = "Bearer " + token
        self._condition = Condition()
        self._active, self._closing, self._closed = 0, False, False
        self._ready = Event()
        self._failure = None
        self._tasks = set()
        self._specs = {}
        self._diagnostics = SDKDiagnostics()
        self._thread = Thread(target=self._run, name="funagent-mcp", daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._diagnostics.Close()
            raise
        if not self._ready.wait(self._timeout + 1) or self._failure:
            self.Close()
            raise DomainError("MCP_CONNECTION", "MCP initialization failed")

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException:
            self._failure = "MCP_CONNECTION"
        finally:
            self._ready.set()

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.sse import sse_client
        import httpx
        async with AsyncExitStack() as stack:
            if self._transport == "stdio":
                errlog = stack.enter_context(open(os.devnull, "w"))
                streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                    command=self._settings["command"], args=self._settings.get("args", []),
                    env=self._credentials, cwd=self._settings.get("cwd")), errlog=errlog))
            elif self._transport == "streamable_http":
                client = await stack.enter_async_context(httpx.AsyncClient(
                    headers=self._headers, timeout=self._timeout, trust_env=False))
                streams = await stack.enter_async_context(streamable_http_client(
                    self._settings["endpoint"], http_client=client))
            else:
                streams = await stack.enter_async_context(sse_client(self._settings["endpoint"],
                    headers=self._headers, timeout=self._timeout, sse_read_timeout=self._timeout))
            self._session = await stack.enter_async_context(self._session_type(
                streams[0], streams[1], read_timeout_seconds=timedelta(seconds=self._timeout)))
            async with asyncio.timeout(self._timeout):
                initialized = await self._session.initialize()
                if initialized.capabilities.tools is None:
                    raise DomainError("MCP_PROTOCOL", "MCP tools capability is required")
            self._ready.set()
            await self._stop.wait()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _invoke(self, method, args, timeout, done):
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with asyncio.timeout(timeout):
                return await getattr(self._session, method)(**args)
        finally:
            self._tasks.discard(task)
            done.set()

    def _call(self, method, args, context):
        context.CheckCancelled()
        with self._condition:
            if self._closing or self._failure or not self._thread.is_alive():
                raise DomainError("MCP_CONNECTION", "MCP connection is unavailable")
            self._active += 1
        done, future = Event(), None
        try:
            context.CheckCancelled()
            future = asyncio.run_coroutine_threadsafe(self._invoke(method, args,
                min(self._timeout, context.Remaining()), done), self._loop)
            limit = time.monotonic() + min(self._timeout, context.Remaining())
            while True:
                context.CheckCancelled()
                if time.monotonic() >= limit:
                    raise DomainError("MCP_TIMEOUT", "MCP request timed out")
                try:
                    value = future.result(0.02)
                    context.CheckCancelled()
                    return value
                except concurrent.futures.TimeoutError:
                    if future.done():
                        raise DomainError("MCP_TIMEOUT", "MCP request timed out") from None
        except (ControlError, DomainError):
            raise
        except Exception:
            context.CheckCancelled()
            raise DomainError("MCP_PROTOCOL", "MCP protocol request failed") from None
        finally:
            if future is not None and not done.is_set():
                future.cancel()
                # Cancellation of the concurrent Future is not IO completion.
                done.wait(self._drain_timeout)
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def ListTools(self, context):
        specs, cursor, seen = [], None, set()
        while True:
            page = self._call("list_tools", {"cursor": cursor}, context)
            for tool in page.tools:
                ToolValidator(tool.inputSchema)
                if tool.name in {s.name for s in specs} or len(specs) >= 1000:
                    raise DomainError("MCP_PROTOCOL", "Invalid MCP tool list")
                # Remote annotations are hints; only local configuration grants read-only access.
                decision = ToolDecision("allow", "none") if tool.name in self._settings.get("read_only", ()) else ToolDecision("approval", "unknown")
                specs.append(RemoteToolSpec(tool.name, tool.description or tool.name, tool.inputSchema, decision))
            cursor = page.nextCursor
            if not cursor:
                break
            if cursor in seen:
                raise DomainError("MCP_PROTOCOL", "Invalid MCP pagination")
            seen.add(cursor)
        self._specs = {s.name: s for s in specs if s.name not in self._settings.get("exclude", ())
                       and ("include" not in self._settings or s.name in self._settings["include"])}
        return tuple(specs)

    def Execute(self, operation, args, context):
        context.CheckCancelled()
        spec = self._specs.get(operation)
        if spec is None:
            return BackendResult("denied", '{"code":"MCP_TOOL_UNAVAILABLE"}')
        if not ToolValidator(spec.parameters).is_valid(thaw(args)):
            return BackendResult("invalid", '{"code":"invalid_arguments"}')
        effect = spec.decision.effect
        try:
            result = self._call("call_tool", {"name": operation, "arguments": thaw(args)}, context)
            if result.isError:
                return BackendResult("error", '{"code":"MCP_TOOL_ERROR"}', effect)
            encoded = json.dumps(result.model_dump(by_alias=True, mode="json", exclude_none=True), ensure_ascii=False)
            if len(encoded.encode("utf8")) > 1048576:
                raise DomainError("MCP_PROTOCOL", "MCP result is too large")
            return BackendResult("ok", encoded, effect)
        except ControlError:
            if effect == "none":
                raise
            return BackendResult("unknown", '{"code":"MCP_INTERRUPTED"}', "unknown")
        except DomainError as error:
            return BackendResult("error" if effect == "none" else "unknown",
                                 json.dumps({"code": error.code}), effect)

    def ToolSpec(self, operation):
        return self._specs.get(operation)

    def Probe(self):
        return Availability(not self._closing and not self._failure and self._thread.is_alive(), {"mcp"})

    def Close(self):
        with self._condition:
            if self._closed:
                return
            self._closing = True
            limit = time.monotonic() + self._drain_timeout
            while self._active:
                remaining = limit - time.monotonic()
                if remaining <= 0:
                    raise DomainError("CLOSE_FAILED", "MCP borrowers are still active")
                self._condition.wait(min(remaining, 0.02))
        if hasattr(self, "_loop") and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(self._drain_timeout)
        if self._thread.is_alive():
            raise DomainError("CLOSE_FAILED", "MCP protocol thread did not drain")
        self._closed = True
        self._diagnostics.Close()
