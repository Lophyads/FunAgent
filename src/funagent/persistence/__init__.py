"""Persistence owns atomic operations; DbMgr exposes the Store directly."""
from typing import Protocol
from ..contracts import (DomainError, SessionCreate, Session, SessionSnapshot, PageQuery,
                         HistoryQuery, Page, TurnRequest, Task, RecordScope, Message,
                         ToolResult, RunResult)


class PersistenceStore(Protocol):
    def CreateSession(self, request: SessionCreate) -> Session: ...
    def LoadSession(self, session_id: str) -> SessionSnapshot: ...
    def ListSessions(self, query: PageQuery) -> Page: ...
    def ReadHistory(self, session_id: str, query: HistoryQuery) -> Page: ...
    def FreezePrompt(self, session_id: str, text: str) -> str: ...
    def RegisterTurn(self, request: TurnRequest) -> Task: ...
    def BeginTurn(self, task_id: str) -> SessionSnapshot: ...
    def AppendToolCalls(self, scope: RecordScope, message: Message) -> None: ...
    def AppendToolResult(self, scope: RecordScope, result: ToolResult) -> None: ...
    def FinishTurn(self, task_id: str, result: RunResult) -> Task: ...
    def GetTask(self, task_id: str) -> Task: ...
    def RepairInterrupted(self) -> int: ...
    def Close(self) -> None: ...


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
            cls._instance.Close()
            cls._instance = None


from .sqlite import SQLiteStore
