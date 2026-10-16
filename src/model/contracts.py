"""Model values; importing them does not load configuration or drivers."""
from dataclasses import dataclass, field
from collections.abc import Mapping
import time
from ..frame.contracts import freeze


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider_id: str
    adapter_id: str
    model: str
    timeout: float
    max_retries: int = 0
    fallback_profiles: tuple[str, ...] = ()
    streaming: bool = False
    task: str = 'chat'

    def __post_init__(self):
        object.__setattr__(self, "fallback_profiles", tuple(self.fallback_profiles))


@dataclass(frozen=True)
class RerankRequest:
    question: str
    texts: tuple[str, ...]


@dataclass(frozen=True)
class RerankResult:
    scores: tuple[float, ...]
    profile_id: str = ''
    model: str = ''


@dataclass(frozen=True)
class RunEvent:
    task_id: str
    run_id: str = ''
    purpose: str = 'main'
    iteration: int = 0
    attempt_id: int = 0
    kind: str = ''
    payload: Mapping = field(default_factory=dict)
    seq: int = 0
    session_id: str = ''
    occurred_at: float = field(default_factory=time.time)

    def __post_init__(self):
        object.__setattr__(self, 'payload', freeze(self.payload))
