from __future__ import annotations

import threading
import time
from typing import Any

from log.logging_system import log_context

from .interfaces import Approver, MemoryUpdater, ReviewScheduler, ToolGuard
from .lease import LeaseHeartbeatService, TurnLeaseStore
from .models import AgentConfig, TurnResult, UserInput
from .provider_runtime import ApiModeRegistry, ProviderResolver
from .session_context import SessionBuildRequest, SessionContextFactory
from .storage import SQLiteStore
from .turn_phase import PhaseAction, RouteState, TurnPhases, run_phase
from .turn_state import LoopState, TurnRuntime


class ConversationLoop:
    """Order phase calls and route unexpected errors through one interface."""

    def __init__(
        self, *, config: AgentConfig, user_input: UserInput,
        session_id: str, turn_id: str, owner_id: str,
        session_request: SessionBuildRequest | None,
        cancel_event: threading.Event, store: SQLiteStore,
        session_contexts: SessionContextFactory, lease_store: TurnLeaseStore,
        heartbeat: LeaseHeartbeatService, provider_resolver: ProviderResolver,
        api_modes: ApiModeRegistry, route_states: tuple[RouteState, ...],
        guard: ToolGuard, approver: Approver | None,
        memory_updater: MemoryUpdater, review_scheduler: ReviewScheduler,
        observability: Any, call_operation: Any,
        recovery: bool = False, delivery_destination: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> None:
        self.phases = TurnPhases(
            config=config, user_input=user_input, session_id=session_id,
            turn_id=turn_id, owner_id=owner_id,
            session_request=session_request, cancel_event=cancel_event,
            store=store, session_contexts=session_contexts,
            lease_store=lease_store, heartbeat=heartbeat,
            provider_resolver=provider_resolver, api_modes=api_modes,
            route_states=route_states, guard=guard, approver=approver,
            memory_updater=memory_updater,
            review_scheduler=review_scheduler, observability=observability,
            call_operation=call_operation, recovery=recovery,
            delivery_destination=delivery_destination,
            max_recovery_attempts=max_recovery_attempts,
        )

    def run(self) -> TurnResult:
        phases = self.phases
        with log_context(phases.config, phases.session_id):
            budget = phases.config.timeouts.run_budget_seconds
            deadline = time.monotonic() + budget if budget is not None else None
            state = LoopState(
                session_id=phases.session_id, turn_id=phases.turn_id,
                trace_id=phases.turn_id,
            )
            runtime = TurnRuntime(
                config=phases.config, store=phases.store,
                lease_store=phases.lease_store,
                observability=phases.observability,
                cancel_event=phases.cancel_event,
                lease_lost_event=threading.Event(), deadline=deadline,
                call_operation=phases.call_operation,
            )
            try:
                phases.turn_started(state)
                for phase in phases.setup_phases():
                    action = run_phase(phase, runtime, state).action
                    if action is PhaseAction.BREAK:
                        break
                    if action is not PhaseAction.FALLTHROUGH:
                        raise RuntimeError(
                            f"{phase.name} returned unexpected action {action.value}; "
                            "expected fallthrough or break"
                        )
                else:
                    self._run_iterations(runtime, state)
            except BaseException as exc:
                phases.handle_outer_loop_error(runtime, state, exc)
            finally:
                phases.cleanup_all(runtime, state)
            return phases.to_result(state)

    def _run_iterations(self, runtime: TurnRuntime, state: LoopState) -> None:
        phases = self.phases
        for iteration in range(1, phases.config.max_iterations + 1):
            state.iteration = iteration
            state.response = None
            state.assistant_message = None
            for phase in (phases.assemble_phase(), phases.provider_phase()):
                action = run_phase(phase, runtime, state).action
                if action is PhaseAction.BREAK:
                    return
                if action is not PhaseAction.FALLTHROUGH:
                    raise RuntimeError(
                        f"{phase.name} returned unexpected action {action.value}; "
                        "expected fallthrough or break"
                    )
            phase = phases.response_phase(state)
            action = run_phase(phase, runtime, state).action
            if action is PhaseAction.CONTINUE:
                continue
            if action in {PhaseAction.BREAK, PhaseAction.RETURN}:
                return
            raise RuntimeError(
                f"{phase.name} returned unexpected action {action.value}; "
                "expected break, return, or continue"
            )
        phases.max_iterations(state)


__all__ = ["ConversationLoop"]
