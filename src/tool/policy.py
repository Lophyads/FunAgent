"""Pure selection rules shared by schema exposure and dispatch."""
from dataclasses import dataclass
from collections.abc import Mapping
from ..frame.contracts import freeze
from ..frame.contracts import ApprovalBinding, thaw
from ..frame.contracts import DomainError
import hashlib
import json
from .contracts import ToolDecision


@dataclass(frozen=True)
class ActionPolicy:
    actions: Mapping[str, ToolDecision]
    default: ToolDecision = ToolDecision("deny")

    def __post_init__(self):
        object.__setattr__(self, "actions", freeze(self.actions))

    def Decide(self, call, context):
        return self.actions.get(call.args.get("action"), self.default)


def Decide(entry, call, context, requires_approval=False):
    try:
        decision = entry.policy.Decide(call, context) if entry.policy is not None else ToolDecision()
    except Exception:
        return ToolDecision("deny")
    if (not isinstance(decision, ToolDecision) or decision.outcome not in {"allow", "deny", "approval"}
            or decision.effect not in {"none", "known", "unknown"}):
        return ToolDecision("deny")
    if requires_approval and decision.outcome == "allow":
        return ToolDecision("approval", decision.effect)
    return decision


def BindApproval(call, context):
    arguments = json.dumps(thaw(call.args), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    action = call.args.get("action", "")
    return ApprovalBinding(context.task_id, context.run_id, call.call_id, call.name,
                           action if isinstance(action, str) else "", hashlib.sha256(arguments.encode("utf8")).hexdigest())


def RequestedNames(request, entries, toolsets, *, validate_names=True):
    if any(any(not isinstance(name, str) or not name for name in values) or len(set(values)) != len(values)
           for values in (request.toolsets, request.enable, request.disable)):
        raise DomainError("CONFIG_REFERENCE", "Invalid tool selection")
    known_sets = set(toolsets) | {s for entry in entries.values() for s in entry.toolsets}
    if not set(request.toolsets) <= known_sets:
        raise DomainError("CONFIG_REFERENCE", "Unknown toolset")
    names = set(request.enable)
    for toolset in request.toolsets:
        names.update(toolsets.get(toolset, ()))
        names.update(name for name, entry in entries.items() if toolset in entry.toolsets)
    if validate_names and not (names | set(request.disable)) <= set(entries):
        raise DomainError("CONFIG_REFERENCE", "Unknown selected tool")
    return names - set(request.disable)


def Available(entry, context, capabilities):
    if context.purpose not in entry.purposes or not entry.access <= context.access:
        return False
    if not entry.capabilities <= capabilities:
        return False
    try:
        return entry.check_fn is None or entry.check_fn(context) is True
    except Exception:
        return False
