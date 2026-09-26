"""Approval persistence and the single execution-time authorization gate."""
from __future__ import annotations

import os
import secrets
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING
import portalocker

from .interfaces import ApprovalDecision, HumanApprovalRequired, ToolInvocation
from tools.lifecycle import SessionResourceKey

if TYPE_CHECKING:
    from .interfaces import Approver
    from tools.context import ToolInteractions

class ApprovalGrantStore:
    """Session grants in RAM; permanent scope IDs in a comments-preserving TOML file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path is not None else None
        self._lock = threading.RLock()
        self._session: dict[SessionResourceKey, set[str]] = {}

    def allowed(self, session_id: SessionResourceKey, scope: str) -> bool:
        with self._lock:
            if scope in self._session.get(session_id, ()):
                return True
        path = self.path
        if path is None or not path.is_file():
            return False
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        approvals = raw.get("approvals", {})
        if not isinstance(approvals, dict):
            return False
        scopes = approvals.get("always", ())
        if not isinstance(scopes, list) or not all(
            isinstance(item, str) for item in scopes
        ):
            return False
        return scope in scopes

    def grant(self, session_id: SessionResourceKey, scope: str, duration: str,
              *, check_active: Callable[[], None] = lambda: None) -> None:
        check_active()
        if duration == "once":
            return
        if duration == "session":
            with self._lock:
                check_active()
                self._session.setdefault(session_id, set()).add(scope)
            return
        if duration != "always":
            raise ValueError("invalid approval duration")
        path = self.path
        if path is None:
            raise ValueError("permanent approval requires a TOML path")
        import tomlkit

        lock_path = path.with_name(path.name + ".approvals.lock")
        with portalocker.Lock(str(lock_path), timeout=10):
            check_active()
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            table = document.get("approvals")
            if table is None:
                table = tomlkit.table()
                document["approvals"] = table
            existing = table.get("always", [])
            if not isinstance(existing, (list, tuple)) or not all(
                isinstance(value, str) for value in existing
            ):
                raise ValueError("[approvals].always must be an array of strings")
            current = set(existing)
            current.add(scope)
            table["always"] = sorted(current)
            temp_path = path.with_name(
                f".{path.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                with temp_path.open("w", encoding="utf-8", newline="") as handle:
                    handle.write(tomlkit.dumps(document))
                    handle.flush()
                    os.fsync(handle.fileno())
                check_active()
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)

    def clear_session(self, session_id: SessionResourceKey) -> None:
        with self._lock:
            self._session.pop(session_id, None)



@dataclass(frozen=True, slots=True)
class ApprovalContext:
    key: SessionResourceKey
    interactions: ToolInteractions
    check_active: Callable[[], None]
    decide: Callable


class ApprovalGate:
    def __init__(self, grants: ApprovalGrantStore, approver: Approver | None = None):
        self.grants = grants
        self._approver = approver

    def authorize(self, invocation: ToolInvocation, context: ApprovalContext) -> ApprovalDecision:
        context.check_active()
        scopes = invocation.approval_scopes or (invocation.tool_name,)
        missing = [s for s in scopes if not self.grants.allowed(context.key, s)]
        if not missing:
            return ApprovalDecision(True)
        decision = (
            context.decide(lambda: self._approver.approve(invocation))
            if self._approver is not None else HumanApprovalRequired()
        )
        staged = []
        if isinstance(decision, ApprovalDecision):
            if not isinstance(decision.approved, bool):
                raise TypeError("approval decision must contain a boolean")
            if not decision.approved:
                return decision
            if decision.scope not in {"once", "session", "always"}:
                raise ValueError("invalid approval duration")
            staged = [(scope, decision.scope) for scope in missing]
        elif isinstance(decision, HumanApprovalRequired):
            for scope in missing:
                response = context.interactions.approve(
                    invocation.tool_name, scope, invocation.arguments,
                    permanent=self.grants.path is not None,
                )
                if response is None or response.decision == "deny":
                    return ApprovalDecision(False, "approval denied or timed out")
                staged.append((scope, response.decision))
        else:
            raise TypeError("approver must return ApprovalDecision or HumanApprovalRequired")
        context.check_active()
        for scope, duration in staged:
            context.check_active()
            self.grants.grant(context.key, scope, duration, check_active=context.check_active)
        context.check_active()
        return ApprovalDecision(True)
