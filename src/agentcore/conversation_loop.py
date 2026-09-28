from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any

from log.logging_system import log_context

from .interfaces import TurnHostContext, MemoryUpdater, ReviewScheduler, ToolGuard
from .lease import LeaseScope
from .message_history import validate_complete_history
from .session_context import SessionRuntimeSnapshot
from .turn_persistence import TurnPersistence
from .turn_preparation import PreparedConversation, prepare_memory_turn
from tools.services import ToolServices
from tools.lifecycle import SessionResourceKey
from .models import AgentConfig, Message, TurnResult, UserInput
from .provider_runtime import ApiModeRegistry, ProviderResolver
from .turn_phase import PhaseAction, TurnPhases, run_phase
from .provider_runner import RouteState
from .turn_state import LoopState, TurnRuntime


class ConversationLoop:
    """Order phase calls and route unexpected errors through one interface."""

    def __init__(
        self, *, config: AgentConfig, user_input: UserInput,
        session_id: str, turn_id: str, owner_id: str, services: ToolServices,
        cancel_event: threading.Event,
        provider_resolver: ProviderResolver,
        api_modes: ApiModeRegistry, route_states: tuple[RouteState, ...],
        guard: ToolGuard, host_context: TurnHostContext,
        memory_updater: MemoryUpdater, review_scheduler: ReviewScheduler,
        observability: Any, call_operation: Any,
        persistence: TurnPersistence | None = None,
        lease_scope: LeaseScope | None = None,
        context: SessionRuntimeSnapshot | None = None,
        history: Sequence[Message] = (),
        workspace: Path | str | None = None,
        resource_key: SessionResourceKey | None = None,
        prepare: Callable[[TurnRuntime], PreparedConversation] | None = None,
        recovery: bool = False, delivery_destination: str | None = None,
        max_recovery_attempts: int = 3,
    ) -> None:
        persistence = persistence if persistence is not None else TurnPersistence(session_id)
        if persistence.session_id != session_id:
            raise ValueError("persistence belongs to another session")
        if persistence.enabled != (lease_scope is not None):
            raise ValueError("persistent execution requires a lease; disabled writes require no lease")
        if lease_scope is not None and (
            lease_scope.session_id, lease_scope.owner_id, lease_scope.turn_id
        ) != (session_id, owner_id, turn_id):
            raise ValueError("lease scope belongs to another execution")
        if prepare is not None and resource_key is not None:
            raise ValueError("custom preparation must return its own resource_key")
        if prepare is not None and (context is not None or history):
            raise ValueError("supply either a preparation callback or context and history")
        if prepare is None and (context is None or persistence.enabled or recovery):
            raise ValueError("stored execution requires preparation; transient execution requires context")
        messages = [message.model_copy(deep=True) for message in history]
        validate_complete_history(messages)
        if prepare is None:
            assert context is not None
            prepare = partial(
                prepare_memory_turn, context=context.copy(), history=tuple(messages),
                user_input=user_input.model_copy(deep=True), workspace=workspace, resource_key=resource_key,
            )
        self._state = LoopState(session_id=session_id, turn_id=turn_id, trace_id=turn_id)
        self._services = services
        self._owner_id = owner_id
        self._started = False
        self.phases = TurnPhases(
            config=config, user_input=user_input, session_id=session_id,
            turn_id=turn_id,
            cancel_event=cancel_event, persistence=persistence,
            lease_scope=lease_scope, prepare=prepare, recovery=recovery,
            delivery_destination=delivery_destination,
            max_recovery_attempts=max_recovery_attempts,
            provider_resolver=provider_resolver, api_modes=api_modes,
            route_states=route_states, guard=guard, host_context=host_context,
            memory_updater=memory_updater,
            review_scheduler=review_scheduler, observability=observability,
            call_operation=call_operation,
        )

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(message.model_copy(deep=True) for message in self._state.messages)

    def run(self) -> TurnResult:
        if self._started:
            raise RuntimeError("a conversation loop can only run once")
        self._started = True
        phases = self.phases
        with log_context(phases.config, phases.session_id):
            budget = phases.config.timeouts.run_budget_seconds
            deadline = time.monotonic() + budget if budget is not None else None
            state = self._state
            state.started_at_monotonic = time.monotonic()
            runtime = TurnRuntime(
                config=phases.config, lease_scope=phases.lease_scope,
                owner_id=self._owner_id, turn_id=state.turn_id, services=self._services,
                observability=phases.observability,
                cancel_event=phases.cancel_event,
                deadline=deadline,
                call_operation=phases.call_operation,
            )
            try:
                phases.turn_started()
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
