from __future__ import annotations

import ast
import math
import operator
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from .interfaces import ToolContext
from tools.tools import ToolCatalog, ToolSpec


class CalculatorArgs(BaseModel):
    expression: str = Field(min_length=1, max_length=200)


class CurrentTimeArgs(BaseModel):
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _evaluate(node: ast.AST, budget: list[int]) -> int | float:
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("expression is too complex")
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, budget)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        if not math.isfinite(float(node.value)) or abs(node.value) > 1e12:
            raise ValueError("numeric literal is out of range")
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate(node.operand, budget))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate(node.left, budget)
        right = _evaluate(node.right, budget)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("exponent is out of range")
        value = _BINARY_OPERATORS[type(node.op)](left, right)
        if type(value) not in (int, float) or not math.isfinite(float(value)) or abs(value) > 1e18:
            raise ValueError("result is out of range")
        return value
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _calculator(args: CalculatorArgs, context: ToolContext) -> dict[str, object]:
    tree = ast.parse(args.expression, mode="eval")
    value = _evaluate(tree, [64])
    return {"expression": args.expression, "value": value}


def _current_time(args: CurrentTimeArgs, context: ToolContext) -> dict[str, str]:
    try:
        zone = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {args.timezone}") from exc
    now = datetime.now(zone)
    return {"timezone": args.timezone, "iso8601": now.isoformat()}


def register_builtin_tools(catalog: ToolCatalog) -> ToolCatalog:
    catalog.register(
        ToolSpec(
            tool_id="builtin.calculator",
            implementation_version="1",
            name="calculator",
            description="Evaluate a bounded arithmetic expression using numbers and +, -, *, /, //, %, or **.",
            args_model=CalculatorArgs,
            handler=_calculator,
            side_effects="none",
        )
    )
    catalog.register(
        ToolSpec(
            tool_id="builtin.current_time",
            implementation_version="1",
            name="current_time",
            description="Return the current time in an IANA timezone such as UTC or Asia/Shanghai.",
            args_model=CurrentTimeArgs,
            handler=_current_time,
            side_effects="none",
        )
    )
    return catalog


def default_tool_catalog() -> ToolCatalog:
    return register_builtin_tools(ToolCatalog())
