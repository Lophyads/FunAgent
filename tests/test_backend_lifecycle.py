from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agentcore import TurnCancelled
from tools.browser_backend import AgentBrowserBackend
from tools.computer_backend import CuaCliBackend
from tools.lifecycle import ResourceUnavailableError, SessionResourcesBusyError


@pytest.fixture(params=["browser", "computer"])
def backend_case(request, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "offline-driver")
    control, closed = {}, []
    if request.param == "browser":
        backend = AgentBrowserBackend(allow_private=False, idle_seconds=60)

        def command(session, name, args, context, **kwargs):
            if name == "open" and "operation" in control:
                control["operation"]()
            if name == "close":
                if "cleanup" in control:
                    control["cleanup"]()
                closed.append(session.name)
            return {"success": True, "data": "https://8.8.8.8/"}

        monkeypatch.setattr(backend, "_command", command)

        def run(context):
            return backend.action(context.resource_key, "open", ["https://8.8.8.8/"], context,
                                  navigation_url="https://8.8.8.8/")
    else:
        backend = CuaCliBackend(idle_seconds=60)

        def call(name, args, context, **kwargs):
            if name == "list_apps" and "operation" in control:
                control["operation"]()
            if name == "end_session":
                if "cleanup" in control:
                    control["cleanup"]()
                closed.append(args["session"])
            return {"apps": []}

        monkeypatch.setattr(backend, "_call", call)

        def run(context):
            return backend.execute(context.resource_key, SimpleNamespace(action="list_apps"), context)
    try:
        yield backend, run, control, closed
    finally:
        control.clear()
        backend.close(deadline=time.monotonic() + 2)


def test_cleanup_cannot_close_a_running_backend_operation(backend_case, context_factory):
    backend, run, control, closed = backend_case
    entered, release = threading.Event(), threading.Event()

    def operation():
        entered.set()
        assert release.wait(3)

    control["operation"] = operation
    with context_factory() as context, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(run, context)
        try:
            assert entered.wait(1)
            backend.reap_idle(now=time.monotonic() + 2000, deadline=time.monotonic() + 1)
            assert not closed
            with pytest.raises(SessionResourcesBusyError):
                backend.close_session(context.resource_key, deadline=time.monotonic() + 0.02)
            assert not closed
        finally:
            release.set()
        pending.result(2)
        backend.close_session(context.resource_key, deadline=time.monotonic() + 1)
        assert len(closed) == 1


def test_waiting_operation_observes_cancellation(backend_case, context_factory, monkeypatch):
    backend, run, control, closed = backend_case
    cancelled, selected = threading.Event(), threading.Event()

    def check():
        if cancelled.is_set():
            raise TurnCancelled("cancelled while waiting for backend")

    with context_factory(check_active=check) as context:
        session = backend._session(context.resource_key)
        original = backend._session

        def select(key):
            value = original(key)
            selected.set()
            return value

        monkeypatch.setattr(backend, "_session", select)
        with ThreadPoolExecutor(max_workers=1) as pool:
            session.lock.acquire()
            try:
                pending = pool.submit(run, context)
                assert selected.wait(1)
                cancelled.set()
                with pytest.raises(TurnCancelled):
                    pending.result(1)
            finally:
                session.lock.release()
        assert not closed


def test_waiter_retries_if_cleanup_removed_its_cached_instance(backend_case, context_factory, monkeypatch):
    backend, run, control, closed = backend_case
    selected = threading.Event()
    with context_factory() as context:
        run(context)
        old = backend._sessions[context.resource_key]
        original = backend._session

        def select(key):
            value = original(key)
            selected.set()
            return value

        monkeypatch.setattr(backend, "_session", select)
        with ThreadPoolExecutor(max_workers=1) as pool:
            old.lock.acquire()
            try:
                pending = pool.submit(run, context)
                assert selected.wait(1)
                backend.close_session(context.resource_key, deadline=time.monotonic() + 1)
            finally:
                old.lock.release()
            pending.result(2)
        assert backend._sessions[context.resource_key] is not old
        assert len(closed) == 1


def test_reaper_rechecks_activity_after_selecting_a_candidate(backend_case, context_factory, monkeypatch):
    backend, run, control, closed = backend_case
    selected, release = threading.Event(), threading.Event()
    with context_factory() as context:
        run(context)
        session = backend._sessions[context.resource_key]
        session.last_used = time.monotonic() - 100
        original = backend.close_session

        def close(*args, **kwargs):
            if kwargs.get("idle_before") is not None:
                selected.set()
                assert release.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(backend, "close_session", close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(backend.reap_idle, now=time.monotonic(), deadline=time.monotonic() + 2)
            try:
                assert selected.wait(1)
                run(context)
            finally:
                release.set()
            pending.result(2)
        assert backend._sessions[context.resource_key] is session
        assert not closed


def test_failed_cleanup_blocks_reuse_until_retry(backend_case, context_factory):
    backend, run, control, closed = backend_case

    def fail():
        raise OSError("close failed")

    with context_factory() as context:
        run(context)
        old = backend._sessions[context.resource_key]
        control["cleanup"] = fail
        with pytest.raises(OSError):
            backend.close_session(context.resource_key, deadline=time.monotonic() + 1)
        with pytest.raises(ResourceUnavailableError):
            run(context)
        del control["cleanup"]
        backend.close_session(context.resource_key, deadline=time.monotonic() + 1)
        run(context)
        assert backend._sessions[context.resource_key] is not old
