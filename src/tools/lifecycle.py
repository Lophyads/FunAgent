"""Resource identity and diagnostic cleanup contracts shared by runtime layers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionResourceKey:
    session_id: str


@dataclass(frozen=True, slots=True)
class CleanupIssue:
    resource: str
    scope: str
    operation: str
    error_code: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class CleanupReport:
    issues: tuple[CleanupIssue, ...] = ()

    @property
    def completed(self) -> bool:
        return not self.issues


class SessionResourcesBusyError(RuntimeError):
    pass


class ResourceUnavailableError(RuntimeError):
    pass


class ToolResourceProvider(Protocol):
    def close_session(self, key: SessionResourceKey, *, deadline: float) -> None: ...
    def reap_idle(self, *, now: float, deadline: float) -> None: ...
    def close(self, *, deadline: float) -> None: ...


def remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())
