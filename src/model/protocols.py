"""IO and wire format are independent; strategies return only four data choices."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Generic, Protocol, TypeVar
from collections.abc import Iterator
from contextlib import AbstractContextManager

from .contracts import ModelProfile, RerankRequest, RerankResult
from ..frame.contracts import DomainError, ModelReply, ModelRequest, RunContext, RunInput, RunResult
from ..tool.contracts import ToolSelectionRequest


class Runner(Protocol):
    def Run(self, input: RunInput, strategy: type["ModelStrategy"], context: RunContext) -> RunResult: ...

    def Rank(self, request: RerankRequest, profile_id: str, context: RunContext) -> RerankResult: ...


@dataclass(frozen=True)
class ProviderRequest:
    payload: Any = field(repr=False)
    # HTTP JSON may execute an explicit finite sequence under the same deadline.
    batch: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class ProviderResponse:
    payload: Any = field(repr=False)


_MODEL_ERROR_CODES = frozenset({
    "TRANSPORT", "TIMEOUT", "RATE_LIMIT", "AUTH", "INVALID_REQUEST",
    "PROTOCOL", "TRUNCATED", "PROVIDER_FATAL", "PROFILES_EXHAUSTED", "MODEL_NOT_FOUND", "EMPTY_REPLY",
})


class ModelError(DomainError):
    def __init__(self, code: str, *, cause_code: str | None = None, retry_after: float | None = None):
        safe_code = code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "PROVIDER_FATAL"
        super().__init__(safe_code, "Model request failed")
        self.cause_code = (cause_code if isinstance(cause_code, str) and cause_code in _MODEL_ERROR_CODES
                           else "PROVIDER_FATAL" if cause_code is not None else None)
        self.retry_after = None
        if type(retry_after) in (int, float) and safe_code in {"RATE_LIMIT", "TRANSPORT", "TIMEOUT"}:
            try:
                seconds = float(retry_after)
                if isfinite(seconds) and seconds >= 0:
                    self.retry_after = seconds
            except OverflowError:
                pass


class Provider(Protocol):
    """One synchronous IO attempt, respecting context deadline/cancel.

    Implementations disable SDK retries and borrow under a short state lock.
    Concurrent Send calls perform IO independently, sharing no attempt state.
    Recheck control before IO. Close rejects new borrows and drains active ones.
    Translate IO failures to ModelError; never expose provider payload/credentials.
    """
    def Send(self, request: ProviderRequest, context: RunContext) -> ProviderResponse: ...
    def Close(self) -> None: ...


class StreamingProvider(Protocol):
    """Optional Stream context manager owns IO through iterator cleanup."""
    def Stream(self, request: ProviderRequest, context: RunContext) -> AbstractContextManager[Iterator[ProviderResponse]]: ...


class StreamingAdapter(Protocol):
    """Each NewStream returns an independent Add/Finish accumulator."""
    def NewStream(self): ...


RequestT = TypeVar('RequestT')
ResultT = TypeVar('ResultT')


class Adapter(Generic[RequestT, ResultT], ABC):
    # Rank uses the wire protocol's batch capacity under one shared deadline.
    batch_size = 32

    @staticmethod
    @abstractmethod
    def Encode(request: RequestT, profile: ModelProfile) -> ProviderRequest: ...

    @staticmethod
    @abstractmethod
    def Decode(response: ProviderResponse) -> ResultT: ...


@dataclass(frozen=True)
class FallbackRule:
    profile_ids: tuple[str, ...] = ()
    retry_codes: frozenset[str] = frozenset({"TRANSPORT", "TIMEOUT", "RATE_LIMIT"})
    fallback_codes: frozenset[str] = frozenset({"TRANSPORT", "TIMEOUT", "RATE_LIMIT",
                                             "AUTH", "MODEL_NOT_FOUND", "EMPTY_REPLY"})

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
    def ToolSet() -> ToolSelectionRequest | tuple[str, ...]: ...

    @staticmethod
    @abstractmethod
    def FallbackPolicy() -> FallbackRule: ...


class ModelDiagnostics(Protocol):
    def Inspect(self) -> list[dict]: ...
