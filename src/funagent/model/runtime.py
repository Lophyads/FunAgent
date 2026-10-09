"""One ReAct loop. Conversation and counters belong exclusively to each Run."""
from collections.abc import Mapping
from dataclasses import replace
from math import isfinite
from types import MappingProxyType
import time

from ..conf import ModelProfile
from ..contracts import (
    ControlError, DomainError, Message, ModelReply, ModelRequest,
    RunContext, RunInput, RunResult, ToolCall, ToolResult, RecordScope,
)
from ..tool import ToolMgr
from ..persistence import DbMgr
from .protocols import Adapter, FallbackRule, ModelError, ModelStrategy, Provider


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return isfinite(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    if isinstance(value, tuple):
        return all(_json_value(item) for item in value)
    return False


def _validate_reply(reply, seen):
    if not isinstance(reply, ModelReply):
        raise ModelError("PROTOCOL")
    if reply.finish_reason == "length":
        raise ModelError("TRUNCATED")
    if reply.finish_reason not in {"stop", "tool_calls"}:
        raise ModelError("PROTOCOL")
    if not isinstance(reply.usage, Mapping) or any(
        not isinstance(key, str) or type(value) is not int or value < 0 for key, value in reply.usage.items()
    ):
        raise ModelError("PROTOCOL")
    if reply.calls:
        if reply.finish_reason != "tool_calls" or (reply.content is not None and not isinstance(reply.content, str)):
            raise ModelError("PROTOCOL")
        batch = set()
        for call in reply.calls:
            if not isinstance(call, ToolCall) or not isinstance(call.call_id, str) or not call.call_id.strip():
                raise ModelError("PROTOCOL")
            if not isinstance(call.name, str) or not call.name.strip() or not isinstance(call.args, Mapping) or not _json_value(call.args):
                raise ModelError("PROTOCOL")
            if call.call_id in seen or call.call_id in batch:
                raise ModelError("PROTOCOL")
            batch.add(call.call_id)
    elif reply.finish_reason != "stop" or not isinstance(reply.content, str) or not reply.content.strip():
        raise ModelError("PROTOCOL")


class ModelRuntime:
    _instance = None

    @classmethod
    def Initialize(cls, profiles: Mapping[str, ModelProfile], providers: Mapping[str, Provider],
                   adapters: Mapping[str, type[Adapter]]) -> None:
        if cls._instance is not None:
            raise DomainError("ALREADY_INITIALIZED", "Model runtime already initialized")
        if any(key != value.profile_id or value.provider_id not in providers or value.adapter_id not in adapters
               or not set(value.fallback_profiles) <= set(profiles)
               for key, value in profiles.items()):
            raise DomainError("CONFIG_REFERENCE", "Invalid model bindings")
        instance = cls()
        instance._profiles = MappingProxyType(dict(profiles))
        instance._providers = MappingProxyType(dict(providers))
        instance._adapters = MappingProxyType(dict(adapters))
        cls._instance = instance

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError("NOT_READY", "Model runtime not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        instance = cls._instance
        if instance is not None:
            error = False
            seen = set()
            for provider in instance._providers.values():
                if id(provider) not in seen:
                    seen.add(id(provider))
                    try:
                        provider.Close()
                    except Exception:
                        error = True
            cls._instance = None
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
                    try:
                        response = self._providers[profile.provider_id].Send(encoded, attempt_context)
                    except (ModelError, ControlError):
                        raise
                    except Exception:
                        raise ModelError("PROVIDER_FATAL") from None
                    context.CheckCancelled()
                    attempt_context.CheckCancelled()
                    try:
                        reply = adapter.Decode(response)
                        _validate_reply(reply, seen)
                    except (ModelError, ControlError):
                        raise
                    except Exception:
                        raise ModelError("PROTOCOL") from None
                    context.CheckCancelled()
                    attempt_context.CheckCancelled()
                    return reply, index
                except ControlError:
                    context.CheckCancelled()
                    last = ModelError("TIMEOUT")
                except ModelError as error:
                    context.CheckCancelled()
                    last = error
                if last.code in rule.retry_codes and retry < profile.max_retries:
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
        definitions = ToolMgr.GetInstance().Definitions(allowed)
        messages = list(input.messages)
        prompt = "\n\n".join(part for part in (input.system_prompt, *input.evidence) if part)
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
            for _ in range(maximum):
                context.CheckCancelled()
                reply, active = self._send(ModelRequest(tuple(messages), definitions), route, active, rule, context, counts, seen)
                counts["iterations"] += 1
                for key, value in reply.usage.items():
                    usage[key] = usage.get(key, 0) + value
                if not reply.calls:
                    return finish("completed", final=reply.content)
                message = Message("assistant", reply.content or "", reply.calls)
                if persistent:
                    DbMgr.GetInstance().AppendToolCalls(scope, message)
                messages.append(message)
                seen.update(call.call_id for call in reply.calls)
                pending = reply.calls
                for call in reply.calls:
                    context.CheckCancelled()
                    result = ToolMgr.GetInstance().Execute(call, allowed, context)
                    results.append(result)
                    pending = pending[1:]
                    if persistent:
                        DbMgr.GetInstance().AppendToolResult(scope, result)
                    messages.append(Message("tool", result.content, call_id=result.call_id,
                                            status=result.status, effect=result.effect))
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
