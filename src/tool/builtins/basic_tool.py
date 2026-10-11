from ...frame.contracts import ToolDefinition
from ..registry import ToolEntry
from ..workspace import ReadWorkspaceHandler
import ast
import math
import operator
from datetime import datetime
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError
import json
from ...frame.contracts import ToolResult


def _calculate(node, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError
    if isinstance(node, ast.Expression):
        return _calculate(node.body, budget)
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        value = node.value
    elif isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
        value = (operator.pos if isinstance(node.op, ast.UAdd) else operator.neg)(_calculate(node.operand, budget))
    elif isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}:
        left, right = _calculate(node.left, budget), _calculate(node.right, budget)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError
        value = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                 ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                 ast.Mod: operator.mod, ast.Pow: operator.pow}[type(node.op)](left, right)
    else:
        raise ValueError
    if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > 1e18:
        raise ValueError
    return value


class CalculatorHandler:
    @staticmethod
    def Execute(args, context):
        context.CheckCancelled()
        try:
            value = _calculate(ast.parse(args["expression"], mode="eval"), [64])
            return ToolResult("", "ok", json.dumps({"expression": args["expression"], "value": value}))
        except (ValueError, SyntaxError, ArithmeticError, TypeError, RecursionError):
            return ToolResult("", "invalid", '{"code":"invalid_expression"}')


class CurrentTimeHandler:
    @staticmethod
    def Execute(args, context):
        context.CheckCancelled()
        try:
            zone = args.get("timezone", "UTC")
            now = datetime.now(ZoneInfo(zone))
            form = args.get("format", "iso8601")
            data = {"timezone": zone, "iso8601": now.isoformat(),
                    "value": {"iso8601": now.isoformat(), "date": now.date().isoformat(),
                              "time": now.time().isoformat()}[form]}
            return ToolResult("", "ok", json.dumps(data))
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return ToolResult("", "invalid", '{"code":"invalid_timezone_or_format"}')


class EchoHandler:
    @staticmethod
    def Execute(args, context):
        from ...frame.contracts import ToolResult
        context.CheckCancelled()
        return ToolResult("", "ok", str(args.get("value", "")))


def register_tools(registrar):
    registrar.Add(ToolEntry("current_time", ToolDefinition("current_time", "Current time in an IANA timezone", {
        "type": "object", "additionalProperties": False,
        "properties": {"timezone": {"type": "string", "minLength": 1, "maxLength": 100},
                       "format": {"enum": ["iso8601", "date", "time"]}}}),
        lambda backend: CurrentTimeHandler, toolsets=("default",), purposes=("main",)))
    registrar.Add(ToolEntry("calculator", ToolDefinition("calculator", "Evaluate bounded arithmetic", {
        "type": "object", "required": ["expression"], "additionalProperties": False,
        "properties": {"expression": {"type": "string", "minLength": 1, "maxLength": 200}}}),
        lambda backend: CalculatorHandler, toolsets=("default",), purposes=("main",)))
    registrar.Add(ToolEntry("echo", ToolDefinition("echo", "Echo a value without side effects", {
        "type": "object", "properties": {"value": {"type": "string"}}}), lambda backend: EchoHandler,
        toolsets=("demo",), purposes=("main",)))
    registrar.Add(ToolEntry("read_workspace", ToolDefinition("read_workspace", "Read a UTF-8 workspace file", {
        "type": "object", "required": ["path"], "additionalProperties": False,
        "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}}}),
        lambda backend: ReadWorkspaceHandler, toolsets=("workspace",), purposes=("main",), access={"workspace.read"}))
