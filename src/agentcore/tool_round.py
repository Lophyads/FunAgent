from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import ValidationError

from .interfaces import TurnCancelled
from .models import Message, NormalizedModelResponse
from .tool_execution import ToolExecutor
from .turn_persistence import TurnPersistence
from .turn_state import LoopState, RunBudgetExceeded, TurnRuntime


logger = logging.getLogger(__name__)


class ToolRoundRunner:
    """Stage and execute a tool-call round with optional durable writes."""

    def __init__(self, *, executor: ToolExecutor, persistence: TurnPersistence) -> None:
        self._executor = executor
        self._persistence = persistence

    def run(
        self, runtime: TurnRuntime, state: LoopState,
        response: NormalizedModelResponse,
    ) -> None:
        context = state.require_context("tool_round")
        call_ids = [call.id for call in response.tool_calls]
        logger.info(
            "tool.round_preparing phase=%s iteration=%s tool_call_count=%s "
            "tool_names=%s finish_reason=%s response_content_present=%s "
            "response_content_length=%s duplicate_call_id_count=%s",
            "tool_round", state.iteration, len(response.tool_calls),
            [call.name for call in response.tool_calls], response.finish_reason,
            response.content is not None,
            len(response.content) if response.content is not None else 0,
            len(call_ids) - len(set(call_ids)),
        )
        try:
            effects = {
                call.id: (
                    context.entries_by_name[call.name].side_effects
                    if call.name in context.entries_by_name else "possible"
                )
                for call in response.tool_calls
            }
            assistant = Message.assistant_tools(
                response.tool_calls, content=response.content,
                tool_call_effects=effects,
            )
        except ValidationError as exc:
            logger.error(
                "tool.message_validation_failed phase=%s iteration=%s "
                "tool_call_count=%s response_content_present=%s "
                "response_content_length=%s validation_errors=%s",
                "tool_round", state.iteration, len(response.tool_calls),
                response.content is not None,
                len(response.content) if response.content is not None else 0,
                _validation_error_details(exc),
            )
            raise

        try:
            self._persistence.save_tool_calls(assistant)
        except BaseException as exc:
            logger.error(
                "tool.stage_failed phase=%s iteration=%s tool_call_count=%s "
                "error_type=%s",
                "tool_round", state.iteration, len(response.tool_calls),
                type(exc).__name__,
            )
            raise

        state.messages.append(assistant)
        state.assistant_message = assistant
        round_started = time.monotonic()
        outcome_start = len(state.tool_outcomes)
        logger.info(
            "tool.round_started phase=%s iteration=%s tool_call_count=%s",
            "tool_round", state.iteration, len(response.tool_calls),
        )
        for call in response.tool_calls:
            runtime.observability.publish(
                "tool.pending", trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round", payload={"tool_name": call.name},
            )
        for tool_call in response.tool_calls:
            runtime.check_active()
            logger.debug(
                "tool.arguments_preview tool_name=%s preview=%s",
                tool_call.name,
                "[redacted]",
            )
            tool_started = time.monotonic()
            executed = self._executor.execute(
                runtime, state, tool_call,
            )
            tool_duration_ms = int((time.monotonic() - tool_started) * 1000)
            self._persistence.save_tool_result(executed.message)
            state.messages.append(executed.message)
            state.tool_outcomes.append(executed.outcome)
            tool_event = f"tool.{executed.outcome.state}"
            tool_logger = (
                logger.info if executed.outcome.state == "succeeded"
                else logger.warning
            )
            tool_logger(
                "%s phase=%s tool_name=%s effect=%s duration_ms=%s error_type=%s",
                tool_event, "tool_round", tool_call.name,
                executed.outcome.effect_disposition, tool_duration_ms,
                executed.outcome.error_type,
            )
            state.warnings.extend(runtime.observability.publish(
                tool_event, trace_id=state.trace_id,
                session_id=state.session_id, turn_id=state.turn_id,
                phase="tool_round",
                payload={
                    "tool_name": tool_call.name,
                    "effect": executed.outcome.effect_disposition,
                    "duration_ms": tool_duration_ms,
                    "error_type": executed.outcome.error_type,
                },
            ))
            if executed.interrupt_reason == "cancelled":
                raise TurnCancelled("turn cancelled during tool execution")
            if executed.interrupt_reason == "run_budget":
                raise RunBudgetExceeded("turn budget expired during tool execution")

        round_outcomes = state.tool_outcomes[outcome_start:]
        failed_count = sum(
            outcome.state != "succeeded" for outcome in round_outcomes
        )
        round_logger = logger.warning if failed_count else logger.info
        round_logger(
            "tool.round_completed phase=%s iteration=%s tool_call_count=%s "
            "failed_count=%s duration_ms=%s",
            "tool_round", state.iteration, len(round_outcomes), failed_count,
            int((time.monotonic() - round_started) * 1000),
        )


def _validation_error_details(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "type": error.get("type"),
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg"),
        }
        for error in exc.errors(include_url=False, include_input=False)
    ]


__all__ = ["ToolRoundRunner"]
