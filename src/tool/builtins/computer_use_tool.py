"""The migrated text-only AX action contract."""
from ...frame.contracts import ToolDefinition
from ..registry import ToolEntry
from ..backends.handler import BackendHandler
from ..contracts import ToolDecision, ComputerReadOnly
import re

ACTIONS = ["capture", "click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key",
           "set_value", "wait", "list_apps", "list_windows", "focus_app"]


class ComputerPolicy:
    def Decide(self, call, context):
        action = call.args.get("action")
        if action == "key" and isinstance(call.args.get("keys"), str):
            aliases = {"windows": "win", "super": "win", "meta": "win", "option": "alt", "command": "cmd", "control": "ctrl"}
            tokens = {aliases.get(k, k) for k in re.split(r"\s*[+\-]\s*", call.args.get("keys", "").lower())}
            if any(set(keys) <= tokens for keys in (("win", "l"), ("ctrl", "alt", "delete"), ("ctrl", "alt", "del"), ("cmd", "shift", "q"), ("cmd", "ctrl", "q"))):
                return ToolDecision("deny")
        if action == "type" and isinstance(call.args.get("text"), str) and any(re.search(pattern, call.args["text"], re.I) for pattern in
            (r"curl\s+[^|]*\|\s*(?:ba)?sh", r"wget\s+[^|]*\|\s*(?:ba)?sh", r"\bsudo\s+rm\s+-[rf]", r"\brm\s+-rf\s+/")):
            return ToolDecision("deny")
        if ComputerReadOnly(call.args):
            return ToolDecision()
        return ToolDecision("approval", "unknown")


def register_tools(registrar):
    text = {"type": "string", "maxLength": 10000}
    positive = {"type": "integer", "minimum": 1}
    coordinate = {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}}
    properties = {"action": {"enum": ACTIONS}, "mode": {"const": "ax"},
        "app": {"type": "string", "maxLength": 200}, "pid": positive, "window_id": positive,
        "snapshot_id": {"type": "string", "maxLength": 80}, "element": positive, "coordinate": coordinate,
        "from_element": positive, "to_element": positive, "from_coordinate": coordinate, "to_coordinate": coordinate,
        "direction": {"enum": ["up", "down", "left", "right"]},
        "amount": {"type": "integer", "minimum": 1, "maximum": 50}, "text": text, "value": text,
        "keys": {"type": "string", "minLength": 1, "maxLength": 100},
        "seconds": {"type": "number", "minimum": 0, "maximum": 30},
        "delivery_mode": {"enum": ["background", "foreground"]}, "bring_to_front": {"type": "boolean"},
        "raise_window": {"type": "boolean"}, "capture_after": {"const": False}}
    variants = [{"if": {"properties": {"action": {"const": action}}}, "then": {"required": [field]}}
                for action, field in (("type", "text"), ("key", "keys"), ("set_value", "value"), ("focus_app", "app"))]
    registrar.Add(ToolEntry("computer_use", ToolDefinition("computer_use", "Text accessibility actions on a configured AX backend", {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": properties, "allOf": variants}), lambda backend: BackendHandler(backend, "computer_use", read_only=ComputerReadOnly),
        toolsets=("computer",), purposes=("main",), access={"computer.use"}, capabilities={"computer.ax"}, policy=ComputerPolicy()))
