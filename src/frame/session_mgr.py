"""Stateless session use cases; snapshots always come from the Store."""
from dataclasses import replace
from pathlib import Path
import time
import logging

from ..persistence.db_mgr import DbMgr
from .contracts import DomainError, ControlError, SessionCreate, PageQuery, HistoryQuery
from ..observability.observation_mgr import ObservationMgr


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

    @staticmethod
    def EnsurePrompt(session_id, context):
        """Initialize once; concurrent callers return the same durable winner."""
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('conversation.prompt', input={'session_id': session_id}):
            result = SessionMgr._ensure_prompt(session_id, context)
            mgr.Complete(output={'prompt': result})
            return result

    @staticmethod
    def _ensure_prompt(session_id, context):
        mgr = ObservationMgr.GetInstance()
        context.CheckCancelled()
        session = SessionMgr.Load(session_id).session
        if session.prompt is not None:
            mgr.Update(mode='reused')
            return session.prompt
        from .conf_mgr import ConfMgr
        from ..memory.memory_mgr import MemoryMgr
        from ..memory.protocols import ProfileReader
        from ..memory.prompt import Evidence
        conf = ConfMgr.GetInstance()
        prompt = conf.Serv().Options().get("system_prompt", "You are a helpful assistant.")
        mgr.Update(mode='initialized', input={'session_id': session_id, 'base_prompt': prompt})
        policy = conf.Memory().Policy("profile")
        child = replace(context, deadline=min(context.deadline or float("inf"), time.monotonic() + policy.get("timeout", 2)))
        try:
            memory: ProfileReader = MemoryMgr.GetInstance()
            profile = memory.GetProfile(child)
            child.CheckCancelled()
            if profile:
                prompt += "\n\n" + Evidence(profile)
        except DomainError as error:
            context.CheckCancelled()
            mgr.Event('profile.degraded', error_code=error.code)
            mgr.Update(mode='initialized_with_fallback')
            logging.getLogger("funagent.session").warning("Profile initialization failed (profile:%s)",
                "DEADLINE" if isinstance(error, ControlError) else "MEMORY_SOURCE",
                extra={"session_id": session_id, "run_id": context.run_id, "purpose": context.purpose})
        context.CheckCancelled()
        try:
            return SessionMgr.FreezePrompt(session_id, prompt)
        except DomainError as error:
            if error.code != "CONFLICT":
                raise
            winner = SessionMgr.Load(session_id).session.prompt
            if winner is None:
                raise
            mgr.Update(mode='concurrent_winner')
            return winner
