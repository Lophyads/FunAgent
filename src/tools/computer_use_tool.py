"""Hermes-shaped, AX-only computer_use action tool."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentcore.interfaces import ToolContext
from .registry import ToolEntry, ToolExecutionPolicy, ToolRegistry


Action = Literal[
    "capture", "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "wait", "list_apps",
    "list_windows", "focus_app",
]

_BLOCKED_KEYS = (
    {"win", "l"}, {"ctrl", "alt", "delete"}, {"ctrl", "alt", "del"},
    {"cmd", "shift", "q"}, {"cmd", "ctrl", "q"},
    {"cmd", "shift", "backspace"},
)
_ALIASES = {
    "windows": "win", "super": "win", "meta": "win",
    "option": "alt", "command": "cmd", "control": "ctrl",
}
_BLOCKED_TEXT = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"curl\s+[^|]*\|\s*(?:ba)?sh",
    r"wget\s+[^|]*\|\s*(?:ba)?sh",
    r"\bsudo\s+rm\s+-[rf]",
    r"\brm\s+-rf\s+/",
    r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}",
))


class ComputerUseArgs(BaseModel):
    action: Action
    mode: Literal["ax"] = "ax"
    app: str | None = Field(default=None, max_length=200)
    pid: int | None = Field(default=None, ge=1)
    window_id: int | None = Field(default=None, ge=1)
    snapshot_id: str | None = Field(default=None, max_length=80)
    element: int | None = Field(default=None, ge=1)
    coordinate: tuple[int, int] | None = None
    from_element: int | None = Field(default=None, ge=1)
    to_element: int | None = Field(default=None, ge=1)
    from_coordinate: tuple[int, int] | None = None
    to_coordinate: tuple[int, int] | None = None
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=50)
    text: str | None = Field(default=None, max_length=10_000)
    keys: str | None = Field(default=None, max_length=100)
    value: str | None = Field(default=None, max_length=10_000)
    seconds: float = Field(default=1, ge=0, le=30)
    delivery_mode: Literal["background", "foreground"] = "background"
    bring_to_front: bool = False
    raise_window: bool = False
    capture_after: bool = False

    @model_validator(mode="after")
    def validate_action(self) -> "ComputerUseArgs":
        if self.capture_after:
            raise ValueError("image capture_after is unavailable in text-only mode")
        if self.bring_to_front and self.delivery_mode != "foreground":
            raise ValueError("bring_to_front requires foreground delivery")
        if self.action == "type" and self.text is None:
            raise ValueError("type requires text")
        if self.action == "key" and not self.keys:
            raise ValueError("key requires keys")
        if self.action == "set_value" and self.value is None:
            raise ValueError("set_value requires value")
        if self.action == "focus_app" and not self.app:
            raise ValueError("focus_app requires app")
        return self


def _policy(args: ComputerUseArgs) -> ToolExecutionPolicy:
    if args.action == "key":
        tokens = {
            _ALIASES.get(piece, piece)
            for piece in re.split(r"\s*[+\-]\s*", args.keys.lower())
        }
        if any(blocked.issubset(tokens) for blocked in _BLOCKED_KEYS):
            raise ValueError("dangerous system shortcut is blocked")
    if args.action == "type" and any(
        pattern.search(args.text or "") for pattern in _BLOCKED_TEXT
    ):
        raise ValueError("dangerous shell input is blocked")
    readonly = {"capture", "wait", "list_apps", "list_windows"}
    if args.action in readonly or (
        args.action == "focus_app" and not args.raise_window
    ):
        return ToolExecutionPolicy(side_effects="none")
    if args.action == "focus_app":
        return ToolExecutionPolicy(
            side_effects="possible", approval_scopes=("cua:bring_to_front",)
        )
    scopes = [f"cua:{args.action}:{args.delivery_mode}"]
    if args.bring_to_front:
        scopes.append("cua:bring_to_front")
    return ToolExecutionPolicy(
        side_effects="possible", approval_scopes=tuple(scopes)
    )


def available(context) -> bool:
    return context.backends.available("computer")


def computer_use(args: ComputerUseArgs, context: ToolContext) -> dict:
    return context.resources.computer().execute(args, context)


def register_tools(registry: ToolRegistry) -> None:
    registry.register(ToolEntry(
        tool_id="builtin.computer_use", implementation_version="1",
        name="computer_use",
        description=(
            "Control this Agent/Gateway host desktop with cua-driver. Capture "
            "returns text accessibility data only (mode=ax); image/SOM and "
            "capture_after are unavailable. Input defaults to background and "
            "needs approval. Honor each result verdict before retrying."
        ),
        args_model=ComputerUseArgs, handler=computer_use,
        toolset="computer", check_fn=available,
        policy_resolver=_policy, side_effects="possible",
        timeout_seconds=120,
    ))


__all__ = ["ComputerUseArgs", "register_tools"]
