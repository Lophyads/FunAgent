"""Build the selected route and lazily load its drivers at startup."""
from importlib import import_module

from ..frame.contracts import DomainError


PROVIDERS = {"demo": (".demo", "DemoProvider"), "openai": (".openai_chat", "OpenAIProvider"),
             "http_json": (".http_json", "HTTPJSONProvider")}
ADAPTERS = {"demo": (".demo", "DemoAdapter"), "openai_chat": (".openai_chat", "OpenAIChatAdapter"),
            "tei_rerank": (".tei_rerank", "TEIRerankAdapter")}


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


def Create(conf, own, *, tool_dispatch, journal, events=None):
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
        if (profile.task not in {"chat", "rerank"} or
                (profile.task == "rerank") != (profile.adapter_id == "tei_rerank") or
                (profile.profile_id in generation and profile.task != "chat") or
                any(profiles[name].task != profile.task for name in profile.fallback_profiles)):
            raise DomainError("CONFIG_INVALID", "Model task and adapter do not match")
        if profile.task == "rerank" and (profile.streaming or profile.max_retries != 0 or profile.fallback_profiles or
                conf.Model().Providers()[profile.provider_id]["kind"] != "http_json"):
            raise DomainError("CONFIG_INVALID", "Invalid scoring route")
    if recall_active and rerank.get("enabled", False) and profiles[rerank["profile"]].task != "rerank":
        raise DomainError("CONFIG_INVALID", "Memory scoring requires a rerank profile")
    for profile in profiles.values():
        adapters[profile.adapter_id] = _selected(ADAPTERS, profile.adapter_id)
        if profile.provider_id not in providers:
            settings = conf.Model().Providers()[profile.provider_id]
            providers[profile.provider_id] = own(_selected(PROVIDERS, settings["kind"])(settings))
    for profile in profiles.values():
        if profile.streaming and (not callable(getattr(providers[profile.provider_id], 'Stream', None))
                                  or not callable(getattr(adapters[profile.adapter_id], 'NewStream', None))):
            raise DomainError('UNSUPPORTED', 'Selected model does not support streaming')
    return Runtime(profiles, providers, adapters, tool_dispatch=tool_dispatch, journal=journal,
                   events=events, owns_providers=False)
