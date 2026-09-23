"""Read and filter Hermes-style text diagnostic logs."""
from __future__ import annotations

import re
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, TextIO

from .logging_system import resolve_logging_directory, _session_tag
from .models import AgentConfig

LOG_FILES = {"agent": "agent.log", "errors": "errors.log", "gateway": "gateway.log"}
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_HEADER = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) "
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)"
    r"(?P<session> \[(?:\\.|[^\]])*\])? "
    r"(?P<logger>[\w.:-]+): (?P<message>.*)$"
)


def _cutoff(since: str | None) -> datetime | None:
    if since is None:
        return None
    if len(since) < 2 or not since[:-1].isdigit() or since[-1] not in "smhd":
        raise ValueError("--since must look like 30m, 1h, or 2d")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[since[-1]]
    return datetime.now().astimezone() - timedelta(**{unit: int(since[:-1])})


def _paths(config: AgentConfig, name: str) -> list[Path]:
    directory = resolve_logging_directory(config)
    filename = LOG_FILES[name]
    backups = {
        "agent": config.logging.agent_backup_count,
        "errors": config.logging.errors_backup_count,
        "gateway": config.logging.gateway_backup_count,
    }[name]
    return [directory / f"{filename}.{index}" for index in range(backups, 0, -1)] + [
        directory / filename
    ]


def _records(path: Path) -> Iterator[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            pending: list[str] = []
            for line in handle:
                if _HEADER.match(line):
                    if pending:
                        yield "".join(pending)
                    pending = [line]
                elif pending:
                    pending.append(line)
            if pending:
                yield "".join(pending)
    except FileNotFoundError:
        return


def _matches(
    record: str, *, level: str | None, session: str | None,
    component: str | None, cutoff: datetime | None,
) -> bool:
    match = _HEADER.match(record.partition("\n")[0])
    if match is None:
        return False
    if level and LEVELS[match["level"]] < LEVELS[level]:
        return False
    if session and match["session"] != _session_tag(session):
        return False
    if component:
        logger_name = match["logger"]
        message = match["message"]
        if not (
            logger_name == component
            or logger_name.startswith(component + ".")
            or logger_name.startswith("agentcore." + component)
            or message.startswith(component + ".")
        ):
            return False
    if cutoff is not None:
        stamp = datetime.strptime(match["time"], "%Y-%m-%d %H:%M:%S,%f").astimezone()
        if stamp < cutoff:
            return False
    return True



def _is_text_log(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    return _HEADER.match(line) is not None
            return True
    except FileNotFoundError:
        return False

def list_logs(config: AgentConfig, *, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    directory = resolve_logging_directory(config)
    for name in LOG_FILES:
        paths = [path for path in _paths(config, name) if _is_text_log(path)]
        size = sum(path.stat().st_size for path in paths)
        count = len(paths)
        print(f"{name}\t{count} file(s)\t{size} byte(s)\t{directory / LOG_FILES[name]}", file=output)


def tail_logs(
    config: AgentConfig, name: str = "agent", *, count: int = 100,
    follow: bool = False, level: str | None = None,
    session: str | None = None, component: str | None = None,
    since: str | None = None, output: TextIO | None = None,
) -> None:
    output = output or sys.stdout
    if name not in LOG_FILES:
        raise ValueError(f"unknown log: {name}")
    if count < 0:
        raise ValueError("-n must be nonnegative")
    if level is not None:
        level = level.upper()
        if level not in LEVELS:
            raise ValueError("--level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    filters = {
        "level": level, "session": session,
        "component": component, "cutoff": _cutoff(since),
    }
    recent: deque[str] = deque(maxlen=count or 1)
    for path in _paths(config, name):
        for record in _records(path):
            if _matches(record, **filters):
                recent.append(record)
    if count:
        for record in recent:
            output.write(record)
    output.flush()
    if follow:
        _follow(_paths(config, name)[-1], filters, output=output)


def _follow(
    path: Path, filters: dict[str, object], *, output: TextIO,
    poll_seconds: float = 0.2,
) -> None:
    # Close the handle between polls so Windows rotation can rename the file.
    identity: tuple[int, int] | None = None
    existed_at_start = path.exists()
    offset = 0
    showing = False

    def read_new(source: Path, start: int) -> tuple[int, bool]:
        nonlocal showing
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(start)
            while True:
                position = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    handle.seek(position)
                    break
                if _HEADER.match(line):
                    showing = _matches(line, **filters)
                if showing:
                    output.write(line)
                    output.flush()
            return handle.tell(), showing

    while True:
        try:
            stat = path.stat()
            current = (stat.st_dev, stat.st_ino)
            if identity is None:
                identity = current
                offset = stat.st_size if existed_at_start else 0
            elif current != identity or stat.st_size < offset:
                if current != identity:
                    backup = path.with_name(path.name + ".1")
                    try:
                        read_new(backup, offset)
                    except (FileNotFoundError, OSError):
                        pass
                identity = current
                offset = 0
                showing = False
            offset, showing = read_new(path, offset)
        except FileNotFoundError:
            pass
        time.sleep(poll_seconds)


__all__ = ["list_logs", "tail_logs"]

