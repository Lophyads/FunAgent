"""A startup-bound registry, shared by schema exposure and dispatch."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
import json
from uuid import uuid4
from threading import Lock

from ..frame.contracts import ControlError, DomainError, RunContext, ToolCall, ToolDefinition, ToolResult, thaw

from ..frame.tool_validator import ToolValidator
from ..frame.interaction_mgr import InteractionMgr
from .contracts import ToolFailure, ToolSelection, ToolSelectionRequest
from .protocols import ToolHandler
from .policy import Available, Decide, BindApproval, RequestedNames
from .tool_registrar import ToolEntry
from ..observability.observation_mgr import ObservationMgr
from ..observability.projections import Definitions, ToolOutput, ToolCall as CallView


class ToolRegistry:
    def __init__(self, definitions: Mapping[str, ToolDefinition], handlers: Mapping[str, ToolHandler], *,
                 approval_names: tuple[str, ...] = (), entries=(), capabilities=frozenset(), toolsets=None,
                 capabilities_by_tool=None, owned_backends=(), disabled_by_purpose=None, diagnostic_backends=None):
        if set(definitions) != set(handlers) or any(key != value.name for key, value in definitions.items()):
            raise DomainError("CONFIG_REFERENCE", "Invalid tool bindings")
        if any(not callable(getattr(handler, "Execute", None)) for handler in handlers.values()):
            raise DomainError("CONFIG_INVALID", "Tool handler must implement Execute")
        if len(set(approval_names)) != len(approval_names) or not set(approval_names) <= set(definitions):
            raise DomainError("CONFIG_REFERENCE", "Invalid approval tool bindings")
        self._approval_names = frozenset(approval_names)
        declared = {entry.definition.name: entry for entry in entries}
        if any(name not in definitions or entry.definition != definitions[name] for name, entry in declared.items()):
            raise DomainError("CONFIG_INVALID", "Tool declaration does not match the bound schema")
        self._entries = MappingProxyType({name: declared.get(name) or ToolEntry(name, definition, lambda b: None)
                                         for name, definition in definitions.items()})
        self._registry_id = uuid4().hex
        self._approval_lock = Lock()
        self._approval_calls = set()
        self._capabilities = frozenset(capabilities)
        self._disabled_by_purpose = MappingProxyType({purpose: frozenset(names)
            for purpose, names in (disabled_by_purpose or {}).items()})
        if any(not names <= set(definitions) for names in self._disabled_by_purpose.values()):
            raise DomainError("CONFIG_REFERENCE", "Unknown disabled tool")
        self._capabilities_by_tool = MappingProxyType({name: frozenset(caps) for name, caps in (capabilities_by_tool or {}).items()})
        self._owned_backends = tuple({id(backend): backend for backend in owned_backends}.values())
        self._closed = False
        self._diagnostic_backends = MappingProxyType(dict(diagnostic_backends or {}))
        self._toolsets = MappingProxyType({name: tuple(names) for name, names in (toolsets or {}).items()})
        if any(not set(names) <= set(definitions) for names in self._toolsets.values()):
            raise DomainError("CONFIG_REFERENCE", "Unknown toolset member")
        self._registry = MappingProxyType({key: (value, handlers[key], ToolValidator(value.parameters))
                                               for key, value in definitions.items()})
        self._purpose_validators = {(name, purpose): ToolValidator(definition.parameters)
            for name, entry in self._entries.items() for purpose, definition in entry.purpose_definitions.items()}

    def _definition(self, name, context):
        entry = self._entries[name]
        return entry.purpose_definitions.get(context.purpose, entry.definition)

    def _requested(self, request):
        return RequestedNames(request, self._entries, self._toolsets)

    def _available(self, name, context):
        if name in self._disabled_by_purpose.get(context.purpose, ()):
            return False
        return Available(self._entries[name], context, self._capabilities_by_tool.get(name, self._capabilities))

    def Inspect(self, request, context):
        context.CheckCancelled()
        requested = self._requested(request)
        def available(name):
            try:
                return self._available(name, context)
            except Exception:
                return False
        availability = {name: available(name) for name in self._entries}
        selected = {name for name in requested if availability[name]}
        return [{"name": name, "tool_id": entry.tool_id, "backend_id": entry.backend_id,
                 "description": entry.definition.description, "parameters": thaw(self._definition(name, context).parameters),
                 "toolset": ' / '.join(entry.toolsets) or 'default', "required_permissions": sorted(entry.access),
                 "requires_approval": name in self._approval_names,
                 "approval_policy": '按调用策略执行' if entry.policy is not None else ('需要审批' if name in self._approval_names else '无需审批'),
                 "side_effects": '按调用策略决定' if entry.policy is not None else '由工具实现声明',
                 "purposes": list(entry.purposes), "capabilities": sorted(entry.capabilities),
                 "requested": name in requested, "available": availability[name],
                 "selected": name in selected}
                for name, entry in self._entries.items()]

    def Connections(self):
        from .availability import Probe
        result = []
        for name, backend in self._diagnostic_backends.items():
            try:
                state = Probe(backend)
                code = state.code if state.code in {None, "UNSUPPORTED", "NOT_READY", "BACKEND_UNAVAILABLE", "DEADLINE"} else "BACKEND_UNAVAILABLE"
                result.append({"backend_id": name, "selected": True, "available": state.available,
                               "capabilities": sorted(state.capabilities), "code": code})
            except Exception:
                result.append({"backend_id": name, "selected": True, "available": False,
                               "capabilities": [], "code": "BACKEND_UNAVAILABLE"})
        return result

    def Close(self):
        self._closed = True
        with self._approval_lock:
            self._approval_calls.clear()
        failed = False
        owned, self._owned_backends = self._owned_backends, ()
        for backend in reversed(owned):
            try:
                backend.Close()
            except Exception:
                failed = True
                self._owned_backends += (backend,)
        if failed:
            raise DomainError("CLOSE_FAILED", "Tool backend close failed")

    def Select(self, request, context=None):
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('tools.select', input=lambda: dict(enable=request.enable, disable=request.disable,
                                                         toolsets=request.toolsets) if isinstance(request, ToolSelectionRequest)
                       else {'enable': request}):
            result = self._select(request, context)
            mgr.Complete(output=lambda: dict(allowed_names=result.allowed_names, definitions=Definitions(result.definitions)))
            return result

    def _select(self, request, context=None):
        if self._closed:
            raise DomainError("NOT_READY", "Tool registry closed")
        request = request if isinstance(request, ToolSelectionRequest) else ToolSelectionRequest(enable=request)
        context = context or RunContext()
        context.CheckCancelled()
        requested = self._requested(request)
        names, excluded = [], []
        for name in self._entries:
            if name in requested:
                if self._available(name, context):
                    names.append(name)
                else:
                    excluded.append({'name': name, 'reason': 'unavailable'})
        ObservationMgr.GetInstance().Update(excluded=excluded)
        return ToolSelection(tuple(self._definition(name, context) for name in names), names, self._registry_id, request)

    def Execute(self, call: ToolCall, allowed_names: tuple[str, ...], context: RunContext) -> ToolResult:
        mgr = ObservationMgr.GetInstance()
        with mgr.Scope('tool.policy_and_validation', input=lambda: dict(call=CallView(call), purpose=context.purpose,
                                                                       access=sorted(context.access))):
            decision = self._validate(call, allowed_names, context)
            if isinstance(decision, ToolResult):
                mgr.Complete(status=decision.status, output=lambda: ToolOutput(decision))
                return decision
            mgr.Complete(status=decision.outcome, output={'decision': decision.outcome})
        if decision.outcome == 'approval':
            rejection = self._approve(call, context)
            if rejection is not None:
                return rejection
        context.CheckCancelled()
        with mgr.Scope('tool.handler', input=lambda: CallView(call)):
            result = self._handle(call, context)
            mgr.Complete(status=result.status, effect=result.effect, output=lambda: ToolOutput(result))
            return result

    def _validate(self, call, allowed_names, context):
        context.CheckCancelled()
        if call.name not in self._registry:
            return ToolResult(call.call_id, "invalid", "Unknown tool")
        if self._closed:
            ObservationMgr.GetInstance().Update(denial_reason='registry_closed')
            return ToolResult(call.call_id, "denied", "Tool registry closed")
        selection = allowed_names if isinstance(allowed_names, ToolSelection) else self.Select(allowed_names, context)
        if (selection.registry_id != self._registry_id or call.name not in selection.allowed_names
                or selection.definitions != tuple(self._definition(name, context) for name in selection.allowed_names
                                                  if name in self._registry)
                or call.name not in self._requested(selection.request)):
            ObservationMgr.GetInstance().Update(denial_reason='selection_mismatch')
            return ToolResult(call.call_id, "denied", "Tool not allowed")
        if not self._available(call.name, context):
            reason = ('access_denied' if not self._entries[call.name].access <= context.access
                      else 'tool_unavailable')
            ObservationMgr.GetInstance().Update(denial_reason=reason)
            return ToolResult(call.call_id, "denied", "Tool not allowed")
        _, _, validator = self._registry[call.name]
        if call.argument_error and not call.args:
            return ToolResult(call.call_id, "invalid", '{"error":"invalid_arguments"}')
        # Deny forbidden actions before schema validation so forged review
        # mutations have the same authority result even when schema is bypassed.
        decision = Decide(self._entries[call.name], call, context, call.name in self._approval_names)
        if decision.outcome == "deny":
            ObservationMgr.GetInstance().Update(denial_reason='operation_denied')
            return ToolResult(call.call_id, "denied", "Operation not allowed")
        if call.argument_error:
            return ToolResult(call.call_id, "invalid", '{"error":"invalid_arguments"}')
        validator = self._purpose_validators.get((call.name, context.purpose), validator)
        try:
            valid = validator.is_valid(thaw(call.args))
        except Exception:
            valid = False
        if not valid:
            return ToolResult(call.call_id, "invalid", '{"error":"invalid_arguments"}')
        return decision

    def _approve(self, call, context):
        mgr = ObservationMgr.GetInstance()
        # The wait belongs to the tool call, after its policy scope has ended.
        if context.purpose != "main" or not context.task_id or not context.run_id:
            return ToolResult(call.call_id, "denied", "This run cannot request approval")
        try:
            binding = BindApproval(call, context)
            if not binding.call_id:
                return ToolResult(call.call_id, "invalid", "Approval requires a call identity")
            prompt = f"Approve {call.name} ({call.call_id}) with arguments: {json.dumps(thaw(call.args), ensure_ascii=False, allow_nan=False)}"
            key = (context.task_id, context.run_id, call.call_id)
            with self._approval_lock:
                if key in self._approval_calls:
                    return ToolResult(call.call_id, "denied", "Approval call already consumed")
                self._approval_calls.add(key)
            approved = InteractionMgr.GetInstance().Wait("approval", prompt, context, binding=binding)
        except ControlError:
            return ToolResult(call.call_id, "cancelled", "Approval interrupted")
        except DomainError as error:
            return ToolResult(call.call_id, "error", str(error))
        except (TypeError, ValueError):
            return ToolResult(call.call_id, "invalid", "Invalid approval arguments")
        if approved is not True:
            return ToolResult(call.call_id, "denied", "Approval declined")
        with mgr.Scope('tool.approval_recheck', input=lambda: CallView(call)):
            result = self._recheck(call, binding, context)
            mgr.Complete(status=result.status if result else 'allowed',
                         output=lambda: ToolOutput(result) if result else {'allowed': True})
            return result

    def _recheck(self, call, binding, context):
        try:
            unchanged = binding == BindApproval(call, context)
        except (TypeError, ValueError):
            unchanged = False
        if not unchanged:
            return ToolResult(call.call_id, 'denied', 'Approval parameters changed')
        if (not self._available(call.name, context)
                or Decide(self._entries[call.name], call, context, call.name in self._approval_names).outcome == 'deny'):
            return ToolResult(call.call_id, 'denied', 'Operation no longer allowed')

    def _handle(self, call, context):
        _, handler, _ = self._registry[call.name]
        try:
            result = handler.Execute(call.args, context)
            if not isinstance(result, ToolResult) or result.status not in {"ok", "denied", "invalid", "error", "unknown", "cancelled"}:
                raise ValueError
            if not isinstance(result.content, str) or result.effect not in {"none", "known", "unknown"}:
                raise ValueError
            return replace(result, call_id=call.call_id)
        except ToolFailure as error:
            effect = error.effect if error.effect in {"none", "known", "unknown"} else "unknown"
            return ToolResult(call.call_id, "unknown" if effect == "unknown" else "error",
                              "Tool execution failed", effect=effect)
        except ControlError:
            return ToolResult(call.call_id, "unknown", "Tool interrupted; outcome unknown", effect="unknown")
        except Exception:
            return ToolResult(call.call_id, "unknown", "Tool execution failed", effect="unknown")
