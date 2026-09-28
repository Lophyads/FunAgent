"""Nine text-only agent-browser actions; no vision or image messages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agentcore.interfaces import ToolContext
from .registry import ToolEntry, ToolRegistry


class NavigateArgs(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class SnapshotArgs(BaseModel):
    full: bool = False


class RefArgs(BaseModel):
    ref: str = Field(min_length=1, max_length=80)


class TypeArgs(RefArgs):
    text: str = Field(max_length=10000)


class ScrollArgs(BaseModel):
    direction: Literal["up", "down", "left", "right"] = "down"
    pixels: int = Field(default=500, ge=1, le=3000)


class EmptyArgs(BaseModel):
    pass


class PressArgs(BaseModel):
    key: str = Field(min_length=1, max_length=80)


class ConsoleArgs(BaseModel):
    clear: bool = False


def navigate(args: NavigateArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "open", [args.url], context,
        navigation_url=args.url, snapshot=True,
    )


def snapshot(args: SnapshotArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "snapshot", [] if args.full else ["-c"],
        context,
    )


def click(args: RefArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "click", [_ref(args.ref)], context
    )


def type_text(args: TypeArgs, context: ToolContext) -> dict:
    result = context.services.browser().action(context.resource_key,
        "fill", [_ref(args.ref), args.text], context
    )
    return {
        "success": bool(result.get("success", True)),
        "url": result.get("url"),
        "element": _ref(args.ref),
        "typed": True,
    }


def scroll(args: ScrollArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "scroll",
        [args.direction, str(args.pixels)], context,
    )


def back(args: EmptyArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,"back", [], context)


def press(args: PressArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "press", [args.key], context,
    )


def get_images(args: EmptyArgs, context: ToolContext) -> dict:
    expression = (
        "JSON.stringify(Array.from(document.images).slice(0,100)"
        ".map(i=>({url:i.currentSrc||i.src,alt:i.alt})))"
    )
    return context.services.browser().action(context.resource_key,
        "eval", [expression], context,
    )


def console(args: ConsoleArgs, context: ToolContext) -> dict:
    return context.services.browser().action(context.resource_key,
        "console",
        ["--clear"] if args.clear else [], context,
    )


def available(context) -> bool:
    return context.backends.available("browser")


def register_tools(registry: ToolRegistry) -> None:
    entries = (
        ("browser_navigate", NavigateArgs, navigate, "Open a public URL and return a compact accessibility snapshot.", "possible"),
        ("browser_snapshot", SnapshotArgs, snapshot, "Read the current page accessibility snapshot and element refs.", "none"),
        ("browser_click", RefArgs, click, "Click a snapshot element ref such as @e5.", "possible"),
        ("browser_type", TypeArgs, type_text, "Fill a referenced input field; supplied text is not echoed.", "possible"),
        ("browser_scroll", ScrollArgs, scroll, "Scroll the current page.", "possible"),
        ("browser_back", EmptyArgs, back, "Go back in browser history.", "possible"),
        ("browser_press", PressArgs, press, "Press a key in the current page.", "possible"),
        ("browser_get_images", EmptyArgs, get_images, "List current page image URLs and alt text without fetching image bytes.", "none"),
        ("browser_console", ConsoleArgs, console, "Read page console messages and errors; arbitrary JavaScript is not supported.", "none"),
    )
    for name, model, handler, description, side_effects in entries:
        registry.register(ToolEntry(
            tool_id=f"builtin.{name}", implementation_version="1",
            name=name, description=description, args_model=model,
            handler=handler, toolset="browser", check_fn=available,
            side_effects=side_effects, timeout_seconds=100,
        ))


def _ref(value: str) -> str:
    if not value.lstrip("@").startswith("e"):
        raise ValueError("browser ref must be an element reference like @e5")
    return value if value.startswith("@") else "@" + value


__all__ = ["register_tools"]
