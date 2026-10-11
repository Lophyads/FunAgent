"""Construct one candidate from explicit declarations, then return it to startup."""
from ..frame.contracts import DomainError, RunContext, ToolResult
from .registry import ToolRegistrar
from .discovery import Discover, AddRemote
from .policy import RequestedNames
from .backends.factory import Create as CreateBackend
from .availability import Probe as _probe
from dataclasses import replace
from contextlib import ExitStack
import time


TOOL_PACKAGES = ("src.tool.builtins",)


def _handler(entry, backend):
    try:
        return entry.handler_factory(backend)
    except Exception:
        raise DomainError("CONFIG_INVALID", "Tool handler construction failed") from None


class _Unavailable:
    def Execute(self, args, context):
        return ToolResult("", "denied", "Tool backend not selected")


def _backend_check(backend, original, required):
    def check(context):
        availability = _probe(backend)
        return (availability.available and required <= availability.capabilities
                and (original is None or original(context) is True))
    return check


def Create(conf, own=None, *, required_backends=(), bind_backend=None):
    from .dispatch import ToolRegistry
    candidate = ToolRegistrar()
    Discover(TOOL_PACKAGES, candidate)
    entries = {entry.definition.name: entry for entry in candidate.Entries()}
    config = conf.Tool()
    backend_ids = {name: config.Tools().get(name, {}).get("backend_id", entry.backend_id) for name, entry in entries.items()}
    if any(identifier is not None and identifier not in config.Backends() for identifier in backend_ids.values()):
        raise DomainError("CONFIG_REFERENCE", "Unknown tool backend reference")
    selected = set()
    for purpose in ("main", "recall", "review"):
        selected.update(RequestedNames(config.Selection(purpose), entries, config.Toolsets(), validate_names=False))
    backends, capabilities, rollback_ids = {}, {}, set()
    with ExitStack() as rollback:
        needed = {identifier for name, identifier in backend_ids.items() if identifier is not None and name in selected}
        needed.update(required_backends)
        if not needed <= set(config.Backends()):
            raise DomainError("CONFIG_REFERENCE", "Unknown required tool backend")
        needed.update(identifier for identifier, settings in config.Backends().items() if settings.get("discover", False))
        for identifier, settings in config.Backends().items():
            if identifier not in needed:
                continue
            backend = CreateBackend(settings)
            if own is not None:
                own(backend)
            elif id(backend) not in rollback_ids and callable(getattr(backend, "Close", None)):
                rollback.callback(backend.Close)
                rollback_ids.add(id(backend))
            backends[identifier] = backend
            if not callable(getattr(backend, "Execute", None)) or not callable(getattr(backend, "Close", None)):
                raise DomainError("CONFIG_INVALID", "Invalid tool backend")
            if settings.get("discover", False):
                if not callable(getattr(backend, "ListTools", None)):
                    raise DomainError("CONFIG_INVALID", "Backend does not support tool discovery")
                context = RunContext(purpose="startup", deadline=time.monotonic() + settings.get("timeout", 10))
                try:
                    specs = backend.ListTools(context)
                    context.CheckCancelled()
                    AddRemote(candidate, identifier, specs, settings)
                except DomainError:
                    raise
                except Exception:
                    raise DomainError("CONFIG_INVALID", "Remote tool discovery failed") from None
        entries = {entry.definition.name: entry for entry in candidate.Freeze()}
        if not set(config.Tools()) <= set(entries):
            raise DomainError("CONFIG_REFERENCE", "Unknown configured tool")
        if any(entry.tool_id.startswith("mcp:") and config.Tools().get(name, {}).get("backend_id", entry.backend_id) != entry.backend_id
               for name, entry in entries.items()):
            raise DomainError("CONFIG_INVALID", "Remote tools cannot be rebound to a different backend")
        backend_ids = {name: config.Tools().get(name, {}).get("backend_id", entry.backend_id) for name, entry in entries.items()}
        definitions, handlers, bound = {}, {}, []
        for name, entry in entries.items():
            identifier = backend_ids[name]
            backend = backends.get(identifier)
            entry = replace(entry, backend_id=identifier)
            if identifier is not None:
                if backend is None:
                    entry = replace(entry, check_fn=lambda context: False)
                else:
                    availability = _probe(backend)
                    entry = replace(entry, check_fn=_backend_check(backend, entry.check_fn, entry.capabilities))
                    capabilities[name] = availability.capabilities
                handlers[name] = _handler(entry, backend) if backend is not None else _Unavailable()
            else:
                handlers[name] = _handler(entry, None)
            definitions[name] = entry.definition
            bound.append(entry)
        approval_names = tuple(name for name, settings in config.Tools().items() if settings.get("requires_approval", False))
        registry = ToolRegistry(definitions, handlers, approval_names=approval_names, entries=bound,
                                diagnostic_backends=backends, toolsets=config.Toolsets(), capabilities_by_tool=capabilities,
                                owned_backends=backends.values() if own is None else (),
                                disabled_by_purpose={purpose: config.Selection(purpose).disable
                                                     for purpose in ("main", "recall", "review")})
        for purpose in ("main", "recall", "review"):
            registry.Select(config.Selection(purpose), RunContext(purpose=purpose))
        if bind_backend is not None:
            for identifier, backend in backends.items():
                bind_backend(identifier, backend)
        rollback.pop_all()
        return registry
