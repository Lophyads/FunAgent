"""One ReAct loop. Conversation and counters belong exclusively to each Run."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
import time

from .contracts import ModelProfile, RunEvent
from ..frame.contracts import (
    ControlError, DomainError, Message, ModelRequest,
    RunContext, RunInput, RunResult, ToolResult, RecordScope,
)
from ..tool.protocols import ToolDispatch
from ..persistence.protocols import RunJournal
from .protocols import Adapter, FallbackRule, ModelError, ModelStrategy, Provider
from .retry import Backoff, Wait
from .validation import ValidateReply
from ..frame.protocols import RunEventWriter, NullEventWriter


class Runtime:
    def __init__(self, profiles: Mapping[str, ModelProfile], providers: Mapping[str, Provider],
                 adapters: Mapping[str, type[Adapter]], *, tool_dispatch: ToolDispatch, journal: RunJournal,
                 owns_providers=True, events: RunEventWriter | None = None):
        if any(key != value.profile_id or value.provider_id not in providers or value.adapter_id not in adapters
               or not set(value.fallback_profiles) <= set(profiles)
               for key, value in profiles.items()):
            raise DomainError("CONFIG_REFERENCE", "Invalid model bindings")
        self._profiles = MappingProxyType(dict(profiles))
        self._providers = MappingProxyType(dict(providers))
        self._adapters = MappingProxyType(dict(adapters))
        self._tool_dispatch = tool_dispatch
        self._journal = journal
        self._owns_providers = owns_providers
        self._events = events if events is not None else NullEventWriter()

    def Inspect(self):
        return [{"profile_id": profile.profile_id, "provider_id": profile.provider_id,
                 "adapter_id": profile.adapter_id, "model": profile.model, "timeout": profile.timeout,
                 "streaming": profile.streaming,
                 "streaming_available": callable(getattr(self._providers[profile.provider_id], "Stream", None)),
                 "max_retries": profile.max_retries, "fallback_profiles": list(profile.fallback_profiles)}
                for profile in self._profiles.values()]

    def _emit(self, context, counts, kind, payload=None, *, iteration=None):
        if context.purpose != 'main' or not context.task_id:
            return
        try:
            self._events.Publish(RunEvent(context.task_id, context.run_id, context.purpose,
                counts['iterations'] + 1 if iteration is None else iteration,
                counts['attempts'], kind, payload or {}, session_id=context.session_id))
        except Exception:
            # Display delivery cannot change a model result or tool effect.
            pass

    def Close(self):
        if not self._owns_providers:
            return
        error = False
        seen = set()
        for provider in self._providers.values():
            if id(provider) not in seen:
                seen.add(id(provider))
                try:
                    provider.Close()
                except Exception:
                    error = True
        if error:
            raise DomainError("CLOSE_FAILED", "Provider close failed") from None

    def _send(self, request, route, active, rule, context, counts, seen):
        last = None
        for index in range(active, len(route)):
            profile = self._profiles[route[index]]
            counts["profile_id"] = profile.profile_id
            adapter = self._adapters[profile.adapter_id]
            for retry in range(profile.max_retries + 1):
                context.CheckCancelled()
                try:
                    # Re-encode every attempt, including profile/adapter changes.
                    try:
                        encoded = adapter.Encode(request, profile)
                    except (ModelError, ControlError):
                        raise
                    except Exception:
                        raise ModelError("PROTOCOL") from None
                    deadline = time.monotonic() + profile.timeout
                    if context.deadline is not None:
                        deadline = min(deadline, context.deadline)
                    attempt_context = replace(context, deadline=deadline)
                    attempt_context.CheckCancelled()
                    counts["attempts"] += 1
                    self._emit(context, counts, 'attempt_started', {'profile_id': profile.profile_id})
                    try:
                        provider = self._providers[profile.provider_id]
                        if profile.streaming:
                            if not callable(getattr(provider, "Stream", None)) or not callable(getattr(adapter, "NewStream", None)):
                                raise DomainError("UNSUPPORTED", "Selected model does not support streaming")
                            accumulator = adapter.NewStream()
                            with provider.Stream(encoded, attempt_context) as packets:
                                for packet in packets:
                                    attempt_context.CheckCancelled()
                                    text = accumulator.Add(packet)
                                    if text:
                                        self._emit(context, counts, 'text_delta', {'text': text})
                            reply = accumulator.Finish()
                        else:
                            response = provider.Send(encoded, attempt_context)
                    except (DomainError, ControlError):
                        raise
                    except Exception:
                        raise ModelError("PROVIDER_FATAL") from None
                    context.CheckCancelled()
                    attempt_context.CheckCancelled()
                    try:
                        if not profile.streaming:
                            reply = adapter.Decode(response)
                        reply = ValidateReply(reply, seen, request.tools)
                    except (ModelError, ControlError):
                        raise
                    except Exception:
                        raise ModelError("PROTOCOL") from None
                    context.CheckCancelled()
                    attempt_context.CheckCancelled()
                    self._emit(context, counts, 'attempt_accepted', {'text': reply.content or '', 'profile_id': profile.profile_id})
                    return reply, index
                except ControlError:
                    self._emit(context, counts, 'attempt_reset', {'error_code': 'TIMEOUT'})
                    context.CheckCancelled()
                    last = ModelError("TIMEOUT")
                except ModelError as error:
                    self._emit(context, counts, 'attempt_reset', {'error_code': error.code})
                    context.CheckCancelled()
                    last = error
                if last.code in {"PROTOCOL", "TRUNCATED", "PROVIDER_FATAL", "INVALID_REQUEST"}:
                    raise last
                if last.code in rule.retry_codes and retry < profile.max_retries:
                    if not Wait(Backoff(retry, last.retry_after), context):
                        raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)
                    continue
                if last.code in rule.fallback_codes:
                    break
                if last.code in rule.retry_codes:
                    raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)
                raise last
        raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)

    def Run(self, input: RunInput, strategy: type[ModelStrategy], context: RunContext) -> RunResult:
        scope = context.record_scope
        persistent = isinstance(scope, RecordScope) and scope.kind == "main_history"
        if persistent:
            if not scope.turn_id or scope.turn_id != context.task_id:
                raise DomainError("INVALID_REQUEST", "Main history requires the current Turn")
        elif scope != "ephemeral" and scope != RecordScope():
            raise DomainError("INVALID_REQUEST", "Invalid record scope")
        maximum = strategy.MaxIterations()
        allowed = strategy.ToolSet()
        rule = strategy.FallbackPolicy()
        primary = strategy.SelectModel()
        if type(maximum) is not int or maximum <= 0 or not isinstance(rule, FallbackRule):
            raise DomainError("INVALID_STRATEGY", "Invalid model strategy")
        if primary not in self._profiles or any(name not in self._profiles for name in rule.profile_ids):
            raise DomainError("CONFIG_REFERENCE", "Unknown strategy profile")
        if len(set(rule.profile_ids)) != len(rule.profile_ids) or primary in rule.profile_ids:
            raise DomainError("INVALID_STRATEGY", "Duplicate fallback profile")
        if not set(rule.profile_ids) <= set(self._profiles[primary].fallback_profiles):
            raise DomainError("CONFIG_REFERENCE", "Fallback profile not configured")
        route = (primary, *rule.profile_ids)
        dispatch = self._tool_dispatch
        from ..tool.contracts import ToolSelectionRequest
        messages = list(input.messages)
        prompt = input.system_prompt
        if prompt:
            messages.insert(0, Message("system", prompt))
        results, usage = [], {}
        counts = {"iterations": 0, "attempts": 0, "profile_id": primary}
        active = 0
        seen = {call.call_id for message in messages for call in message.calls}
        pending = ()

        def finish(status, *, final=None, reason=None, error_code=None):
            return RunResult(status, final=final, reason=reason, error_code=error_code,
                             tool_results=tuple(results), usage=usage,
                             iterations=counts["iterations"], attempts=counts["attempts"], profile_id=counts["profile_id"])

        try:
            selection = dispatch.Select(allowed if isinstance(allowed, ToolSelectionRequest)
                                        else ToolSelectionRequest(enable=allowed), context)
            definitions = selection.definitions
            for _ in range(maximum):
                context.CheckCancelled()
                reply, active = self._send(ModelRequest(tuple(messages), definitions), route, active, rule, context, counts, seen)
                counts["iterations"] += 1
                for key, value in reply.usage.items():
                    usage[key] = usage.get(key, 0) + value
                if not reply.calls:
                    return finish("completed", final=reply.content)
                safe_calls = tuple(replace(call, args={}) if call.argument_error else call for call in reply.calls)
                message = Message("assistant", reply.content or "", safe_calls)
                if persistent:
                    self._journal.AppendToolCalls(scope, message)
                messages.append(message)
                seen.update(call.call_id for call in reply.calls)
                pending = reply.calls
                for call in reply.calls:
                    context.CheckCancelled()
                    self._emit(context, counts, 'tool_started', {'call_id': call.call_id, 'tool_name': call.name},
                               iteration=counts['iterations'])
                    result = dispatch.Execute(call, selection, context)
                    results.append(result)
                    pending = pending[1:]
                    if persistent:
                        self._journal.AppendToolResult(scope, result)
                    messages.append(Message("tool", result.content, call_id=result.call_id,
                                            status=result.status, effect=result.effect))
                    self._emit(context, counts, 'tool_finished', {'call_id': result.call_id,
                        'status': result.status, 'effect': result.effect}, iteration=counts['iterations'])
                    context.CheckCancelled()
            context.CheckCancelled()
            return finish("limit", reason="MAX_ITERATIONS")
        except ControlError as error:
            results.extend(ToolResult(call.call_id, "cancelled", "Tool not executed") for call in pending)
            return finish("cancelled", reason=error.code)
        except ModelError as error:
            return finish("failed", reason=error.code, error_code=error.cause_code or error.code)
        except DomainError as error:
            results.extend(ToolResult(call.call_id, "cancelled", "Tool not executed") for call in pending)
            return finish("failed", reason=error.code, error_code=error.code)
