"""Bounded reads of application-owned log paths, including rotation."""
from collections import deque
from threading import Event
import os
import time
from ..frame.contracts import DomainError

MAX_BYTES = 262144
MAX_OUTPUT = 10000


class LogReader:
    def __init__(self, files, redact):
        self._files, self._redact = dict(files), redact

    def Lines(self, *, source="normal", limit=50, contains="", follow_seconds=0, cancel=None):
        if (source not in {"normal", "error"} or type(limit) is not int or not 1 <= limit <= 1000 or
                not isinstance(contains, str) or len(contains) > 512 or
                type(follow_seconds) not in {int, float} or not 0 <= follow_seconds <= 60):
            raise DomainError("INVALID_REQUEST", "Invalid log options")
        path = self._files.get(source)
        if path is None:
            raise DomainError("UNSUPPORTED", "This application log file is not configured")
        cancel = cancel or Event()
        deadline = time.monotonic() + follow_seconds
        identity, offset, partial, count = None, 0, b"", 0
        first = True
        while not cancel.is_set():
            safe = []
            try:
                with path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    current = (stat.st_dev, stat.st_ino)
                    if first:
                        start = max(0, stat.st_size - MAX_BYTES)
                    elif current != identity or stat.st_size < offset:
                        start, partial = 0, b""
                    else:
                        start = offset
                    # If a writer outpaces us, keep the newest bounded window.
                    clipped = stat.st_size - start > MAX_BYTES
                    start = max(start, stat.st_size - MAX_BYTES)
                    stream.seek(start)
                    block = stream.read(MAX_BYTES)
                    offset, identity = stream.tell(), current
                    if clipped or first and start:
                        partial = b""
                        block = block.split(b"\n", 1)[-1] if b"\n" in block else b""
                    lines = (partial + block).split(b"\n")
                    partial = lines.pop()[-MAX_BYTES:]
                    safe = [self._redact(line.decode("utf8", errors="replace").rstrip("\r"))[:4000] for line in lines]
                    safe = [line for line in safe if contains in line]
                    if first:
                        safe = list(deque(safe, maxlen=limit))
            except FileNotFoundError:
                if first:
                    raise DomainError("NOT_FOUND", "Application log file not found") from None
                # During rollover there can briefly be no file at the live path.
            except OSError:
                raise DomainError("LOG_READ_FAILED", "Application log could not be read") from None
            # Close the read handle before yielding, so a paused consumer cannot
            # prevent Windows log rotation from replacing the live file.
            for line in safe:
                if count >= MAX_OUTPUT:
                    return
                count += 1
                yield line
            first = False
            if not follow_seconds or time.monotonic() >= deadline:
                return
            cancel.wait(min(0.05, max(0, deadline - time.monotonic())))
