"""Startup binding for task submission, cancellation and worker shutdown."""

from ..frame.contracts import DomainError
from .protocols import TaskService


class TaskMgr:
    _instance: TaskService | None = None

    @classmethod
    def Initialize(cls, service: TaskService) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Tasks already initialized")
        cls._instance = service

    @classmethod
    def GetInstance(cls) -> TaskService:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Tasks not initialized")
        return cls._instance

    @classmethod
    def Close(cls) -> None:
        instance = cls._instance
        if instance is not None:
            try:
                instance.Stop()
            finally:
                # Storage failure after drain still releases ownership. A live
                # worker keeps its binding so Stop can be retried.
                if instance.IsStopped():
                    cls._instance = None
