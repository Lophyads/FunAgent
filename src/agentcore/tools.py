from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, TypeVar

from pydantic import BaseModel

from .interfaces import ToolContext


ArgsT = TypeVar("ArgsT", bound=BaseModel)
ToolHandler = Callable[[ArgsT, ToolContext], Any | Awaitable[Any]]
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ToolSpec(Generic[ArgsT]):
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    requires_approval: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec[Any]] = {}
        self._frozen = False
        self._definitions: tuple[dict[str, Any], ...] = ()

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, spec: ToolSpec[Any]) -> None:
        if self._frozen:
            raise RuntimeError("tool registry is frozen")
        if not _TOOL_NAME_RE.fullmatch(spec.name):
            raise ValueError(f"invalid tool name: {spec.name!r}")
        if not spec.description.strip():
            raise ValueError("tool description must not be blank")
        if not issubclass(spec.args_model, BaseModel):
            raise TypeError("args_model must be a pydantic BaseModel subclass")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def freeze(self) -> None:
        if self._frozen:
            return
        ordered = [self._tools[name] for name in sorted(self._tools)]
        self._definitions = tuple(spec.definition() for spec in ordered)
        self._frozen = True

    def get(self, name: str) -> ToolSpec[Any] | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        if not self._frozen:
            raise RuntimeError("freeze the tool registry before reading definitions")
        # Return detached JSON-shaped values so callers cannot mutate the snapshot.
        return json.loads(json.dumps(self._definitions, ensure_ascii=False))

    async def invoke(self, spec: ToolSpec[Any], args: BaseModel, context: ToolContext) -> Any:
        result = spec.handler(args, context)
        return await result if inspect.isawaitable(result) else result


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def encode_tool_success(value: Any, max_chars: int) -> str:
    return _encode_bounded({"ok": True, "result": value}, max_chars)


def encode_tool_error(error_type: str, message: str, max_chars: int, **details: Any) -> str:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"type": error_type, "message": message},
    }
    if details:
        payload["error"]["details"] = details
    return _encode_bounded(payload, max_chars)


def _encode_bounded(payload: dict[str, Any], max_chars: int) -> str:
    rendered = _json_dumps(payload)
    if len(rendered) <= max_chars:
        return rendered

    preview_budget = max(16, max_chars - 96)
    bounded = {
        "ok": bool(payload.get("ok")),
        "truncated": True,
        "preview": rendered[:preview_budget],
    }
    encoded = _json_dumps(bounded)
    while len(encoded) > max_chars and preview_budget > 0:
        preview_budget = max(0, preview_budget - (len(encoded) - max_chars) - 1)
        bounded["preview"] = rendered[:preview_budget]
        encoded = _json_dumps(bounded)
    return encoded[:max_chars]

