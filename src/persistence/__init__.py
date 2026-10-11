"""Persistence owns atomic operations; DbMgr exposes the Store directly."""
from ..frame.contracts import DomainError
from .protocols import PersistenceStore, SessionStore, TaskStore, RunJournal, ReviewStore


class DbMgr:
    _instance = None

    @classmethod
    def Initialize(cls, store: PersistenceStore):
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Database already initialized")
        cls._instance = store

    @classmethod
    def GetInstance(cls) -> PersistenceStore:
        if cls._instance is None:
            raise DomainError("NOT_READY", "Database not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        if cls._instance is not None:
            try:
                cls._instance.Close()
            finally:
                cls._instance = None
