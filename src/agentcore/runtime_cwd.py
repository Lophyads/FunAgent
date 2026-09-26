"""Resolve trusted workspace requests without changing the process cwd."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tools.config import WorkspaceConfig

if TYPE_CHECKING:
    from .interfaces import ToolContext


class WorkspaceValidationError(ValueError):
    pass


class SessionWorkspaceConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    cwd: Path
    allowed_root: Path


def resolve_workspace(
    request: Path | str | None,
    session_binding: ResolvedWorkspace | None,
    defaults: WorkspaceConfig,
) -> ResolvedWorkspace:
    if defaults.default_cwd is None or defaults.allowed_root is None:
        raise WorkspaceValidationError("workspace defaults must be normalized at startup")
    base = session_binding.cwd if session_binding else defaults.default_cwd
    cwd = (base / request).expanduser().resolve() if request is not None else base
    root = defaults.allowed_root
    if not cwd.is_dir():
        raise WorkspaceValidationError("workspace must be an existing directory")
    if not cwd.is_relative_to(root):
        raise WorkspaceValidationError("workspace is outside allowed_root")
    return ResolvedWorkspace(cwd, root)


def resolve_agent_cwd(context: ToolContext) -> Path:
    context.check_active()
    return context.workspace.cwd
