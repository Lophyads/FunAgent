"""Atomic TOML loading; these snapshots never create clients or other managers."""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
import tomllib
from math import isfinite

from .contracts import DomainError


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider_id: str
    adapter_id: str
    model: str
    timeout: float
    max_retries: int = 0
    fallback_profiles: tuple[str, ...] = ()


class ModelConf(ABC):
    @abstractmethod
    def Profile(self, profile_id: str) -> ModelProfile: ...

    @abstractmethod
    def Providers(self) -> Mapping: ...


class ToolConf(ABC):
    @abstractmethod
    def Definitions(self) -> Mapping: ...

    @abstractmethod
    def Backends(self) -> Mapping: ...


class MemoryConf(ABC):
    @abstractmethod
    def Sources(self) -> Mapping: ...

    @abstractmethod
    def Policy(self, purpose: str) -> Mapping: ...


class LogConf(ABC):
    @abstractmethod
    def Options(self) -> Mapping: ...


class ServConf(ABC):
    @abstractmethod
    def Options(self) -> Mapping: ...


class _TomlModelConf(ModelConf):
    def __init__(self, data):
        self._providers = _freeze(data["providers"])
        self._profiles = MappingProxyType({
            key: ModelProfile(key, value["provider_id"], value["adapter_id"],
                              value["model"], value["timeout"], value.get("max_retries", 0),
                              tuple(value.get("fallback_profiles", ())))
            for key, value in data["profiles"].items()
        })

    def Profile(self, profile_id):
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise DomainError("CONFIG_REFERENCE", "Unknown model profile") from None

    def Providers(self):
        return self._providers


class _TomlToolConf(ToolConf):
    def __init__(self, data):
        self._data = _freeze(data)

    def Definitions(self):
        return self._data.get("definitions", MappingProxyType({}))

    def Backends(self):
        return self._data.get("backends", MappingProxyType({}))


class _TomlMemoryConf(MemoryConf):
    def __init__(self, data):
        self._data = _freeze(data)

    def Sources(self):
        return self._data.get("sources", MappingProxyType({}))

    def Policy(self, purpose):
        try:
            return self._data["policies"][purpose]
        except KeyError:
            raise DomainError("CONFIG_REFERENCE", "Unknown memory policy") from None


class _TomlOptionsConf:
    def __init__(self, data):
        self._options = _freeze(data["options"])

    def Options(self):
        return self._options


class _TomlLogConf(_TomlOptionsConf, LogConf):
    pass


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
        ("tool", {"definitions", "backends", "purposes"}),
        ("memory", {"sources", "policies"}),
        ("log", {"options"}), ("serv", {"options"}),
    ):
        _require(set(docs[key]) <= allowed, key)
    providers = _table(model, "providers", "model.providers", required=True)
    profiles = _table(model, "profiles", "model.profiles", required=True)
    adapters = _names(model["adapters"], "model.adapters")
    _require(bool(providers) and bool(profiles) and bool(adapters), "model")
    for provider in providers.values():
        _require(isinstance(provider, dict), "model.providers")
        _text(provider["kind"], "model.providers.kind")
    for profile in profiles.values():
        _require(isinstance(profile, dict), "model.profiles")
        _refs([profile["provider_id"]], providers, "model.profiles.provider_id")
        _refs([profile["adapter_id"]], adapters, "model.profiles.adapter_id")
        _text(profile["model"], "model.profiles.model")
        _number(profile["timeout"], "model.profiles.timeout", strict=True)
        _number(profile.get("max_retries", 0), "model.profiles.max_retries", integer=True)
        _refs(_names(profile.get("fallback_profiles", []), "model.profiles.fallback_profiles"),
              profiles, "model.profiles.fallback_profiles")
    definitions = _table(tool, "definitions", "tool.definitions")
    backends = _table(tool, "backends", "tool.backends")
    for backend in backends.values():
        _require(isinstance(backend, dict), "tool.backends")
        _text(backend["kind"], "tool.backends.kind")
        if "timeout" in backend:
            _number(backend["timeout"], "tool.backends.timeout", strict=True)
    for definition in definitions.values():
        _require(isinstance(definition, dict), "tool.definitions")
        _text(definition["handler"], "tool.definitions.handler")
        _require(isinstance(definition["parameters"], dict), "tool.definitions.parameters")
        if "backend_id" in definition:
            _refs([definition["backend_id"]], backends, "tool.definitions.backend_id")
    for names in _table(tool, "purposes", "tool.purposes").values():
        _refs(_names(names, "tool.purposes"), definitions, "tool.purposes")
    sources = _table(memory, "sources", "memory.sources")
    for source in sources.values():
        _require(isinstance(source, dict), "memory.sources")
        _require(source["kind"] in {"semantic", "episodic"}, "memory.sources.kind")
        _text(source["store"], "memory.sources.store")
        _names(source.get("capabilities", ["search"]), "memory.sources.capabilities")
        if "timeout" in source:
            _number(source["timeout"], "memory.sources.timeout", strict=True)
    for policy in _table(memory, "policies", "memory.policies").values():
        _require(isinstance(policy, dict), "memory.policies")
        if "profile" in policy:
            _refs([policy["profile"]], profiles, "memory.policies.profile")
        _refs(_names(policy.get("sources", []), "memory.policies.sources"), sources, "memory.policies.sources")
        for key in ("max_records", "max_iterations", "timeout"):
            if key in policy:
                _number(policy[key], f"memory.policies.{key}", strict=True, integer=key != "timeout")
    log_options = _table(log, "options", "log.options", required=True)
    _require(log_options["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}, "log.options.level")
    for key in ("max_bytes", "backup_count"):
        if key in log_options:
            _number(log_options[key], f"log.options.{key}", integer=True)
    serv_options = _table(serv, "options", "serv.options", required=True)
    _require(serv_options["mode"] in {"cli", "web", "both"}, "serv.options.mode")
    _number(serv_options["capacity"], "serv.options.capacity", integer=True, strict=True)
    _number(serv_options["port"], "serv.options.port", integer=True, strict=True)
    _require(serv_options["port"] <= 65535, "serv.options.port")
    for key in ("host", "workspace", "database"):
        _text(serv_options[key], f"serv.options.{key}")
    _refs([serv_options["model_profile"]], profiles, "serv.options.model_profile")


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
