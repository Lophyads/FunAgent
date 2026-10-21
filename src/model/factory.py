"""Build the selected route and lazily load its drivers at startup."""
from importlib import import_module
from dataclasses import replace
import hashlib
import json
import time
import secrets

from ..frame.contracts import DomainError, RunContext


PROVIDERS = {"demo": (".demo", "DemoProvider"), "openai": (".openai_provider", "OpenAIProvider"),
             "http_json": (".http_json", "HTTPJSONProvider")}
ADAPTERS = {"demo": (".demo", "DemoAdapter"), "openai_chat": (".openai_chat", "OpenAIChatAdapter"),
            "openai_responses": (".openai_responses", "OpenAIResponsesAdapter"),
            "tei_rerank": (".tei_rerank", "TEIRerankAdapter"),
            "qwen3_rerank": (".qwen3_rerank", "Qwen3RerankAdapter")}


def _selected(table, kind):
    selected = table.get(kind)
    if selected is None:
        raise DomainError("DRIVER_UNAVAILABLE", "Selected model driver is unavailable")
    if isinstance(selected, tuple):
        try:
            return getattr(import_module(selected[0], __package__), selected[1])
        except ImportError:
            raise DomainError("DRIVER_UNAVAILABLE", "Selected model driver is unavailable") from None
    return selected


def Create(conf, own, *, tool_dispatch, journal, events=None, resolve_siwc=None):
    from .runtime import Runtime
    profiles, providers, adapters = {}, {}, {}
    pending = [conf.Serv().Options()["model_profile"]]
    if "eval_model_profile" in conf.Serv().Options():
        pending.append(conf.Serv().Options()["eval_model_profile"])
    recall_active = any(conf.Memory().Policy(branch).get("enabled", False) and conf.Memory().Policy(branch).get("sources")
                        for branch in ("facts", "knowledge"))
    for purpose in ("review", "recall"):
        policy = conf.Memory().Policy(purpose)
        active = (recall_active if purpose == "recall" else
                  policy.get("enabled", False) and policy.get("sources"))
        if active:
            pending.append(policy["profile"])
    generation = set(pending)
    rerank = conf.Memory().Policy("rerank")
    if recall_active and rerank.get("enabled", False):
        pending.append(rerank["profile"])
    while pending:
        name = pending.pop()
        if name not in profiles:
            profiles[name] = conf.Model().Profile(name)
            pending.extend(profiles[name].fallback_profiles)
    # Check bindings before creating any resources, including custom Conf views.
    for profile in profiles.values():
        settings = conf.Model().Providers()[profile.provider_id]
        if settings.get('auth', {}).get('kind') == 'siwc' and (settings['kind'] != 'openai' or
                profile.adapter_id != 'openai_responses' or not profile.streaming or profile.max_retries or
                profile.fallback_profiles or 'credential_env' in settings or
                settings.get('base_url', 'https://api.openai.com/v1').rstrip('/') != 'https://api.openai.com/v1'):
            raise DomainError('CONFIG_INVALID', 'Invalid SIWC model route')
        if (profile.task not in {"chat", "rerank"} or
                (profile.task == "rerank") != (profile.adapter_id in {"tei_rerank", "qwen3_rerank"}) or
                (profile.profile_id in generation and profile.task != "chat") or
                any(profiles[name].task != profile.task for name in profile.fallback_profiles)):
            raise DomainError("CONFIG_INVALID", "Model task and adapter do not match")
        if profile.task == "rerank" and (profile.streaming or profile.max_retries != 0 or profile.fallback_profiles or
                conf.Model().Providers()[profile.provider_id]["kind"] != "http_json"):
            raise DomainError("CONFIG_INVALID", "Invalid scoring route")
    if recall_active and rerank.get("enabled", False) and profiles[rerank["profile"]].task != "rerank":
        raise DomainError("CONFIG_INVALID", "Memory scoring requires a rerank profile")
    bindings = {}
    for name, profile in tuple(profiles.items()):
        adapters[profile.adapter_id] = _selected(ADAPTERS, profile.adapter_id)
        if profile.provider_id not in providers:
            settings = conf.Model().Providers()[profile.provider_id]
            if settings.get("auth", {}).get("kind") == "siwc":
                if resolve_siwc is None:
                    raise DomainError("CONFIG_REFERENCE", "SIWC credential resolver is required")
                session = resolve_siwc(settings["auth"]["state_dir"])
                credential = session.GetAccess(RunContext(purpose="startup", deadline=time.monotonic() + 15))
                bindings[profile.provider_id] = credential.binding
                providers[profile.provider_id] = own(_selected(PROVIDERS, settings["kind"])(settings, get_access=session.GetAccess))
            else:
                providers[profile.provider_id] = own(_selected(PROVIDERS, settings["kind"])(settings))
                # API keys have no validated SIWC account binding. Limit opaque
                # replay to this connection lifetime instead of guessing identity.
                bindings[profile.provider_id] = 'connection:' + secrets.token_hex(16)
        if profile.adapter_id == "openai_responses":
            settings = conf.Model().Providers()[profile.provider_id]
            if not profile.streaming or settings["kind"] != "openai":
                raise DomainError("CONFIG_INVALID", "Responses requires an OpenAI streaming profile")
            binding = bindings[profile.provider_id]
            scope = hashlib.sha256(json.dumps([1, "openai_responses", profile.provider_id,
                settings.get("base_url", "https://api.openai.com/v1").rstrip("/"), profile.model, binding],
                separators=(",", ":")).encode()).hexdigest()
            profiles[name] = replace(profile, replay_scope=scope)
    for profile in profiles.values():
        if profile.streaming and (not callable(getattr(providers[profile.provider_id], 'Stream', None))
                                  or not callable(getattr(adapters[profile.adapter_id], 'NewStream', None))):
            raise DomainError('UNSUPPORTED', 'Selected model does not support streaming')
    return Runtime(profiles, providers, adapters, tool_dispatch=tool_dispatch, journal=journal,
                   events=events, owns_providers=False)
