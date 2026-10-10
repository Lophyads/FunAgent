"""Stateless session use cases; snapshots always come from the Store."""
from dataclasses import replace
from pathlib import Path

from .persistence import DbMgr
from .contracts import DomainError, SessionCreate, PageQuery, HistoryQuery


class SessionMgr:
    @staticmethod
    def Create(request):
        if not isinstance(request, SessionCreate) or not isinstance(request.workspace, str) or not request.workspace.strip():
            raise DomainError("INVALID_REQUEST", "Invalid session workspace")
        try:
            workspace = Path(request.workspace).resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError
        except (OSError, ValueError, RuntimeError):
            raise DomainError("INVALID_REQUEST", "Session workspace must be an existing directory") from None
        return DbMgr.GetInstance().CreateSession(replace(request, workspace=str(workspace)))

    @staticmethod
    def Load(session_id):
        return DbMgr.GetInstance().LoadSession(session_id)

    @staticmethod
    def List(query=PageQuery()):
        return DbMgr.GetInstance().ListSessions(query)

    @staticmethod
    def ReadHistory(session_id, query=HistoryQuery()):
        return DbMgr.GetInstance().ReadHistory(session_id, query)

    @staticmethod
    def FreezePrompt(session_id, text):
        return DbMgr.GetInstance().FreezePrompt(session_id, text)
