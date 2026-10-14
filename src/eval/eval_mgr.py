"""Application-owned binding for the evaluation worker."""
from ..frame.contracts import DomainError


class EvalMgr:
    _instance = None

    @classmethod
    def Initialize(cls, service):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Evaluation already initialized")
        cls._instance = service

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError("NOT_READY", "Evaluation not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        instance = cls._instance
        if instance is not None:
            try:
                instance.Stop()
            finally:
                if instance.IsStopped():
                    cls._instance = None
