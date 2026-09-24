from __future__ import annotations

import json
from typing import Any


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def encode_tool_success(value: Any, max_chars: int) -> str:
    return _encode_bounded({"ok": True, "result": value}, max_chars)


def encode_tool_error(
    error_type: str, message: str, max_chars: int, **details: Any
) -> str:
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


__all__ = ["encode_tool_error", "encode_tool_success"]
