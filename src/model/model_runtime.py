"""Startup binding for the shared model runner; no runtime is constructed here."""

from ..frame.contracts import DomainError
from .protocols import Runner


class ModelRuntime:
    _instance: Runner | None = None

    @classmethod
    def Initialize(cls, runner: Runner) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Model runtime already initialized")
        cls._instance = runner

    @classmethod
    def GetInstance(cls) -> Runner:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Model runtime not initialized")
        return cls._instance

    @classmethod
    def Replace(cls, runner: Runner | None):
        previous, cls._instance = cls._instance, runner
        return previous

    @classmethod
    def Close(cls) -> None:
        instance, cls._instance = cls._instance, None
        if instance is not None:
            close = getattr(instance, "Close", None)
            if close is not None:
                close()
