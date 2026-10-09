"""Stateless session use cases; snapshots always come from the Store."""
from .persistence import DbMgr
from .contracts import PageQuery, HistoryQuery


class SessionMgr:
    @staticmethod
    def Create(request):
        return DbMgr.GetInstance().CreateSession(request)

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
