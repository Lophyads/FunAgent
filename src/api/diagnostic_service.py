"""Explicit safe projections of effective configuration and bound capabilities."""
from ..frame.contracts import DomainError, RunContext, thaw
from .diagnostics_mgr import DiagnosticsMgr
from ..model.protocols import ModelDiagnostics
from ..tool.protocols import ToolDiagnostics
from ..task.protocols import TaskDiagnostics
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

KINDS = frozenset({"tools", "connections", "models", "config", "status"})


def Sanitized(value, key=''):
    name = key.lower()
    if name in {'args', 'env', 'headers', 'system_prompt', 'credential_env', 'credential_ref'} or any(
            marker in name for marker in ('secret', 'password', 'api_key', 'authorization', 'token')):
        return '[redacted]'
    if isinstance(value, dict):
        return {k: Sanitized(v, k) for k, v in value.items() if k.lower() not in
                {'args','env','headers','system_prompt','credential_env','credential_ref','endpoint'}
                and not any(marker in k.lower() for marker in ('secret','password','api_key','authorization','token'))}
    if isinstance(value, (list, tuple)):
        return [Sanitized(v) for v in value]
    if isinstance(value, str) and (name.endswith('url') or name == 'endpoint'):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ''
            if ':' in host:
                host = '[' + host + ']'
            if parsed.port:
                host += ':' + str(parsed.port)
            return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
        except ValueError:
            return '[redacted]'
    return value


class DiagnosticService:
    def __init__(self, conf, tools: ToolDiagnostics, models: ModelDiagnostics, tasks: TaskDiagnostics, *, logs, redact):
        self._conf, self._tools, self._models, self._tasks = conf, tools, models, tasks
        self._redact = redact
        self._logs = logs
        self._closed = False

    def Read(self, kind):
        def safe(value):
            if isinstance(value, str):
                return self._redact(value)
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [safe(item) for item in value]
            return value
        return safe(self._view(kind))

    def _view(self, kind):
        if self._closed:
            raise DomainError("NOT_READY", "Diagnostics closed")
        if kind not in KINDS:
            raise DomainError("NOT_FOUND", "Diagnostic view not found")
        conf = self._conf
        if kind == "tools":
            context = RunContext(purpose="main", access=conf.Tool().Access("management"))
            return {"tools": self._tools.Inspect(conf.Tool().Selection("main"), context)}
        if kind == "connections":
            actual = {value["backend_id"]: value for value in self._tools.Connections()}
            return {"connections": [actual.get(name, {"backend_id": name, "selected": False,
                    "available": None, "capabilities": [], "code": None}) | {"kind": settings["kind"]}
                    for name, settings in conf.Tool().Backends().items()],
                    "settings": Sanitized(thaw(conf.Tool().Backends()))}
        if kind == "models":
            bound = {item['profile_id']: item for item in self._models.Inspect()}
            configured = conf.Model().Profiles()
            return {"profiles": [asdict(profile) | bound.get(name, {}) | {'selected': name in bound,
                       'base_url': Sanitized(thaw(conf.Model().Providers()[profile.provider_id])).get('base_url')}
                       for name, profile in configured.items()] if configured else list(bound.values())}
        if kind == "status":
            return {"ready": True, "mode": conf.Serv().Options()["mode"], "tasks": self._tasks.Status()}
        options = conf.Serv().Options()
        log = conf.Log().Options()
        return {"effective": Sanitized({'model.conf': {'providers': thaw(conf.Model().Providers()),
                    'adapters': list(conf.Model().Adapters()),
                    'profiles': {k: asdict(v) for k, v in conf.Model().Profiles().items()}},
                    'tool.conf': {'tools': thaw(conf.Tool().Tools()), 'toolsets': thaw(conf.Tool().Toolsets()),
                                  'backends': thaw(conf.Tool().Backends()), 'session_access': thaw(conf.Tool().AccessMap()),
                                  'purposes': {k: asdict(conf.Tool().Selection(k)) for k in ('main','recall','review')}},
                    'memory.conf': {'sources': thaw(conf.Memory().Sources()),
                                    'policies': {k: thaw(conf.Memory().Policy(k)) for k in ('profile', 'facts', 'knowledge', 'recall', 'review')}},
                    'log.conf': thaw(log), 'serv.conf': thaw(options)}),
                "serv": {key: options[key] for key in ("mode", "host", "port", "capacity", "model_profile")},
                "runtime": {"max_iterations": options.get("max_iterations", 6),
                            "task_timeout": options.get("task_timeout", 60),
                            "interaction_timeout": options.get("interaction_timeout", 120)},
                "persistence": {"kind": options.get("persistence", {}).get("kind", "sqlite")},
                "log": {"level": log["level"], "file_enabled": bool(log.get("file")),
                        "error_file_enabled": bool(log.get("error_file")),
                        "max_bytes": log.get("max_bytes", 1048576), "backup_count": log.get("backup_count", 2)},
                "memory": {"sources": [{"source_id": name, "kind": source["kind"], "store": source["store"],
                             "capabilities": list(source.get("capabilities", ("search",)))}
                            for name, source in conf.Memory().Sources().items()],
                           "policies": {name: ({"every_turns": conf.Memory().Policy(name).get("every_turns", 10)} if name == "review" else {}) |
                              {key: thaw(value) for key, value in conf.Memory().Policy(name).items()
                              if key in {"enabled", "sources", "max_records", "max_chars", "max_queries", "every_turns"}}
                              for name in ("profile", "facts", "knowledge", "review")}},
                "tools": {"purposes": {purpose: {"enable": list(conf.Tool().Selection(purpose).enable),
                          "disable": list(conf.Tool().Selection(purpose).disable),
                          "toolsets": list(conf.Tool().Selection(purpose).toolsets)}
                          for purpose in ("main", "recall", "review")}}}

    def Logs(self, **options):
        if self._closed:
            raise DomainError("NOT_READY", "Diagnostics closed")
        return self._logs.Lines(**options)

    def Close(self):
        self._closed = True


def Read(kind):
    return DiagnosticsMgr.GetInstance().Read(kind)
