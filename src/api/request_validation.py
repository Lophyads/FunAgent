"""Shared strict input parsing for user entry points."""
from ..frame.contracts import DomainError


def Fields(body, allowed, required=()):
    if not isinstance(body, dict) or body.keys() - set(allowed) or set(required) - body.keys():
        raise DomainError("INVALID_REQUEST", "Invalid fields")


def Text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DomainError("INVALID_REQUEST", "Invalid text")
    return value
