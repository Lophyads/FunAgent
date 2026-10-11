"""Build the selected route and lazily load its drivers at startup."""
from importlib import import_module

from ..frame.contracts import DomainError


PROVIDERS = {"demo": (".demo", "DemoProvider"), "openai": (".openai_chat", "OpenAIProvider")}
ADAPTERS = {"demo": (".demo", "DemoAdapter"), "openai_chat": (".openai_chat", "OpenAIChatAdapter")}


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
    for purpose in ("review", "recall"):
        policy = conf.Memory().Policy(purpose)
        active = (any(conf.Memory().Policy(branch).get("enabled", False) and conf.Memory().Policy(branch).get("sources")
                      for branch in ("facts", "knowledge")) if purpose == "recall" else
                  policy.get("enabled", False) and policy.get("sources"))
        if active:
            pending.append(policy["profile"])
    while pending:
        name = pending.pop()
        if name not in profiles:
            profiles[name] = conf.Model().Profile(name)
            pending.extend(profiles[name].fallback_profiles)
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
