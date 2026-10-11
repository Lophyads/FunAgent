"""Public diagnostics contracts and startup binding."""

from ..frame.contracts import DomainError
from .diagnostics_mgr import DiagnosticsMgr
from .protocols import OperationsDiagnostics

__all__ = ["DiagnosticsMgr", "DomainError", "OperationsDiagnostics"]
