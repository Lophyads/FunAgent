"""Atomic TOML loading; these snapshots never create clients or other managers."""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from threading import Lock
from types import MappingProxyType
import tomllib
from urllib.parse import urlsplit

from .contracts import DomainError, freeze
from ..observability.contracts import DEFAULTS as OBSERVABILITY_DEFAULTS
from ..tool.contracts import ToolSelectionRequest


from ..model.contracts import ModelProfile


class ModelConf(ABC):
    def Profiles(self) -> Mapping:
        return {}
    def Adapters(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.adapter_id for p in self.Profiles().values()))
    @abstractmethod
    def Profile(self, profile_id: str) -> ModelProfile: ...

    @abstractmethod
    def Providers(self) -> Mapping: ...


class ToolConf(ABC):
    def AccessMap(self) -> Mapping:
        return {}
    @abstractmethod
    def Selection(self, purpose: str) -> ToolSelectionRequest: ...

    @abstractmethod
    def Toolsets(self) -> Mapping: ...

    @abstractmethod
    def Access(self, session_id: str) -> frozenset[str]: ...

    @abstractmethod
    def Tools(self) -> Mapping: ...

    @abstractmethod
    def Backends(self) -> Mapping: ...


class MemoryConf(ABC):
    def Topics(self) -> Mapping:
        return freeze({"path": ".funagent/memory-topics.json"})

    @abstractmethod
    def Sources(self) -> Mapping: ...

    @abstractmethod
    def Policy(self, purpose: str) -> Mapping: ...


class LogConf(ABC):
    @abstractmethod
    def Options(self) -> Mapping: ...

    def Observability(self) -> Mapping:
        return freeze(OBSERVABILITY_DEFAULTS)


class ServConf(ABC):
    @abstractmethod
    def Options(self) -> Mapping: ...


class _TomlModelConf(ModelConf):
    def __init__(self, data):
        self._providers = freeze(data["providers"])
        self._adapters = tuple(data['adapters'])
        self._profiles = MappingProxyType({
            key: ModelProfile(key, value["provider_id"], value["adapter_id"],
                              value["model"], value["timeout"], value.get("max_retries", 0),
                              tuple(value.get("fallback_profiles", ())), value.get("streaming", False),
                              value.get("task", "chat"))
            for key, value in data["profiles"].items()
        })

    def Profile(self, profile_id):
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise DomainError("CONFIG_REFERENCE", "Unknown model profile") from None

    def Providers(self):
        return self._providers

    def Profiles(self):
        return self._profiles

    def Adapters(self):
        return self._adapters


class _TomlToolConf(ToolConf):
    def __init__(self, data):
        self._data = freeze(data)

    def Tools(self):
        return self._data.get("tools", MappingProxyType({}))

    def Backends(self):
        return self._data.get("backends", MappingProxyType({}))

    def Selection(self, purpose):
        value = self._data.get("purposes", {}).get(purpose, ())
        return (ToolSelectionRequest(**value) if isinstance(value, Mapping)
                else ToolSelectionRequest(enable=value))

    def Toolsets(self):
        return self._data.get("toolsets", MappingProxyType({}))

    def Access(self, session_id):
        permissions = self._data.get("session_access", {})
        return frozenset(permissions.get(session_id, permissions.get("default", ())))

    def AccessMap(self):
        return self._data.get('session_access', MappingProxyType({}))


class _TomlMemoryConf(MemoryConf):
    def __init__(self, data):
        self._data = freeze(data)

    def Sources(self):
        return self._data.get("sources", MappingProxyType({}))

    def Topics(self):
        return self._data.get("topics", super().Topics())

    def Policy(self, purpose):
        if purpose in {"profile", "facts", "knowledge", "rerank"}:
            return self._data.get("policies", {}).get(purpose, MappingProxyType({"enabled": False, "sources": ()}))
        try:
            return self._data["policies"][purpose]
        except KeyError:
            raise DomainError("CONFIG_REFERENCE", "Unknown memory policy") from None


