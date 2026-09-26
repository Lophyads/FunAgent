"""Resource identity and diagnostic cleanup contracts shared by runtime layers."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionResourceKey:
    namespace: str
    profile: str
    session_id: str

    @property
    def backend_id(self) -> str:
        value = json.dumps((self.namespace, self.profile, self.session_id))
        return hashlib.sha256(value.encode()).hexdigest()


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
