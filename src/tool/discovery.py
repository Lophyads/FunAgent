"""Only trusted packages and explicitly named tool modules are imported."""
from importlib import import_module
from pkgutil import iter_modules
from ..frame.contracts import DomainError
from ..frame.contracts import ToolDefinition
from .contracts import RemoteToolSpec
from .registry import ToolEntry
from .policy import ActionPolicy
import re


def Discover(packages, registrar):
    for package_name in packages:
        try:
            package = import_module(package_name)
        except Exception:
            raise DomainError("CONFIG_INVALID", "Tool package import failed") from None
        for module in sorted(iter_modules(package.__path__), key=lambda m: m.name):
            if module.ispkg or not module.name.endswith("_tool"):
                continue
            try:
                loaded = import_module(package_name + "." + module.name)
            except Exception:
                raise DomainError("CONFIG_INVALID", "Tool module import failed") from None
            hook = getattr(loaded, "register_tools", None)
            if not callable(hook):
                raise DomainError("CONFIG_INVALID", "Tool module requires register_tools")
            try:
                hook(registrar)
            except DomainError:
                raise
            except Exception:
                raise DomainError("CONFIG_INVALID", "Tool registration failed") from None


def AddRemote(registrar, backend_id, specs, settings):
    from .backends.handler import BackendHandler
    include, exclude = settings.get("include"), set(settings.get("exclude", ()))
    server = settings.get("server_id", backend_id)
    for spec in specs:
        if not isinstance(spec, RemoteToolSpec) or not isinstance(spec.name, str) or not spec.name.strip():
            raise DomainError("CONFIG_INVALID", "Invalid remote tool declaration")
        if spec.name in exclude or (include is not None and spec.name not in include):
            continue
        name = "mcp_" + re.sub(r"[^A-Za-z0-9_-]", "_", server) + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", spec.name)
        registrar.Add(ToolEntry("mcp:" + backend_id + ":" + spec.name,
            ToolDefinition(name, spec.description, spec.parameters),
            lambda backend, operation=spec.name, readonly=spec.decision.effect == "none": BackendHandler(backend, operation, read_only=readonly),
            toolsets=("mcp", "mcp_" + backend_id),
            purposes=() if spec.name in settings.get("hidden", ()) else ("main",),
            policy=ActionPolicy({}, default=spec.decision), backend_id=backend_id))
