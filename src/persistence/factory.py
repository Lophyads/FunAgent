"""Main Store constructors, selected only during startup."""
from pathlib import Path

from ..frame.contracts import DomainError


def _sqlite(settings, conf_dir):
    from .sqlite import SQLiteStore
    path = Path(settings["path"])
    return SQLiteStore(path if path.is_absolute() else conf_dir / path)


STORES = {"sqlite": _sqlite}


def Create(options, conf_dir):
    selected = options.get("persistence", {"kind": "sqlite", "settings": {"path": options.get("database")}})
    factory = STORES.get(selected["kind"])
    if factory is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected persistence store is unavailable")
    return factory(selected.get("settings", {}), conf_dir)
