"""Immutable execution cursors. Copies inherited by async tasks cannot share stacks."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from threading import get_ident

from .contracts import NodeRef


def Owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return (get_ident(), id(task) if task else None)


@dataclass(frozen=True)
class Frame:
    previous: "Cursor"
    ref: NodeRef | None
    owner: tuple


@dataclass(frozen=True)
class Cursor:
    manager: object = None
    ref: NodeRef | None = None
    stack: tuple[Frame, ...] = ()
    suppressed: bool = False
    borrowed: bool = False


CURRENT = ContextVar("funagent_observation", default=Cursor())