class _TomlOptionsConf:
    def __init__(self, data):
        self._options = freeze(data["options"])

    def Options(self):
        return self._options


class _TomlLogConf(_TomlOptionsConf, LogConf):
    def __init__(self, data):
        super().__init__(data)
        self._observability = freeze(dict(OBSERVABILITY_DEFAULTS, **data.get('observability', {})))

    def Observability(self):
        return self._observability


class _TomlServConf(_TomlOptionsConf, ServConf):
    pass


def _require(condition, field, code="CONFIG_INVALID"):
    if not condition:
        raise DomainError(code, f"Invalid configuration field: {field}")


def _table(data, key, field, *, required=False):
    value = data[key] if required else data.get(key, {})
    _require(isinstance(value, dict), field)
    return value


def _names(value, field):
    _require(isinstance(value, list), field)
    _require(all(isinstance(name, str) and name for name in value), field)
    _require(len(set(value)) == len(value), field)
    return value


def _text(value, field):
    _require(isinstance(value, str) and bool(value.strip()), field)


def _number(value, field, *, minimum=0, integer=False, strict=False):
    _require(type(value) in ((int,) if integer else (int, float)), field)
    _require(isfinite(value) and (value > minimum if strict else value >= minimum), field)


def _refs(names, available, field):
    _require(all(name in available for name in names), field, "CONFIG_REFERENCE")


