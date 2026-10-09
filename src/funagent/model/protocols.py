"""IO and wire format are independent; strategies return only four data choices."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..conf import ModelProfile
from ..contracts import DomainError, ModelReply, ModelRequest, RunContext


@dataclass(frozen=True)
class ProviderRequest:
    payload: Any = field(repr=False)


@dataclass(frozen=True)
class ProviderResponse:
    payload: Any = field(repr=False)


_MODEL_ERROR_CODES = frozenset({
    "TRANSPORT", "TIMEOUT", "RATE_LIMIT", "AUTH", "INVALID_REQUEST",
    "PROTOCOL", "TRUNCATED", "PROVIDER_FATAL", "PROFILES_EXHAUSTED",
})


class ModelError(DomainError):
    def __init__(self, code: str, *, cause_code: str | None = None):
        safe_code = code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "PROVIDER_FATAL"
        super().__init__(safe_code, "Model request failed")
        self.cause_code = (cause_code if isinstance(cause_code, str) and cause_code in _MODEL_ERROR_CODES
                           else "PROVIDER_FATAL" if cause_code is not None else None)


class Provider(Protocol):
    """One synchronous IO attempt, respecting context deadline/cancel.

    Implementations own client/connection locks and disable SDK automatic retries.
    A lock wait consumes the same deadline; recheck control before starting IO.
    Concurrent Send calls must be safe. Close runs only after borrowers finish.
    Translate IO failures to ModelError; never expose provider payload/credentials.
    """
    def Send(self, request: ProviderRequest, context: RunContext) -> ProviderResponse: ...
    def Close(self) -> None: ...


class Adapter(ABC):
    @staticmethod
    @abstractmethod
    def Encode(request: ModelRequest, profile: ModelProfile) -> ProviderRequest: ...

    @staticmethod
    @abstractmethod
    def Decode(response: ProviderResponse) -> ModelReply: ...


@dataclass(frozen=True)
class FallbackRule:
    profile_ids: tuple[str, ...] = ()
    retry_codes: frozenset[str] = frozenset({"TRANSPORT", "TIMEOUT", "RATE_LIMIT"})
    fallback_codes: frozenset[str] = frozenset({"TRANSPORT", "TIMEOUT", "RATE_LIMIT"})

    def __post_init__(self):
        object.__setattr__(self, "profile_ids", tuple(self.profile_ids))
        object.__setattr__(self, "retry_codes", frozenset(self.retry_codes))
        object.__setattr__(self, "fallback_codes", frozenset(self.fallback_codes))


class ModelStrategy(ABC):
    @staticmethod
    @abstractmethod
    def SelectModel() -> str: ...

    @staticmethod
    @abstractmethod
    def MaxIterations() -> int: ...

    @staticmethod
    @abstractmethod
    def ToolSet() -> tuple[str, ...]: ...

    @staticmethod
    @abstractmethod
    def FallbackPolicy() -> FallbackRule: ...
