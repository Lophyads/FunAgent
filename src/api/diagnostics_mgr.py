"""Startup binding for the operational diagnostics service."""

from ..frame.contracts import DomainError
from .protocols import OperationsDiagnostics


class DiagnosticsMgr:
    _instance: OperationsDiagnostics | None = None

    @classmethod
    def Initialize(cls, diagnostics: OperationsDiagnostics):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Diagnostics already initialized")
        cls._instance = diagnostics

    @classmethod
    def GetInstance(cls) -> OperationsDiagnostics:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Diagnostics not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        instance, cls._instance = cls._instance, None
        if instance is not None:
            instance.Close()
