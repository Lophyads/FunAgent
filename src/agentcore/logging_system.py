"""Hermes-style, redacted text logging for FunAgent entry points."""
from __future__ import annotations

import atexit
import json
import logging
import queue
import re
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import portalocker
from concurrent_log_handler import ConcurrentRotatingFileHandler

from .models import AgentConfig, LoggingConfig

_LOG_FILES = {"agent": "agent.log", "errors": "errors.log", "gateway": "gateway.log"}
_LOG_FORMAT = "%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s"
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{4,}=*")
_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{4,}|gh[opusr]_[A-Za-z0-9_]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_.-]+)"
)
_ASSIGNMENT_RE = re.compile(
    r"""(?i)(["']?(?:api[_-]?key|authorization|password|passwd|secret|token|credential)["']?\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;}]+)"""
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_log_context: ContextVar[tuple[Path, str | None] | None] = ContextVar(
    "agentcore_log_context", default=None
)
_registry_lock = threading.RLock()
_runtimes: dict[Path, tuple["LoggingRuntime", int]] = {}
_root_handler: "_RoutingHandler | None" = None
_previous_handlers: list[logging.Handler] = []
_previous_root_level = logging.WARNING
_previous_factory = logging.getLogRecordFactory()
_record_factory_wrapper = None


def resolve_logging_directory(config: AgentConfig) -> Path:
    directory = config.logging.directory.expanduser()
    if directory.is_absolute():
        return directory.resolve()
    return (config.database_path.expanduser().resolve().parent / directory).resolve()


def _redact_text(value: str) -> str:
    value = _PRIVATE_KEY_RE.sub("[redacted private key]", value)
    value = _URL_CREDENTIAL_RE.sub(r"\1[redacted]@", value)
    value = _BEARER_RE.sub("Bearer [redacted]", value)
    value = _TOKEN_RE.sub("[redacted]", value)
    return _ASSIGNMENT_RE.sub(r"\1[redacted]", value)


def _session_tag(session_id: str | None) -> str:
    if not session_id:
        return ""
    # Keep the caller's ID while preventing a forged second log record.
    safe = session_id.replace("\\", "\\\\").replace("]", "\\]")
    safe = safe.replace("\r", "\\r").replace("\n", "\\n")
    return f" [{safe}]"


@contextmanager
def log_context(
    config: AgentConfig | None, session_id: str | None = None,
) -> Iterator[None]:
    """Bind file routing and a raw session tag for the current operation."""
    if config is None:
        yield
        return
    try:
        token = _log_context.set((resolve_logging_directory(config), session_id))
    except (OSError, ValueError):
        yield
        return
    try:
        yield
    finally:
        _log_context.reset(token)


class RedactingFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(_LOG_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        return _redact_text(super().format(record))


def _legacy_jsonl(path: Path) -> bool:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                return False
            return isinstance(item, dict) and item.get("schema") == 1
    return False


def _archive_legacy(directory: Path) -> None:
    """Move old JSONL files before any text writer opens the directory."""
    with portalocker.Lock(str(directory / ".format-migration.lock"), timeout=10):
        legacy = []
        for name in _LOG_FILES.values():
            for path in directory.glob(name + "*"):
                suffix = path.name[len(name):]
                if (not suffix or (suffix.startswith(".") and suffix[1:].isdigit())) and _legacy_jsonl(path):
                    legacy.append(path)
        if not legacy:
            return
        batch = directory / "legacy-jsonl" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        )
        batch.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            for source in legacy:
                target = batch / source.name
                source.replace(target)
                moved.append((source, target))
        except BaseException:
            for source, target in reversed(moved):
                try:
                    target.replace(source)
                except OSError:
                    pass
            raise


class _RaisingRotatingHandler(ConcurrentRotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[1]
        raise error if error is not None else OSError("log write failed")


class LoggingRuntime:
    """One bounded queue and writer per log directory."""

    def __init__(self, directory: Path, config: LoggingConfig, *, gateway: bool) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        _archive_legacy(directory)
        self.directory = directory
        self.config = config
        self.level = getattr(logging, config.level)
        self._gateway_users = int(gateway)
        self._queue: queue.Queue[logging.LogRecord | threading.Event] = queue.Queue(
            maxsize=config.queue_capacity
        )
        self._reserve = max(1, min(128, config.queue_capacity // 8))
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()
        self._dropped = 0
        self._write_errors = 0
        self._redaction_errors = 0
        self._last_notice = 0.0
        self._writer = threading.Thread(
            target=self._write_loop, name="agentcore-log-writer", daemon=True
        )
        self._writer.start()

    @property
    def dropped_records(self) -> int:
        with self._stats_lock:
            return self._dropped

    @property
    def write_errors(self) -> int:
        with self._stats_lock:
            return self._write_errors

    @property
    def redaction_errors(self) -> int:
        with self._stats_lock:
            return self._redaction_errors

    def enable_gateway(self) -> None:
        self._gateway_users += 1

    def disable_gateway(self) -> None:
        self._gateway_users = max(0, self._gateway_users - 1)

    def _enqueue(self, record: logging.LogRecord) -> None:
        if self._stop.is_set() or record.levelno < self.level:
            return
        if record.levelno < logging.WARNING and self._queue.qsize() >= self.config.queue_capacity - self._reserve:
            self._note_drop()
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._note_drop()

    def _note_drop(self) -> None:
        with self._stats_lock:
            self._dropped += 1
            now = time.monotonic()
            notice = now - self._last_notice >= 60
            if notice:
                self._last_notice = now
                count = self._dropped
        if notice:
            try:
                sys.stderr.write(f"agentcore: diagnostic log queue dropped {count} record(s)\n")
            except BaseException:
                pass

    def _handler_for(self, name: str) -> _RaisingRotatingHandler:
        size, backups = {
            "agent": (self.config.agent_max_size_mb, self.config.agent_backup_count),
            "errors": (self.config.errors_max_size_mb, self.config.errors_backup_count),
            "gateway": (self.config.gateway_max_size_mb, self.config.gateway_backup_count),
        }[name]
        handler = _RaisingRotatingHandler(
            self.directory / _LOG_FILES[name],
            maxBytes=size * 1024 * 1024,
            backupCount=backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def _write_loop(self) -> None:
        handlers: dict[str, _RaisingRotatingHandler] = {}
        formatter = RedactingFormatter()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                try:
                    if isinstance(item, threading.Event):
                        item.set()
                        continue
                    try:
                        line = formatter.format(item)
                    except BaseException:
                        with self._stats_lock:
                            self._redaction_errors += 1
                            self._dropped += 1
                        continue
                    destinations = ["agent"]
                    if item.levelno >= logging.WARNING:
                        destinations.append("errors")
                    if self._gateway_users and (
                        item.name.startswith(("agentcore.gateway", "agentcore.delivery"))
                        or item.getMessage().startswith(("recovery.", "runtime."))
                    ):
                        destinations.append("gateway")
                    output_record = logging.LogRecord(
                        "agentcore.file", item.levelno, "", 0, line, (), None
                    )
                    for destination in destinations:
                        try:
                            handler = handlers.get(destination)
                            if handler is None:
                                handler = self._handler_for(destination)
                                handlers[destination] = handler
                            handler.emit(output_record)
                        except BaseException:
                            with self._stats_lock:
                                self._write_errors += 1
                finally:
                    self._queue.task_done()
        finally:
            for handler in handlers.values():
                try:
                    handler.close()
                except BaseException:
                    pass

    def flush(self, timeout: float = 1.0) -> bool:
        marker = threading.Event()
        try:
            self._queue.put_nowait(marker)
        except queue.Full:
            return False
        return marker.wait(max(0.0, timeout))

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._writer.join(max(0.0, timeout))
        if self._writer.is_alive():
            try:
                sys.stderr.write(
                    f"agentcore: diagnostic log drain timed out; {self._queue.qsize()} record(s) remain queued\n"
                )
            except BaseException:
                pass
            return False
        return True


class _RoutingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with _registry_lock:
                directory = getattr(record, "log_directory", None)
                if directory is None:
                    if len(_runtimes) != 1:
                        return
                    runtime = next(iter(_runtimes.values()))[0]
                else:
                    entry = _runtimes.get(directory)
                    if entry is None:
                        return
                    runtime = entry[0]
                runtime._enqueue(record)
        except BaseException:
            # Logging must never change the result of a turn.
            pass


class LoggingHandle:
    def __init__(self, directory: Path | None, runtime: LoggingRuntime | None, *, gateway: bool = False) -> None:
        self.directory = directory
        self.runtime = runtime
        self._closed = False
        self._gateway = gateway

    def __enter__(self) -> "LoggingHandle":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self, timeout: float = 5.0) -> bool:
        if self._closed:
            return True
        self._closed = True
        if self.runtime is None or self.directory is None:
            return True
        with _registry_lock:
            current = _runtimes.get(self.directory)
            if current is None or current[0] is not self.runtime:
                return True
            if current[1] > 1:
                if self._gateway:
                    self.runtime.disable_gateway()
                _runtimes[self.directory] = (self.runtime, current[1] - 1)
                return True
            del _runtimes[self.directory]
            if not _runtimes:
                _uninstall_root()
        return self.runtime.close(timeout)


def _install_root() -> None:
    global _root_handler, _previous_handlers, _previous_root_level
    global _previous_factory, _record_factory_wrapper
    root = logging.getLogger()
    if _root_handler is None:
        _previous_handlers = root.handlers[:]
        _previous_root_level = root.level
        _previous_factory = logging.getLogRecordFactory()

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = _previous_factory(*args, **kwargs)
            context = _log_context.get()
            record.log_directory = context[0] if context else None
            record.session_tag = _session_tag(context[1]) if context else ""
            return record

        _record_factory_wrapper = factory
        logging.setLogRecordFactory(factory)
        _root_handler = _RoutingHandler()
        for handler in _previous_handlers:
            root.removeHandler(handler)
        root.addHandler(_root_handler)
    root.setLevel(min(runtime.level for runtime, _ in _runtimes.values()))


def _uninstall_root() -> None:
    global _root_handler, _record_factory_wrapper
    root = logging.getLogger()
    if _root_handler is not None:
        root.removeHandler(_root_handler)
        _root_handler.close()
        _root_handler = None
    for handler in _previous_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(_previous_root_level)
    if logging.getLogRecordFactory() is _record_factory_wrapper:
        logging.setLogRecordFactory(_previous_factory)
    _record_factory_wrapper = None


def configure_logging(config: AgentConfig, *, mode: str = "cli") -> LoggingHandle:
    if not config.logging.enabled:
        return LoggingHandle(None, None)
    try:
        directory = resolve_logging_directory(config)
    except (OSError, ValueError):
        return LoggingHandle(None, None)
    with _registry_lock:
        existing = _runtimes.get(directory)
        if existing is not None:
            runtime, references = existing
            if mode == "gateway":
                runtime.enable_gateway()
            _runtimes[directory] = (runtime, references + 1)
            _install_root()
            return LoggingHandle(directory, runtime, gateway=(mode == "gateway"))
        try:
            runtime = LoggingRuntime(directory, config.logging, gateway=(mode == "gateway"))
        except BaseException:
            try:
                sys.stderr.write("agentcore: diagnostic logging unavailable\n")
            except BaseException:
                pass
            return LoggingHandle(None, None)
        _runtimes[directory] = (runtime, 1)
        _install_root()
        return LoggingHandle(directory, runtime, gateway=(mode == "gateway"))


def active_logging_runtime(config: AgentConfig) -> LoggingRuntime | None:
    try:
        directory = resolve_logging_directory(config)
    except (OSError, ValueError):
        return None
    with _registry_lock:
        entry = _runtimes.get(directory)
        return entry[0] if entry is not None else None


def _atexit_close() -> None:
    with _registry_lock:
        runtimes = [runtime for runtime, _ in _runtimes.values()]
        _runtimes.clear()
        if _root_handler is not None:
            _uninstall_root()
    for runtime in runtimes:
        runtime.close(0.2)


atexit.register(_atexit_close)

__all__ = [
    "LoggingHandle", "LoggingRuntime", "RedactingFormatter",
    "active_logging_runtime", "configure_logging", "log_context",
    "resolve_logging_directory",
]

