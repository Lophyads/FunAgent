"""Main use case: prepare input, run, then commit the Turn atomically."""
from dataclasses import replace
import logging

from ..frame.conf_mgr import ConfMgr
from ..frame.contracts import (ControlError, DomainError, RecordScope,
                         RunContext, RunInput, RunResult)
from ..model.model_runtime import ModelRuntime
from ..persistence.db_mgr import DbMgr
from ..persistence.protocols import TaskStore
from ..frame.session_mgr import SessionMgr
from ..memory.contracts import MemoryPrepareRequest
from ..memory.prompt import AttachRecall, WithRecallRules
from ..memory.memory_prepare import MemoryPrepare
from ..frame.review_scheduler import ReviewScheduler
from ..frame.event_mgr import EventMgr, Emit
from ..model.contracts import RunEvent
from .main_strategy import MainStrategy
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import TaskOutput, Messages, Memory, RunOutput


logger = logging.getLogger("funagent.task")


class TaskProcess:
    @staticmethod
    def Run(task_id, context):
        store: TaskStore = DbMgr.GetInstance()
        mgr = ObservationMgr.GetInstance()
        try:
            context.CheckCancelled()
            Emit(context, 'task_started')
            Emit(context, 'phase_started', {'phase': 'prepare_conversation'})
            with mgr.Scope('conversation.prepare', input={'task_id': task_id}):
                task = store.GetTask(task_id)
                session = SessionMgr.Load(task.request.session_id).session
                context = replace(context, session_id=session.session_id, workspace=session.workspace,
                                  access=ConfMgr.GetInstance().Tool().Access(session.session_id))
                SessionMgr.EnsurePrompt(session.session_id, context)
                context.CheckCancelled()
                with mgr.Scope('journal.begin_turn', input={'task_id': task_id}):
                    snapshot = store.BeginTurn(task_id)
                    mgr.Complete(output=lambda: dict(messages=Messages(snapshot.messages), prompt=snapshot.session.prompt))
                mgr.Complete(output=lambda: dict(session_id=session.session_id, workspace=session.workspace,
                                                 prompt=snapshot.session.prompt, messages=Messages(snapshot.messages)))
            Emit(context, 'phase_finished', {'phase': 'prepare_conversation'})
            context = replace(context, record_scope=RecordScope("main_history", task_id),
                              memory_evidence=tuple(f"session:{session.session_id}/message:{m.message_id}"
                                                    for m in snapshot.messages if m.role == "user"))
            Emit(context, 'phase_started', {'phase': 'prepare_memory'})
            recalled = MemoryPrepare.Run(MemoryPrepareRequest(task.request.text), context)
            Emit(context, 'phase_finished', {'phase': 'prepare_memory'})
            with mgr.Scope('memory.attach', input=lambda: dict(user_message_id=snapshot.messages[-1].message_id,
                                                               evidence=Memory(recalled))):
                messages = AttachRecall(snapshot.messages, snapshot.messages[-1].message_id, recalled)
                mgr.Complete(output=lambda: Messages(messages))
            # BeginTurn already contains this user; only this Run sees evidence.
            result = ModelRuntime.GetInstance().Run(RunInput(messages, WithRecallRules(snapshot.session.prompt)), MainStrategy, context)
        except ControlError as error:
            result = RunResult("cancelled", reason=error.code)
        except DomainError as error:
            result = RunResult("failed", reason=error.code, error_code=error.code)
        with mgr.Scope('turn.finalize', input=lambda: RunOutput(result)):
            try:
                Emit(context, 'phase_started', {'phase': 'finalize_turn'})
                with mgr.Scope('journal.finish_turn', input=lambda: RunOutput(result)):
                    committed = store.FinishTurn(task_id, result)
                    mgr.Complete(status=committed.status, output=lambda: TaskOutput(committed))
            except DomainError as error:
                actual = store.GetTask(task_id)
                if actual.status not in {"queued", "running"}:
                    committed = actual
                    mgr.Event('turn.durable_winner', status=actual.status)
                else:
                    # Preserve known effects when the final transaction fails.
                    failure = replace(result, status="failed", final=None, reason=error.code, error_code=error.code)
                    with mgr.Scope('journal.finish_turn', input=lambda: RunOutput(failure), compensation=True):
                        committed = store.FinishTurn(task_id, failure)
                        mgr.Complete(status=committed.status, output=lambda: TaskOutput(committed))
            mgr.Complete(status=committed.status, output=lambda: TaskOutput(committed))
        mgr.EndTask(status=committed.status, output=lambda: TaskOutput(committed))
        try:
            Emit(context, 'phase_finished', {'phase': 'finalize_turn'})
            EventMgr.GetInstance().Publish(RunEvent(task_id, context.run_id, kind='task_committed',
                                                   payload={'status': committed.status}, session_id=committed.request.session_id))
        except Exception:
            pass  # The committed task is recoverable independently of display delivery.
        policy = ConfMgr.GetInstance().Memory().Policy("review")
        log_context = {"session_id": committed.request.session_id, "task_id": task_id,
                       "run_id": context.run_id, "purpose": context.purpose}
        if committed.status == "failed":
            logger.error("Task failed (%s)", committed.result.error_code or committed.result.reason,
                         extra=log_context)
        else:
            logger.info("Task %s", committed.status, extra=log_context)
        if committed.status == "completed" and policy.get("enabled", False) and policy.get("sources"):
            try:
                through = store.ObserveReviewTurn(task_id, policy.get("every_turns", 10))
                if through is not None:
                    ReviewScheduler.GetInstance().Schedule(committed.request.session_id, through)
            except DomainError as error:
                # The reply is already committed; background admission cannot
                # turn it into a failed task or rewrite its final assistant.
                logger.warning("Review scheduling failed (%s)", error.code, extra=log_context)
        return committed.result


