"""Selected backend constructors; optional dependencies remain lazy."""
from importlib import import_module
from ...frame.contracts import DomainError


BACKENDS = {"mcp": ("src.tool.backends.mcp", "MCPBackend"),
            "terminal": ("src.tool.backends.terminal", "TerminalBackend")}
BACKENDS["web"] = ("src.tool.backends.web", "WebBackend")
BACKENDS["computer_use"] = ("src.tool.backends.computer", "ComputerBackend")


def Create(settings):
    constructor = BACKENDS.get(settings["kind"])
    if constructor is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected tool backend is unavailable")
    try:
        if isinstance(constructor, tuple):
            constructor = getattr(import_module(constructor[0]), constructor[1])
        return constructor(settings)
    except DomainError:
        raise
    except ImportError:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected tool backend dependency is unavailable") from None
    except Exception:
        raise DomainError("BACKEND_FAILED", "Tool backend construction failed") from None
