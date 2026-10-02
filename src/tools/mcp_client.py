"""Synchronous MCP provider backed by one long-lived asyncio loop."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, CallToolResult, InputRequiredResult, Tool

from .config import McpServerConfig, WorkspaceConfig
from .lifecycle import ResourceUnavailableError, remaining

logger = logging.getLogger(__name__)
_STARTUP_GRACE = 5.0
_MAX_TOOL_PAGES = 50


def _resolve_values(values: Mapping[str, str]) -> dict[str, str]:
    resolved = {}
    for name, value in values.items():
        if value.startswith("env:"):
            variable = value[4:]
            if not variable or variable not in os.environ:
                raise ValueError(f"missing MCP environment variable: {variable}")
            value = os.environ[variable]
        resolved[name] = value
    return resolved


def _transport_failure(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_transport_failure(child) for child in exc.exceptions)
    return isinstance(exc, (
        OSError, httpx2.HTTPError, anyio.BrokenResourceError,
        anyio.ClosedResourceError, anyio.EndOfStream,
    )) or (isinstance(exc, MCPError) and exc.code == CONNECTION_CLOSED)


@dataclass(slots=True)
class _Connection:
    server_id: str
    config: McpServerConfig
    ready: asyncio.Future[tuple[Tool, ...]]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    rpc_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Client | None = None
    task: asyncio.Task[None] | None = None


class McpClient:
    def __init__(self, servers: Mapping[str, McpServerConfig], *, workspace: WorkspaceConfig):
        # Resolve every enabled credential before any server is started.
        self._servers = {}
        for server_id, config in servers.items():
            if not server_id.strip():
                raise ValueError("MCP server id must not be blank")
            if config.enabled:
                self._servers[server_id] = config.model_copy(update={
                    "env": _resolve_values(config.env),
                    "headers": _resolve_values(config.headers),
                })
        self._workspace = workspace
        self._lock = threading.Lock()
        self._available: set[str] = set()
        self._statuses: dict[str, str] = {}
        self._closing = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready: Future[asyncio.AbstractEventLoop] = Future()
        self._thread: threading.Thread | None = None
        self._loop_error: BaseException | None = None
        self._discover_future: Future | None = None
        self._close_future: Future | None = None
        # These collections and all SDK objects are used only on mcp_loop.
        self._connections: dict[str, _Connection] = {}
        self._inflight: dict[asyncio.Task, _Connection] = {}

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._serve_loop())
        except BaseException as exc:
            self._loop_error = exc
            if not self._loop_ready.done():
                self._loop_ready.set_exception(exc)
            logger.error("mcp.loop_failed error_type=%s", type(exc).__name__)

    async def _serve_loop(self) -> None:
        self._loop_stop = asyncio.Event()
        self._loop_ready.set_result(asyncio.get_running_loop())
        await self._loop_stop.wait()

    def _submit_locked(self, factory: Callable[[], Coroutine]) -> Future:
        if self._loop is None or self._loop.is_closed():
            raise ResourceUnavailableError("MCP loop is unavailable")
        coroutine = factory()
        try:
            return asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        except BaseException:
            coroutine.close()
            raise

    def discover(self) -> dict[str, tuple[Tool, ...]]:
        with self._lock:
            if self._closing:
                raise ResourceUnavailableError("MCP provider is stopping")
            if not self._servers:
                return {}
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run_loop, name="mcp-loop", daemon=True,
                )
                try:
                    self._thread.start()
                except BaseException:
                    self._thread = None
                    raise
        loop = self._loop_ready.result(timeout=_STARTUP_GRACE)
        with self._lock:
            if self._closing:
                raise ResourceUnavailableError("MCP provider is stopping")
            self._loop = loop
            if self._discover_future is None:
                self._discover_future = self._submit_locked(self._discover)
            future = self._discover_future
        budget = max(c.connect_timeout_seconds for c in self._servers.values()) + _STARTUP_GRACE
        try:
            return future.result(timeout=budget)
        except FutureTimeout:
            if future.done():
                return future.result()
            # Owners, not this Future, own transport cleanup.
            future.cancel()
            raise TimeoutError("MCP discovery exceeded its startup budget") from None

    async def _discover(self) -> dict[str, tuple[Tool, ...]]:
        loop = asyncio.get_running_loop()
        for server_id, config in self._servers.items():
            connection = _Connection(server_id, config, loop.create_future())
            self._connections[server_id] = connection
            connection.task = asyncio.create_task(
                self._serve_connection(connection), name=f"mcp-connection:{server_id}",
            )
            connection.task.add_done_callback(self._consume_task_exception)
        connections = tuple(self._connections.values())
        results = await asyncio.gather(
            *(asyncio.shield(c.ready) for c in connections), return_exceptions=True,
        )
        discovered = {}
        required_failures = []
        failed_tasks = []
        for connection, result in zip(connections, results):
            if isinstance(result, BaseException):
                if connection.config.required:
                    required_failures.append(connection.server_id)
                failed_tasks.append(connection.task)
            else:
                discovered[connection.server_id] = result
        if required_failures:
            raise RuntimeError(f"required MCP servers failed: {', '.join(required_failures)}")
        if failed_tasks:
            await asyncio.gather(*failed_tasks, return_exceptions=True)
        return discovered

    @staticmethod
    def _consume_task_exception(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _open_client(self, connection: _Connection, stack: AsyncExitStack) -> Client:
        config = connection.config

        async def on_message(message):
            if isinstance(message, Exception) and _transport_failure(message):
                self._disable(connection)

        if config.command is not None:
            server = StdioServerParameters(
                command=config.command, args=list(config.args), env=config.env,
                cwd=str(self._workspace.default_cwd) if self._workspace.default_cwd else None,
            )
        elif config.headers:
            http_client = await stack.enter_async_context(httpx2.AsyncClient(
                headers=config.headers, timeout=httpx2.Timeout(30.0, read=300.0),
            ))
            server = streamable_http_client(config.url, http_client=http_client)
        else:
            server = config.url
        return await stack.enter_async_context(Client(server, message_handler=on_message, cache=None))

    async def _list_all_tools(self, client: Client) -> tuple[Tool, ...]:
        if client.server_capabilities.tools is None:
            return ()
        tools = []
        cursor = None
        seen = set()
        for _ in range(_MAX_TOOL_PAGES):
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if page.next_cursor is None:
                return tuple(tools)
            if page.next_cursor in seen:
                raise RuntimeError("MCP tools/list repeated a cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor
        raise RuntimeError("MCP tools/list exceeded the page limit")

    async def _serve_connection(self, connection: _Connection) -> None:
        started = time.monotonic()
        with self._lock:
            self._statuses[connection.server_id] = "connecting"
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(connection.config.connect_timeout_seconds):
                        client = await self._open_client(connection, stack)
                        tools = await self._list_all_tools(client)
                except BaseException as exc:
                    with self._lock:
                        self._statuses[connection.server_id] = "failed"
                    if not connection.ready.done():
                        connection.ready.set_exception(exc)
                    logger.warning("mcp.start_failed server=%s error_type=%s",
                                   connection.server_id, type(exc).__name__)
                    return
                connection.client = client
                with self._lock:
                    if not self._closing and not connection.stop.is_set():
                        self._available.add(connection.server_id)
                        self._statuses[connection.server_id] = "available"
                connection.ready.set_result(tools)
                logger.info("mcp.ready server=%s tools=%s elapsed_ms=%s", connection.server_id,
                            len(tools), int((time.monotonic() - started) * 1000))
                try:
                    await connection.stop.wait()
                finally:
                    self._disable(connection)
                    # A failed SDK task group must not interrupt draining our RPCs.
                    with anyio.CancelScope(shield=True):
                        await self._drain_rpcs(connection)
        finally:
            connection.client = None
            self._disable(connection)
            if not connection.ready.done():
                connection.ready.set_exception(ResourceUnavailableError("MCP connection stopped"))

    def _disable(self, connection: _Connection) -> None:
        with self._lock:
            self._available.discard(connection.server_id)
            if self._statuses.get(connection.server_id) != "failed":
                self._statuses[connection.server_id] = "unavailable"
        connection.stop.set()

    def is_available(self, server_id: str) -> bool:
        with self._lock:
            return not self._closing and server_id in self._available

    def status_snapshot(self) -> dict[str, str]:
        with self._lock:
            return {name: ("stopping" if self._closing else self._statuses.get(name, "uninitialized"))
                    for name in self._servers}

    def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any],
                  *, check_active: Callable[[], None]) -> CallToolResult:
        check_active()
        with self._lock:
            if self._closing or server_id not in self._available:
                raise ResourceUnavailableError(f"MCP server is unavailable: {server_id}")
            future = self._submit_locked(lambda: self._rpc(server_id, tool_name, arguments))
        deadline = time.monotonic() + self._servers[server_id].tool_timeout_seconds
        try:
            while True:
                check_active()
                budget = remaining(deadline)
                if not budget:
                    raise TimeoutError(f"MCP tool timed out: {server_id}/{tool_name}")
                try:
                    return future.result(timeout=min(0.05, budget))
                except FutureTimeout:
                    if future.done():
                        return future.result()
        except BaseException:
            if not future.done():
                future.cancel()
            raise

    async def _rpc(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> CallToolResult:
        task = asyncio.current_task()
        connection = self._connections[server_id]
        self._inflight[task] = connection
        try:
            async with connection.rpc_lock:
                if not self.is_available(server_id) or connection.client is None:
                    raise ResourceUnavailableError(f"MCP server is unavailable: {server_id}")
                try:
                    result = await connection.client.session.call_tool(
                        tool_name, arguments, allow_input_required=True,
                    )
                except Exception as exc:
                    if _transport_failure(exc):
                        self._disable(connection)
                    # Transport messages can contain URLs/headers; expose only safe metadata.
                    raise RuntimeError(f"MCP call failed: {server_id} ({type(exc).__name__})") from None
                if isinstance(result, InputRequiredResult):
                    raise RuntimeError(f"MCP input continuation is unsupported: {server_id}/{tool_name}")
                return result
        finally:
            self._inflight.pop(task, None)

    async def _drain_rpcs(self, connection: _Connection | None = None) -> None:
        tasks = tuple(t for t, c in self._inflight.items() if connection is None or c is connection)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _shutdown(self) -> None:
        await self._drain_rpcs()
        tasks = []
        for connection in self._connections.values():
            connection.stop.set()
            task = connection.task
            if not connection.ready.done() and not task.done() and not task.cancelling():
                task.cancel()
            tasks.append(task)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if any(isinstance(r, Exception) for r in results):
            raise RuntimeError("MCP connection cleanup failed")
        self._loop_stop.set()

    def close(self, *, deadline: float) -> None:
        with self._lock:
            if self._closed:
                return
            self._closing = True
            if self._thread is None:
                self._closed = True
                return
            if self._loop_ready.done() and self._loop_ready.exception() is not None:
                # The loop failed before admitting any connection or RPC.
                self._thread.join(remaining(deadline))
                if self._thread.is_alive():
                    raise TimeoutError("MCP loop is still closing")
                self._closed = True
                return
        loop = self._loop_ready.result(timeout=remaining(deadline))
        with self._lock:
            self._loop = loop
            if self._close_future is None:
                self._close_future = self._submit_locked(self._shutdown)
            future = self._close_future
        future.result(timeout=remaining(deadline))
        self._thread.join(remaining(deadline))
        if self._thread.is_alive():
            raise TimeoutError("MCP loop is still closing")
        if self._loop_error is not None:
            raise RuntimeError("MCP loop failed during cleanup") from None
        with self._lock:
            self._closed = True

    def close_session(self, key, *, deadline: float) -> None:
        pass

    def reap_idle(self, *, now: float, deadline: float) -> None:
        pass