def _validate(docs):
    model, tool, memory, log, serv = (docs[key] for key in ("model", "tool", "memory", "log", "serv"))
    for key, allowed in (
        ("model", {"adapters", "providers", "profiles"}),
        ("tool", {"tools", "toolsets", "backends", "purposes", "session_access"}),
        ("memory", {"sources", "policies", "topics"}),
        ("log", {"options", "observability"}), ("serv", {"options"}),
    ):
        _require(set(docs[key]) <= allowed, key)
    providers = _table(model, "providers", "model.providers", required=True)
    profiles = _table(model, "profiles", "model.profiles", required=True)
    adapters = _names(model["adapters"], "model.adapters")
    _require(bool(providers) and bool(profiles) and bool(adapters), "model")
    for provider in providers.values():
        _require(isinstance(provider, dict), "model.providers")
        _text(provider["kind"], "model.providers.kind")
        if provider["kind"] == "http_json":
            _require(set(provider) <= {"kind", "endpoint", "credential_env"}, "model.providers.http_json")
            _text(provider["endpoint"], "model.providers.endpoint")
            endpoint = urlsplit(provider["endpoint"])
            _require(endpoint.scheme in {"http", "https"} and bool(endpoint.hostname) and
                     endpoint.username is None and endpoint.password is None and not endpoint.fragment,
                     "model.providers.endpoint")
            if "credential_env" in provider:
                _text(provider["credential_env"], "model.providers.credential_env")
    for profile in profiles.values():
        _require(isinstance(profile, dict), "model.profiles")
        _refs([profile["provider_id"]], providers, "model.profiles.provider_id")
        _refs([profile["adapter_id"]], adapters, "model.profiles.adapter_id")
        _text(profile["model"], "model.profiles.model")
        _number(profile["timeout"], "model.profiles.timeout", strict=True)
        _number(profile.get("max_retries", 0), "model.profiles.max_retries", integer=True)
        _require(type(profile.get("streaming", False)) is bool, "model.profiles.streaming")
        _refs(_names(profile.get("fallback_profiles", []), "model.profiles.fallback_profiles"),
              profiles, "model.profiles.fallback_profiles")
        task = profile.get("task", "chat")
        _require(task in {"chat", "rerank"}, "model.profiles.task")
        _require((task == "rerank") == (profile["adapter_id"] == "tei_rerank"), "model.profiles.adapter_id")
        if task == "rerank":
            _require(providers[profile["provider_id"]]["kind"] == "http_json", "model.profiles.provider_id")
            _require(not profile.get("streaming", False) and profile.get("max_retries", 0) == 0 and
                     not profile.get("fallback_profiles", []), "model.profiles.rerank")
        _require(all(profiles[name].get("task", "chat") == task for name in profile.get("fallback_profiles", [])),
                 "model.profiles.fallback_profiles")
    definitions = _table(tool, "tools", "tool.tools")
    backends = _table(tool, "backends", "tool.backends")
    for backend in backends.values():
        _require(isinstance(backend, dict), "tool.backends")
        _text(backend["kind"], "tool.backends.kind")
        if "timeout" in backend:
            _number(backend["timeout"], "tool.backends.timeout", strict=True)
        if "discover" in backend:
            _require(type(backend["discover"]) is bool, "tool.backends.discover")
        for field in ("include", "exclude", "hidden", "read_only"):
            if field in backend:
                _names(backend[field], "tool.backends." + field)
        if "server_id" in backend:
            _text(backend["server_id"], "tool.backends.server_id")
        for field in ("drain_timeout",):
            if field in backend:
                _number(backend[field], "tool.backends." + field, strict=True)
        for field, maximum in (("max_output", 256000), ("max_bytes", 2000000)):
            if field in backend:
                _number(backend[field], "tool.backends." + field, integer=True, strict=True)
                _require(backend[field] <= maximum, "tool.backends." + field)
        if "allow_private" in backend:
            _require(type(backend["allow_private"]) is bool, "tool.backends.allow_private")
        if "allowed_hosts" in backend:
            _names(backend["allowed_hosts"], "tool.backends.allowed_hosts")
        for field in ("endpoint", "search_endpoint", "credential_env", "cwd"):
            if field in backend:
                _text(backend[field], "tool.backends." + field)
        if backend["kind"] == "mcp":
            transport = backend.get("transport", "stdio")
            _require(transport in {"stdio", "streamable_http", "sse"}, "tool.backends.transport")
            if transport == "stdio":
                _require(("command" in backend) != ("command_env" in backend), "tool.backends.command")
                _text(backend.get("command", backend.get("command_env")), "tool.backends.command")
                _require(isinstance(backend.get("args", []), list) and
                         all(isinstance(a, str) for a in backend.get("args", [])), "tool.backends.args")
                if "args_env" in backend:
                    _require("args" not in backend, "tool.backends.args")
                    _names(backend["args_env"], "tool.backends.args_env")
            else:
                _text(backend["endpoint"], "tool.backends.endpoint")
            if "env" in backend:
                _require(isinstance(backend["env"], dict), "tool.backends.env")
                for key, variable in backend["env"].items():
                    _text(key, "tool.backends.env")
                    _text(variable, "tool.backends.env")
        if backend["kind"] == "computer_use":
            _text(backend["endpoint"], "tool.backends.endpoint")
        if backend["kind"] == "treg_web":
            _require(not backend.get("discover", False), "tool.backends.discover")
            _require(not backend.get("allow_private", False), "tool.backends.allow_private")
            _require(backend.get("endpoint", "https://treg.to/mcp/") == "https://treg.to/mcp/",
                     "tool.backends.endpoint")
    for definition in definitions.values():
        _require(isinstance(definition, dict) and set(definition) <= {"requires_approval", "backend_id"}, "tool.tools")
        if "requires_approval" in definition:
            _require(type(definition["requires_approval"]) is bool, "tool.definitions.requires_approval")
        if "backend_id" in definition:
            _refs([definition["backend_id"]], backends, "tool.definitions.backend_id")
    for names in _table(tool, "purposes", "tool.purposes").values():
        if isinstance(names, dict):
            _require(set(names) <= {"toolsets", "enable", "disable"}, "tool.purposes")
            for values in names.values():
                _names(values, "tool.purposes")
        else:
            _names(names, "tool.purposes")
    for names in _table(tool, "toolsets", "tool.toolsets").values():
        _names(names, "tool.toolsets")
    for names in _table(tool, "session_access", "tool.session_access").values():
        _names(names, "tool.session_access")
    topics = _table(memory, "topics", "memory.topics")
    _require(set(topics) <= {"path"}, "memory.topics")
    if "path" in topics:
        _text(topics["path"], "memory.topics.path")
    sources = _table(memory, "sources", "memory.sources")
    for source in sources.values():
        _require(isinstance(source, dict), "memory.sources")
        _require(source["kind"] in {"semantic", "episodic"}, "memory.sources.kind")
        _text(source["store"], "memory.sources.store")
        capabilities = _names(source.get("capabilities", ["search"]), "memory.sources.capabilities")
        _require("search" in capabilities and set(capabilities) <= {"search", "list", "write", "update", "delete"}, "memory.sources.capabilities")
        for key in ("read_access", "write_access"):
            if key in source:
                _text(source[key], f"memory.sources.{key}")
        if source["store"] == "sqlite":
            _require(source["kind"] == "episodic", "memory.sources.kind")
            _text(source["path"], "memory.sources.path")
        if source["store"] == "cognee":
            from ..memory.cognee import ValidateSettings
            ValidateSettings(source)
        if source["store"] == "supabase":
            _require(source["kind"] == "episodic", "memory.sources.kind")
            for field in ("namespace", "endpoint"):
                _text(source[field], "memory.sources." + field)
            for field in ("credential_env", "api_key_env"):
                if field in source:
                    _text(source[field], "memory.sources." + field)
            if "similarity_threshold" in source:
                threshold = source["similarity_threshold"]
                _require(type(threshold) in (int, float) and isfinite(threshold) and -1 <= threshold <= 1,
                         "memory.sources.similarity_threshold")
            embedding = _table(source, "embedding", "memory.sources.embedding", required=True)
            _require(set(embedding) <= {"kind", "endpoint", "credential_env", "model", "dimensions",
                                      "query_prefix", "document_prefix", "timeout", "request_dimensions"}, "memory.sources.embedding")
            for field in ("kind", "endpoint", "model"):
                _text(embedding[field], "memory.sources.embedding." + field)
            if "credential_env" in embedding:
                _text(embedding["credential_env"], "memory.sources.embedding.credential_env")
            _number(embedding["dimensions"], "memory.sources.embedding.dimensions", integer=True, strict=True)
            _require(embedding["dimensions"] <= 4096, "memory.sources.embedding.dimensions")
            for field in ("query_prefix", "document_prefix"):
                _require(isinstance(embedding.get(field, ""), str) and len(embedding.get(field, "")) <= 2000,
                         "memory.sources.embedding." + field)
            if "timeout" in embedding:
                _number(embedding["timeout"], "memory.sources.embedding.timeout", strict=True)
            if "request_dimensions" in embedding:
                _require(type(embedding["request_dimensions"]) is bool, "memory.sources.embedding.request_dimensions")
        if source["store"] == "callback":
            _require(source["kind"] == "semantic" and capabilities == ["search"], "memory.sources.capabilities")
            _text(source["callback_id"], "memory.sources.callback_id")
            if source["callback_id"] == "http_json":
                _text(source["endpoint"], "memory.sources.endpoint")
                if "credential_env" in source:
                    _text(source["credential_env"], "memory.sources.credential_env")
            if source["callback_id"] == "mcp":
                _refs([source["backend_id"]], backends, "memory.sources.backend_id")
                selected = backends[source["backend_id"]]
                _require(selected["kind"] == "mcp" and selected.get("discover", False), "memory.sources.backend_id")
                _require("query_knowledge_hub" not in selected.get("exclude", ()) and
                         ("include" not in selected or "query_knowledge_hub" in selected["include"]), "memory.sources.backend_id")
                _text(source["collection"], "memory.sources.collection")
                _number(source.get("top_k", 5), "memory.sources.top_k", integer=True, strict=True)
                _require(source.get("top_k", 5) <= 100, "memory.sources.top_k")
        if "timeout" in source:
            _number(source["timeout"], "memory.sources.timeout", strict=True)
        if "source_description" in source:
            _require(isinstance(source["source_description"], str) and len(source["source_description"]) <= 2000,
                     "memory.sources.source_description")
    for purpose, policy in _table(memory, "policies", "memory.policies").items():
        _require(isinstance(policy, dict), "memory.policies")
        if purpose == "recall":
            _require(set(policy) <= {"profile", "timeout", "history_max_chars", "input_max_chars"}, "memory.policies.recall")
        if purpose == "rerank":
            _require(set(policy) <= {"enabled", "profile"}, "memory.policies.rerank")
            if policy.get("enabled", False):
                _require("profile" in policy, "memory.policies.rerank.profile")
        if purpose in {"profile", "facts", "knowledge"}:
            allowed = {"enabled", "sources", "max_records", "max_chars", "timeout", "max_queries"}
            if purpose in {"facts", "knowledge"}:
                allowed |= {"candidate_limit", "min_score"}
            _require(set(policy) <= allowed, "memory.policies")
        if "profile" in policy:
            _refs([policy["profile"]], profiles, "memory.policies.profile")
            _require(profiles[policy["profile"]].get("task", "chat") ==
                     ("rerank" if purpose == "rerank" else "chat"), "memory.policies.profile.task")
        _refs(_names(policy.get("sources", []), "memory.policies.sources"), sources, "memory.policies.sources")
        if "enabled" in policy:
            _require(type(policy["enabled"]) is bool, "memory.policies.enabled")
        if purpose in {"recall", "review"} and policy.get("enabled", True) and policy.get("sources"):
            _require("profile" in policy, "memory.policies.profile")
        if "capacity" in policy:
            _number(policy["capacity"], "memory.policies.capacity", integer=True, strict=True)
        if purpose == "review" and "every_turns" in policy:
            _number(policy["every_turns"], "memory.policies.review.every_turns", integer=True)
        if purpose == "review" and policy.get("enabled", False):
            _require(bool(policy.get("sources")), "memory.policies.review.sources")
            _require(all(sources[name]["kind"] == "episodic" and "write" in sources[name].get("capabilities", ())
                         for name in policy["sources"]), "memory.policies.review.sources")
        if "max_records" in policy:
            _require(policy["max_records"] <= 100, "memory.policies.max_records")
        for key in ("max_records", "max_iterations", "timeout", "max_chars"):
            if key in policy:
                _number(policy[key], f"memory.policies.{key}", strict=True, integer=key != "timeout")
        if purpose in {"profile", "facts", "knowledge"}:
            _require(all(sources[name]["kind"] == ("semantic" if purpose == "knowledge" else "episodic")
                         for name in policy.get("sources", ())), "memory.policies.sources")
            if policy.get("enabled", False):
                from ..memory.prompt import MinimumBranchBudget
                _require(policy.get("max_chars", 16000) >= MinimumBranchBudget(purpose, policy.get("sources", ())),
                         "memory.policies.max_chars")
        if "max_queries" in policy:
            _number(policy["max_queries"], "memory.policies.max_queries", strict=True, integer=True)
            _require(policy["max_queries"] <= 3, "memory.policies.max_queries")
        if "candidate_limit" in policy:
            _number(policy["candidate_limit"], "memory.policies.candidate_limit", integer=True, strict=True)
            _require(policy["candidate_limit"] <= 100, "memory.policies.candidate_limit")
        if "min_score" in policy:
            _number(policy["min_score"], "memory.policies.min_score")
            _require(policy["min_score"] <= 1, "memory.policies.min_score")
        if "history_max_chars" in policy:
            _number(policy["history_max_chars"], "memory.policies.history_max_chars", integer=True, minimum=256)
        if "input_max_chars" in policy:
            _number(policy["input_max_chars"], "memory.policies.input_max_chars", integer=True, minimum=256)
    policies = memory.get("policies", {})
    from ..memory.factory import ValidatePolicies
    ValidatePolicies(sources, policies)
    if any(policies.get(branch, {}).get("enabled", False) and policies.get(branch, {}).get("sources") for branch in ("facts", "knowledge")):
        _require("profile" in policies.get("recall", {}), "memory.policies.recall.profile")
    log_options = _table(log, "options", "log.options", required=True)
    observation = _table(log, 'observability', 'log.observability')
    _require(set(observation) <= set(OBSERVABILITY_DEFAULTS), 'log.observability')
    for key, value in observation.items():
        _number(value, 'log.observability.' + key, strict=True, integer=key != 'shutdown_timeout')
    _require(log_options["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}, "log.options.level")
    for key in ("max_bytes", "backup_count"):
        if key in log_options:
            _number(log_options[key], f"log.options.{key}", integer=True)
    for key in ("file", "error_file", "format"):
        if key in log_options:
            _text(log_options[key], f"log.options.{key}")
    for key in ("redact", "console"):
        if key in log_options:
            _require(type(log_options[key]) is bool, f"log.options.{key}")
    serv_options = _table(serv, "options", "serv.options", required=True)
    _require(serv_options["mode"] in {"cli", "web", "both"}, "serv.options.mode")
    _number(serv_options["capacity"], "serv.options.capacity", integer=True, strict=True)
    _number(serv_options["port"], "serv.options.port", integer=True, strict=True)
    _require(serv_options["port"] <= 65535, "serv.options.port")
    for key in ("host", "workspace"):
        _text(serv_options[key], f"serv.options.{key}")
    if "persistence" in serv_options:
        selected = _table(serv_options, "persistence", "serv.options.persistence")
        _text(selected["kind"], "serv.options.persistence.kind")
        settings = _table(selected, "settings", "serv.options.persistence.settings")
        if selected["kind"] == "sqlite":
            _text(settings["path"], "serv.options.persistence.settings.path")
    else:
        _text(serv_options["database"], "serv.options.database")
    _refs([serv_options["model_profile"]], profiles, "serv.options.model_profile")
    _require(profiles[serv_options["model_profile"]].get("task", "chat") == "chat", "serv.options.model_profile.task")
    if "eval_model_profile" in serv_options:
        _refs([serv_options["eval_model_profile"]], profiles, "serv.options.eval_model_profile")
        _require(profiles[serv_options["eval_model_profile"]].get("task", "chat") == "chat", "serv.options.eval_model_profile.task")
    for key in ("max_iterations", "task_timeout", "interaction_timeout", "eval_source_timeout", "eval_judge_timeout"):
        if key in serv_options:
            _number(serv_options[key], f"serv.options.{key}", strict=True, integer=key == "max_iterations")
    if "system_prompt" in serv_options:
        _text(serv_options["system_prompt"], "serv.options.system_prompt")


class ConfMgr:
    _instance = None
    _lock = Lock()

    @classmethod
    def Initialize(cls, conf_dir):
        with cls._lock:
            if cls._instance is not None:
                raise DomainError("ALREADY_INITIALIZED", "Configuration already initialized")
            docs = {}
            for name in ("model", "tool", "memory", "log", "serv"):
                file = Path(conf_dir) / f"{name}.conf"
                try:
                    docs[name] = tomllib.loads(file.read_bytes().decode("utf8"))
                except FileNotFoundError:
                    raise DomainError("CONFIG_MISSING", f"Missing configuration: {file}") from None
                except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                    raise DomainError("CONFIG_INVALID", f"Invalid configuration: {file}") from None
            try:
                _validate(docs)
            except (KeyError, TypeError, ValueError, OverflowError):
                raise DomainError("CONFIG_INVALID", "Invalid configuration structure") from None
            instance = cls()
            instance._model = _TomlModelConf(docs["model"])
            instance._tool = _TomlToolConf(docs["tool"])
            instance._memory = _TomlMemoryConf(docs["memory"])
            instance._log = _TomlLogConf(docs["log"])
            instance._serv = _TomlServConf(docs["serv"])
            cls._instance = instance

    @classmethod
    def GetInstance(cls):
        if cls._instance is None:
            raise DomainError("NOT_READY", "Configuration not initialized")
        return cls._instance

    @classmethod
    def Close(cls):
        with cls._lock:
            cls._instance = None

    def Model(self) -> ModelConf:
        return self._model

    def Tool(self) -> ToolConf:
        return self._tool

    def Memory(self) -> MemoryConf:
        return self._memory

    def Log(self) -> LogConf:
        return self._log

    def Serv(self) -> ServConf:
        return self._serv
