"""Opt-in workspace terminal tool."""

from __future__ import annotations

import subprocess

from pydantic import BaseModel, Field

from agentcore.interfaces import ToolContext
from .registry import ToolEntry, ToolRegistry


class TerminalArgs(BaseModel):
    command: str = Field(min_length=1, max_length=4_000)
    workdir: str = "."
    timeout_seconds: int = Field(default=30, ge=1, le=60)


def register_tools(registry: ToolRegistry) -> None:
    root = registry.workspace_root

    def terminal(args: TerminalArgs, context: ToolContext) -> dict:
        workdir = (root / args.workdir).resolve()
        if not workdir.is_relative_to(root) or not workdir.is_dir():
            raise ValueError("workdir must be an existing workspace directory")
        completed = subprocess.run(
            args.command,
            cwd=workdir,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout_seconds,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        return {
            "exit_code": completed.returncode,
            "stdout": stdout[:16_000],
            "stderr": stderr[:8_000],
            "truncated": len(stdout) > 16_000 or len(stderr) > 8_000,
        }

    registry.register(ToolEntry(
        tool_id="builtin.terminal", implementation_version="1",
        name="terminal",
        description=(
            "Run a shell command in a workspace directory after explicit approval."
        ),
        args_model=TerminalArgs, handler=terminal, toolset="terminal",
        check_fn=lambda: root.is_dir(),
        required_permissions=frozenset({"process.execute"}),
        requires_approval=True, side_effects="possible", timeout_seconds=65,
    ))
