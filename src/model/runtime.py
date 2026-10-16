"""One ReAct loop. Conversation and counters belong exclusively to each Run."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
import time

from .contracts import ModelProfile, RerankRequest, RerankResult, RunEvent
from ..frame.contracts import (
    ControlError, DomainError, Message, ModelRequest,
    RunContext, RunInput, RunResult, ToolResult, RecordScope,
)
from ..tool.protocols import ToolDispatch
from ..persistence.protocols import RunJournal
from .protocols import Adapter, FallbackRule, ModelError, ModelStrategy, Provider
from .retry import Backoff, Wait
from .validation import ValidateReply, ValidateRerankRequest, ValidateRerankResult
from ..frame.protocols import RunEventWriter, NullEventWriter
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import (Request, Wire, Usage, Reply, RunOutput, Messages, StreamHasOutput,
                                        ToolCall as CallView, ToolOutput)


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
                 "streaming": profile.streaming, "task": profile.task,
                 "streaming_available": callable(getattr(self._providers[profile.provider_id], "Stream", None)),
                 "max_retries": profile.max_retries, "fallback_profiles": list(profile.fallback_profiles)}
                for profile in self._profiles.values()]

    def Rank(self, request: RerankRequest, profile_id: str, context: RunContext) -> RerankResult:
        """Score in fixed batches under one deadline, returning no partial result."""
        context.CheckCancelled()
        ValidateRerankRequest(request)
        if profile_id not in self._profiles:
            raise DomainError('CONFIG_REFERENCE', 'Unknown scoring profile')
        profile = self._profiles[profile_id]
        if profile.task != 'rerank':
            raise DomainError('INVALID_REQUEST', 'Selected profile does not support scoring')
        if profile.streaming or profile.max_retries != 0 or profile.fallback_profiles:
            raise DomainError('CONFIG_INVALID', 'Scoring requires a single non-streaming route')
        deadline = time.monotonic() + profile.timeout
        if context.deadline is not None:
            deadline = min(deadline, context.deadline)
        child = replace(context, deadline=deadline)
        mgr = ObservationMgr.GetInstance()
        counts = {'attempts': 0}
        with mgr.Scope('model.rank', input=lambda: dict(question=request.question, texts=request.texts),
                       profile_id=profile.profile_id, model=profile.model, input_count=len(request.texts)):
            scores = []
            try:
                for offset in range(0, len(request.texts), 32):
                    child.CheckCancelled()
                    batch = RerankRequest(request.question, request.texts[offset:offset + 32])
                    result = self._invoke(batch, profile, child, counts,
                                          lambda result: ValidateRerankResult(result, len(batch.texts)))
                    scores.extend(result.scores)
            except ControlError:
                context.CheckCancelled()
                raise ModelError('TIMEOUT') from None
            except ModelError:
                context.CheckCancelled()
                raise
            result = RerankResult(tuple(scores), profile.profile_id, profile.model)
            mgr.Complete(output=lambda: dict(scores=result.scores), attempts=counts['attempts'])
            return result

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

    def _invoke(self, request, profile, context, counts, validate):
        """One Encode/IO/Decode attempt; routing and task validation stay with callers."""
        mgr = ObservationMgr.GetInstance()
        adapter = self._adapters[profile.adapter_id]
        context.CheckCancelled()
        # Re-encode every attempt, including profile/adapter changes.
        with mgr.Scope('model.encode', input=lambda: (Request(request) if profile.task == 'chat' else
                       dict(question=request.question, texts=request.texts)), profile_id=profile.profile_id):
            try:
                encoded = adapter.Encode(request, profile)
            except (ModelError, ControlError):
                raise
            except Exception:
                raise ModelError('PROTOCOL') from None
            mgr.Complete(output=lambda: Wire(encoded.payload))
        deadline = time.monotonic() + profile.timeout
        if context.deadline is not None:
            deadline = min(deadline, context.deadline)
        attempt_context = replace(context, deadline=deadline)
        attempt_context.CheckCancelled()
        counts["attempts"] += 1
        if profile.task == 'chat':
            self._emit(context, counts, 'attempt_started', {'profile_id': profile.profile_id})
        provider = self._providers[profile.provider_id]
        if profile.streaming:
            if not callable(getattr(provider, 'Stream', None)) or not callable(getattr(adapter, 'NewStream', None)):
                raise DomainError('UNSUPPORTED', 'Selected model does not support streaming')
            try:
                accumulator = adapter.NewStream()
            except (DomainError, ControlError):
                raise
            except Exception:
                raise ModelError('PROVIDER_FATAL') from None
        with mgr.Scope('model.attempt', kind='generation' if profile.task == 'chat' else 'rerank',
                       input=lambda: Wire(encoded.payload),
                       usage_status='unknown', accepted=False, profile_id=profile.profile_id,
                       provider_id=profile.provider_id, model=profile.model, streaming=profile.streaming,
                       attempt_id=counts['attempts']):
            try:
                if profile.streaming:
                    first_output = None
                    received_usage = None
                    unprocessed = None
                    try:
                        with provider.Stream(encoded, attempt_context) as packets:
                            for packet in packets:
                                # Capture receipt before the next control/protocol check.
                                unprocessed = packet.payload
                                if isinstance(packet.payload, Mapping) and packet.payload.get('usage') is not None:
                                    received_usage = packet.payload['usage']
                                if first_output is None and StreamHasOutput(packet.payload):
                                    first_output = time.time()
                                attempt_context.CheckCancelled()
                                text = accumulator.Add(packet)
                                unprocessed = None
                                if text:
                                    self._emit(context, counts, 'text_delta', {'text': text})
                        reply = accumulator.Finish()
                    finally:
                        snapshot = getattr(accumulator, 'Snapshot', None)
                        def partial():
                            value = snapshot() if callable(snapshot) else {'snapshot_unavailable': True}
                            if unprocessed is not None:
                                value = dict(value, received_unprocessed=Wire(unprocessed))
                            return value
                        mgr.Update(output=partial, usage=lambda: received_usage or {},
                                   **({'first_output_at': first_output} if first_output is not None else {}))
                else:
                    response = provider.Send(encoded, attempt_context)
                    mgr.Update(output=lambda: (Wire(response.payload) if profile.task == 'chat' else response.payload),
                               usage=lambda: Usage(response.payload),
                               response_received=True)
            except (DomainError, ControlError):
                raise
            except Exception:
                raise ModelError("PROVIDER_FATAL") from None
            context.CheckCancelled()
            attempt_context.CheckCancelled()
            try:
                if not profile.streaming:
                    reply = adapter.Decode(response)
                reply = validate(reply)
            except (ModelError, ControlError):
                raise
            except Exception:
                raise ModelError("PROTOCOL") from None
            context.CheckCancelled()
            attempt_context.CheckCancelled()
            mgr.Complete(status='accepted', accepted=True,
                         decoded_reply=lambda: (Reply(reply) if profile.task == 'chat' else dict(scores=reply.scores)),
                         usage=lambda: getattr(reply, 'usage', {}))
            if profile.task == 'chat':
                self._emit(context, counts, 'attempt_accepted', {'text': reply.content or '', 'profile_id': profile.profile_id})
            return reply

    def _send(self, request, route, active, rule, context, counts, seen):
        mgr = ObservationMgr.GetInstance()
        last = None
        for index in range(active, len(route)):
            profile = self._profiles[route[index]]
            counts["profile_id"] = profile.profile_id
            for retry in range(profile.max_retries + 1):
                context.CheckCancelled()
                try:
                    reply = self._invoke(request, profile, context, counts,
                                         lambda reply: ValidateReply(reply, seen, request.tools))
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
                    delay = Backoff(retry, last.retry_after)
                    with mgr.Scope('model.retry_wait', input={'seconds': delay, 'error_code': last.code}):
                        if not Wait(delay, context):
                            mgr.Complete(status='exhausted')
                            raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)
                    continue
                if last.code in rule.fallback_codes:
                    if index + 1 < len(route):
                        mgr.Event('model.fallback', from_profile=profile.profile_id,
                                  to_profile=route[index + 1], error_code=last.code)
                    else:
                        mgr.Event('model.route_exhausted', from_profile=profile.profile_id, error_code=last.code)
                    break
                if last.code in rule.retry_codes:
                    raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)
                raise last
        raise ModelError("PROFILES_EXHAUSTED", cause_code=last.code)

    def Run(self, input: RunInput, strategy: type[ModelStrategy], context: RunContext) -> RunResult:
        mgr = ObservationMgr.GetInstance()
        # Also protect callers that invoke the shared Runtime directly.
        with mgr.Suspend() if context.purpose in {'review', 'management'} else mgr.Scope(
                'agent.run', kind='agent', run_id=context.run_id, purpose=context.purpose,
                input=lambda: dict(messages=Messages(input.messages),
                                   system_prompt=input.system_prompt)):
            result = self._run(input, strategy, context)
            mgr.Complete(status=result.status, output=lambda: RunOutput(result), error_code=result.error_code)
            return result

    def _run(self, input, strategy, context):
        mgr = ObservationMgr.GetInstance()
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
        if any(self._profiles[name].task != 'chat' for name in route):
            raise DomainError('INVALID_REQUEST', 'Generation requires chat profiles')
        mgr.Update(route=route, max_iterations=maximum, strategy=strategy.__name__)
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
                with mgr.Scope('agent.iteration', iteration=counts['iterations'] + 1):
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
                        with mgr.Scope('journal.tool_calls', input=lambda: Wire(ModelRequest((message,)))):
                            self._journal.AppendToolCalls(scope, message)
                            mgr.Complete(output={'recorded': True})
                    messages.append(message)
                    seen.update(call.call_id for call in reply.calls)
                    pending = reply.calls
                    for call in reply.calls:
                        context.CheckCancelled()
                        self._emit(context, counts, 'tool_started', {'call_id': call.call_id, 'tool_name': call.name},
                                   iteration=counts['iterations'])
                        with mgr.Scope('tool.call', kind='tool', call_id=call.call_id, input=lambda: CallView(call)):
                            result = dispatch.Execute(call, selection, context)
                            results.append(result)
                            pending = pending[1:]
                            mgr.Update(output=lambda: ToolOutput(result), tool_status=result.status, effect=result.effect,
                                       result_recorded=False)
                            if persistent:
                                with mgr.Scope('journal.tool_result', input=lambda: ToolOutput(result)):
                                    self._journal.AppendToolResult(scope, result)
                                    mgr.Complete(output={'recorded': True})
                            messages.append(Message("tool", result.content, call_id=result.call_id,
                                                    status=result.status, effect=result.effect))
                            mgr.Complete(status=result.status, effect=result.effect, result_recorded=persistent)
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
