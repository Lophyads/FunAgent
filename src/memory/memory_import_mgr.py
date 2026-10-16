"""Startup binding for the shared import service."""
from ..frame.contracts import DomainError
from .protocols import MemoryImportAccess


class MemoryImportMgr:
    _instance: MemoryImportAccess | None = None

    @classmethod
    def Initialize(cls, service):
        if cls._instance is not None:
            raise DomainError('ALREADY_INITIALIZED', 'Import service already initialized')
        cls._instance = service

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError('NOT_READY', 'Import service not initialized')
        return cls._instance

    @classmethod
    def Close(cls):
        if cls._instance:
            cls._instance.Stop()
            cls._instance = None
