"""Synchronous, text-only cua-driver CLI adapter for the runtime host."""

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
from dataclasses import dataclass, field
from typing import Any

from agentcore.interfaces import ToolContext
from .web_tool import _minimal_env
from .lifecycle import SessionResourceKey, remaining


@dataclass(slots=True)
class _DesktopSession:
    driver_session: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    target: dict[str, Any] | None = None
    last_used: float = field(default_factory=time.monotonic)
    started: bool = False
    elements: dict[int, dict[str, Any]] = field(default_factory=dict)


class CuaCliBackend:
    _availability_lock = threading.RLock()
    _availability_cache: tuple[tuple, float, bool] | None = None

    @classmethod
    def available(cls, *, refresh: bool = False, ttl: float = 30) -> bool:
        binary = shutil.which("cua-driver")
        environment = tuple(
            (name, os.environ.get(name)) for name in (
                "PATH", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
                "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
            )
        )
        key = (binary, environment)
        with cls._availability_lock:
            cached = cls._availability_cache
            if not refresh and cached and cached[0] == key and time.monotonic() - cached[1] < ttl:
                return cached[2]
            result = cls._probe_available()
            cls._availability_cache = (key, time.monotonic(), result)
            return result

    @classmethod
    def _probe_available(cls) -> bool:
        binary = shutil.which("cua-driver")
        if binary is None:
            return False
        try:
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True,
                timeout=5, env=_minimal_env(),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            match = re.search(r"(\d+)\.(\d+)", version.stdout + version.stderr)
            if not match or tuple(map(int, match.groups())) < (0, 26):
                return False
            status = subprocess.run(
                [binary, "status"], capture_output=True, text=True,
                timeout=5, env=_minimal_env(),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            return status.returncode == 0 and "is running" in status.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    def __init__(self, *, idle_seconds: float = 900.0) -> None:
        self.binary = shutil.which("cua-driver")
        if self.binary is None:
            raise RuntimeError(
                "cua-driver is not installed or not on PATH; install and start it explicitly"
            )
        self._lock = threading.RLock()
        self._sessions: dict[SessionResourceKey, _DesktopSession] = {}
        self.idle_seconds = idle_seconds

    def _session(self, session_id: SessionResourceKey) -> _DesktopSession:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = _DesktopSession(
                    driver_session="funagent-" + uuid.uuid4().hex[:20]
                )
                self._sessions[session_id] = session
            session.last_used = time.monotonic()
            return session

    def _call(
        self, name: str, args: dict[str, Any], context: ToolContext | None,
        *, timeout: float = 30,
    ) -> dict[str, Any]:
        if context is not None:
            context.check_active()
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [self.binary, "call", name],
                stdin=subprocess.PIPE, stdout=output,
                stderr=subprocess.DEVNULL, env=_minimal_env(),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            try:
                assert process.stdin is not None
                process.stdin.write(
                    json.dumps(args, ensure_ascii=False).encode("utf-8")
                )
                process.stdin.close()
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if context is not None and context.check_active is not None:
                        context.check_active()
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"cua-driver {name} timed out")
                    time.sleep(0.05)
                output.seek(0)
                raw = output.read(256_001)
                if len(raw) > 256_000:
                    raise ValueError("cua-driver output exceeded text limit")
                if process.returncode != 0:
                    raise RuntimeError(f"cua-driver {name} failed")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("cua-driver returned a non-object")
                if value.get("isError") or value.get("error"):
                    raise RuntimeError(str(value.get("error") or "driver error")[:1000])
                return value
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    @staticmethod
    def _structured(result: dict[str, Any]) -> dict[str, Any]:
        value = result.get("structuredContent", result)
        return value if isinstance(value, dict) else {"text": str(value)[:20_000]}

    def _windows(self, session: _DesktopSession, context: ToolContext) -> list[dict]:
        result = self._call(
            "list_windows",
            {"on_screen_only": True, "session": session.driver_session},
            context, timeout=20,
        )
        structured = self._structured(result)
        windows = structured.get("windows") or result.get("windows") or []
        return [item for item in windows if isinstance(item, dict)]

    def execute(
        self, session_id: SessionResourceKey, args: Any, context: ToolContext,
    ) -> dict[str, Any]:
        context.check_active()
        session = self._session(session_id)
        action = args.action
        with session.lock:
            sid = session.driver_session
            if not session.started:
                self._call("start_session", {"session": sid}, context)
                session.started = True
            if action == "list_apps":
                result = self._call("list_apps", {"session": sid}, context)
                return self._finish(session, self._bounded(self._structured(result)))
            if action == "list_windows":
                return self._finish(
                    session, self._bounded({
                        "windows": self._windows(session, context)
                    }),
                )
            if action == "wait":
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    if context.check_active is not None:
                        context.check_active()
                    time.sleep(min(0.05, deadline - time.monotonic()))
                return self._finish(
                    session, {
                        "waited_seconds": args.seconds,
                        "verdict": "completed",
                    },
                )
            if action in {"capture", "focus_app"}:
                target = self._resolve_target(session, args, context)
                if action == "focus_app":
                    session.target = target
                    if args.raise_window:
                        result = self._call(
                            "bring_to_front", {
                                "pid": target["pid"],
                                "window_id": target["window_id"],
                                "session": sid,
                            }, context,
                        )
                        return self._finish(session, self._bounded({
                            "target": target,
                            "result": self._structured(result),
                            "verdict": "verify_with_capture",
                        }))
                    return self._finish(
                        session, {"target": target, "verdict": "selected"}
                    )
                result = self._call(
                    "get_window_state", {
                        "pid": target["pid"],
                        "window_id": target["window_id"],
                        "include_screenshot": False,
                        "include_accessibility_tree": True,
                        "max_elements": 300,
                        "session": sid,
                    }, context,
                )
                session.target = target
                content = self._structured(result)
                session.elements = {
                    item["element_index"]: item
                    for item in content.get("elements", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("element_index"), int)
                }
                return self._finish(session, self._bounded({
                    "target": target,
                    "ax_tree": content.get("tree_markdown"),
                    "elements": content.get("elements"),
                    "snapshot_id": content.get("snapshot_id"),
                    "verdict": "use_element_indices_or_re_capture",
                }))
            target = session.target
            if target is None:
                raise ValueError("capture or focus_app must select a target first")
            if args.app and args.app.lower() not in str(
                target.get("app_name", "")
            ).lower():
                raise ValueError("requested app differs from the selected target")
            if args.pid is not None and args.pid != target["pid"]:
                raise ValueError("requested pid differs from the selected target")
            if args.window_id is not None and args.window_id != target["window_id"]:
                raise ValueError("requested window_id differs from the selected target")
            call_args: dict[str, Any] = {
                "pid": target["pid"], "window_id": target["window_id"],
                "session": sid,
            }
            driver_action = action
            if action in {"click", "double_click", "right_click", "middle_click"}:
                driver_action = "double_click" if action == "double_click" else "click"
                call_args["button"] = (
                    "right" if action == "right_click"
                    else "middle" if action == "middle_click" else "left"
                )
                if args.element is None and args.coordinate is None:
                    raise ValueError("click requires element or coordinate")
                if args.element is not None:
                    call_args["element_index"] = args.element
                    if args.snapshot_id:
                        call_args["snapshot_id"] = args.snapshot_id
                else:
                    call_args["x"], call_args["y"] = args.coordinate
            elif action == "drag":
                if args.from_element is not None and args.to_element is not None:
                    start = self._element_center(session, args.from_element)
                    end = self._element_center(session, args.to_element)
                    call_args.update(
                        from_x=start[0], from_y=start[1],
                        to_x=end[0], to_y=end[1],
                    )
                elif args.from_coordinate and args.to_coordinate:
                    call_args.update(
                        from_x=args.from_coordinate[0],
                        from_y=args.from_coordinate[1],
                        to_x=args.to_coordinate[0],
                        to_y=args.to_coordinate[1],
                    )
                else:
                    raise ValueError("drag requires two elements or coordinates")
            elif action == "scroll":
                call_args.update(direction=args.direction, amount=args.amount)
                if args.element is not None:
                    call_args["element_index"] = args.element
            elif action == "type":
                driver_action = "type_text"
                call_args["text"] = args.text
                if args.element is not None:
                    call_args["element_index"] = args.element
                    if args.snapshot_id:
                        call_args["snapshot_id"] = args.snapshot_id
                elif args.coordinate is not None:
                    call_args["x"], call_args["y"] = args.coordinate
            elif action == "key":
                parts = [part.strip() for part in args.keys.replace("-", "+").split("+")]
                if len(parts) > 1:
                    driver_action = "hotkey"
                    call_args["keys"] = parts
                else:
                    driver_action = "press_key"
                    call_args["key"] = parts[0]
            elif action == "set_value":
                if args.element is None:
                    raise ValueError("set_value requires element")
                call_args["element_index"] = args.element
                if args.snapshot_id:
                    call_args["snapshot_id"] = args.snapshot_id
                call_args["value"] = args.value
            if action in {
                "click", "double_click", "right_click", "middle_click",
                "drag", "scroll", "type", "key",
            }:
                call_args["delivery_mode"] = args.delivery_mode
                if args.bring_to_front:
                    self._call("bring_to_front", {
                        "pid": target["pid"], "window_id": target["window_id"],
                        "session": sid,
                    }, context)
            result = self._call(driver_action, call_args, context)
            safe = self._structured(result)
            public_result = {
                key: safe[key] for key in (
                    "verified", "effect", "escalation", "code",
                    "delivery_mode", "success",
                ) if key in safe
            }
            return self._finish(session, self._bounded({
                "action": action, "target": target,
                "result": public_result,
                "verdict": (
                    safe.get("verdict") or safe.get("escalation")
                    or ("verify_with_capture" if action != "set_value" else "check_value")
                ),
            }))

    @staticmethod
    def _finish(
        session: _DesktopSession, payload: dict[str, Any],
    ) -> dict[str, Any]:
        session.last_used = time.monotonic()
        return payload

    @staticmethod
    def _element_center(
        session: _DesktopSession, index: int,
    ) -> tuple[int, int]:
        item = session.elements.get(index)
        frame = item.get("frame") if item is not None else None
        if not isinstance(frame, dict):
            raise ValueError(
                "drag element has no coordinates in the last AX capture"
            )
        return (
            int(frame["x"] + frame["w"] / 2),
            int(frame["y"] + frame["h"] / 2),
        )

    def _resolve_target(
        self, session: _DesktopSession, args: Any, context: ToolContext,
    ) -> dict[str, Any]:
        windows = self._windows(session, context)
        if args.pid is not None:
            windows = [w for w in windows if w.get("pid") == args.pid]
        if args.window_id is not None:
            windows = [w for w in windows if w.get("window_id") == args.window_id]
        if args.app:
            windows = [
                w for w in windows
                if args.app.lower() in str(
                    w.get("app_name") or w.get("name") or ""
                ).lower()
            ]
        if not windows:
            raise ValueError("no matching on-screen window")
        if args.app and len(windows) > 1 and args.window_id is None:
            raise ValueError("app matches multiple windows; specify window_id")
        chosen = windows[0]
        pid, window_id = chosen.get("pid"), chosen.get("window_id")
        if not isinstance(pid, int) or not isinstance(window_id, int):
            raise ValueError("window has no usable pid/window_id")
        return {
            "pid": pid, "window_id": window_id,
            "app_name": str(chosen.get("app_name") or chosen.get("name") or ""),
            "window_title": str(chosen.get("window_title") or chosen.get("title") or ""),
        }

    @staticmethod
    def _bounded(payload: dict[str, Any]) -> dict[str, Any]:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        if len(rendered) <= 24_000:
            return payload
        return {"truncated": True, "preview": rendered[:20_000]}

    def close_session(self, session_id: SessionResourceKey, *, deadline: float) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        if not session.lock.acquire(timeout=remaining(deadline)):
            raise TimeoutError("session is still in use")
        try:
            if remaining(deadline) <= 0:
                raise TimeoutError("cleanup deadline expired")
            self._call("end_session", {"session": session.driver_session}, None, timeout=remaining(deadline))
            with self._lock:
                self._sessions.pop(session_id, None)
        finally:
            session.lock.release()

    def reap_idle(self, *, now: float, deadline: float) -> None:
        with self._lock:
            candidates = tuple(self._sessions.items())
        for sid, session in candidates:
            if now - session.last_used >= self.idle_seconds:
                self.close_session(sid, deadline=deadline)

    def close(self, *, deadline: float) -> None:
        with self._lock:
            sessions = tuple(self._sessions)
        for sid in sessions:
            self.close_session(sid, deadline=deadline)



__all__ = ["CuaCliBackend"]
