"""Isolated per-Session agent-browser CLI sessions."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from agentcore.interfaces import ToolContext
from .network_safety import validate_url
from .web_tool import _minimal_env
from .lifecycle import ResourceUnavailableError, SessionResourcesBusyError, SessionResourceKey, remaining


@dataclass(slots=True)
class _BrowserSession:
    name: str
    temp: tempfile.TemporaryDirectory[str]
    lock: threading.RLock = field(default_factory=threading.RLock)
    navigated: bool = False
    last_used: float = field(default_factory=time.monotonic)
    closing: bool = False


class AgentBrowserBackend:
    @classmethod
    def available(cls) -> bool:
        binary = shutil.which("agent-browser")
        if binary is None:
            return False
        try:
            result = subprocess.run(
                [binary, "--version"], capture_output=True,
                timeout=5, check=True, text=True,
                env=_minimal_env(),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            match = re.search(r"(\d+)\.(\d+)", result.stdout + result.stderr)
            return bool(match and tuple(map(int, match.groups())) >= (0, 24))
        except (OSError, subprocess.SubprocessError):
            return False

    def __init__(self, *, allow_private: bool, idle_seconds: float) -> None:
        self.allow_private = allow_private
        self.idle_seconds = idle_seconds
        self.binary = shutil.which("agent-browser")
        if self.binary is None:
            raise RuntimeError(
                "agent-browser is not installed; install it explicitly and make it available on PATH"
            )
        self._lock = threading.RLock()
        self._closing = False
        self._sessions: dict[SessionResourceKey, _BrowserSession] = {}

    def _session(self, session_id: SessionResourceKey) -> _BrowserSession:
        with self._lock:
            if self._closing:
                raise ResourceUnavailableError("backend is stopping")
            session = self._sessions.get(session_id)
            if session is None:
                session = _BrowserSession(
                    name="funagent_" + uuid.uuid4().hex[:16],
                    temp=tempfile.TemporaryDirectory(prefix="funagent-browser-"),
                )
                self._sessions[session_id] = session
            return session

    @contextmanager
    def _locked_session(self, key: SessionResourceKey, check_active):
        # A cleanup may remove a cached instance while we wait for its lock.
        # Re-check identity after locking so we never operate on a closed instance.
        while True:
            check_active()
            session = self._session(key)
            while not session.lock.acquire(timeout=0.05):
                check_active()
            try:
                with self._lock:
                    if self._sessions.get(key) is not session:
                        continue
                    if self._closing or session.closing:
                        raise ResourceUnavailableError("session resources are closing")
                check_active()
                try:
                    yield session
                finally:
                    session.last_used = time.monotonic()
                return
            finally:
                session.lock.release()

    def _command(
        self, session: _BrowserSession, command: str, args: list[str],
        context: ToolContext | None, *, timeout: float = 30,
    ) -> dict[str, Any]:
        env = _minimal_env()
        env["AGENT_BROWSER_SOCKET_DIR"] = session.temp.name
        argv = [self.binary, "--session", session.name, "--json", command, *args]
        if context is not None:
            context.check_active()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.DEVNULL, env=env,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            try:
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if context is not None:
                        context.check_active()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"browser {command} timed out")
                    time.sleep(0.05)
                output.seek(0)
                data = output.read(100_001)
                if len(data) > 100_000:
                    raise ValueError("browser output exceeded size limit")
                if process.returncode != 0:
                    raise RuntimeError(f"agent-browser {command} failed")
                parsed = json.loads(data)
                if not isinstance(parsed, dict) or not parsed.get("success"):
                    raise RuntimeError(
                        str(parsed.get("error", "browser action failed"))[:1000]
                    )
                return parsed
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def _current_url(
        self, session: _BrowserSession, context: ToolContext,
    ) -> str:
        response = self._command(session, "get", ["url"], context)
        data = response.get("data")
        if isinstance(data, dict):
            return str(data.get("url") or data.get("value") or "")
        return str(data or "")

    def _check_current(
        self, session: _BrowserSession, context: ToolContext,
    ) -> str:
        url = self._current_url(session, context)
        validate_url(url, allow_private=self.allow_private)
        return url

    def action(
        self, session_id: SessionResourceKey, command: str, args: list[str],
        context: ToolContext, *, navigation_url: str | None = None,
        snapshot: bool = False,
    ) -> dict[str, Any]:
        context.check_active()
        with self._locked_session(session_id, context.check_active) as session:
            try:
                if navigation_url is not None:
                    validate_url(
                        navigation_url, allow_private=self.allow_private
                    )
                elif not session.navigated:
                    raise RuntimeError("browser_navigate must be called first")
                else:
                    self._check_current(session, context)
                result = self._command(session, command, args, context)
            except BaseException:
                self.close_session(session_id, deadline=time.monotonic() + 5)
                raise
            try:
                final_url = self._check_current(session, context)
            except BaseException:
                self.close_session(session_id, deadline=time.monotonic() + 5)
                raise
            session.navigated = True
            payload: dict[str, Any] = {
                "success": True, "url": final_url,
                "data": result.get("data"),
            }
            if snapshot:
                view = self._command(session, "snapshot", ["-c"], context)
                payload["snapshot"] = view.get("data")
            return _bound_browser_payload(payload)

    def close_session(self, session_id: SessionResourceKey, *, deadline: float,
                      idle_before: float | None = None) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        # Reaping is opportunistic; explicit reset/close waits only within budget.
        if not session.lock.acquire(timeout=0 if idle_before is not None else remaining(deadline)):
            if idle_before is not None:
                return
            raise SessionResourcesBusyError("session is still in use")
        try:
            with self._lock:
                if self._sessions.get(session_id) is not session:
                    return
                if idle_before is not None and not session.closing and session.last_used > idle_before:
                    return
                if remaining(deadline) <= 0:
                    raise TimeoutError("cleanup deadline expired")
                session.closing = True
            self._command(session, "close", [], None, timeout=remaining(deadline))
            session.temp.cleanup()
            with self._lock:
                self._sessions.pop(session_id)
        finally:
            session.lock.release()

    def reap_idle(self, *, now: float, deadline: float) -> None:
        with self._lock:
            candidates = tuple(self._sessions)
        failure = None
        for sid in candidates:
            try:
                self.close_session(sid, deadline=deadline, idle_before=now - self.idle_seconds)
            except Exception as exc:
                failure = exc
        if failure is not None:
            raise failure

    def close(self, *, deadline: float) -> None:
        with self._lock:
            self._closing = True
            sessions = tuple(self._sessions)
        failure = None
        for sid in sessions:
            try:
                self.close_session(sid, deadline=deadline)
            except Exception as exc:
                failure = exc
        if failure is not None:
            raise failure




def _bound_browser_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(payload, ensure_ascii=False, default=str)
    if len(rendered) <= 24_000:
        return payload
    compact = {
        "success": True, "url": payload.get("url"),
        "truncated": True, "preview": rendered[:20_000],
    }
    return compact


__all__ = ["AgentBrowserBackend"]
