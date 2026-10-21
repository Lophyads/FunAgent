"""IO and wire format are independent; strategies return only four data choices."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Generic, Protocol, TypeVar
from collections.abc import Iterator
from contextlib import AbstractContextManager

from .contracts import ModelProfile, RerankRequest, RerankResult
from ..frame.contracts import DomainError, Message, ModelReply, ModelRequest, RunContext, RunInput, RunResult
from ..tool.contracts import ToolSelectionRequest


class Runner(Protocol):
    def Run(self, input: RunInput, strategy: type["ModelStrategy"], context: RunContext) -> RunResult: ...

    def Rank(self, request: RerankRequest, profile_id: str, context: RunContext) -> RerankResult: ...


@dataclass(frozen=True)
class ProviderRequest:
    payload: Any = field(repr=False)
    # HTTP JSON may execute an explicit finite sequence under the same deadline.
    batch: bool = field(default=False, kw_only=True)
    operation: str = field(default="chat_completions", kw_only=True)


@dataclass(frozen=True)
class ProviderResponse:
    payload: Any = field(repr=False)


_MODEL_ERROR_CODES = frozenset({
    "TRANSPORT", "TIMEOUT", "RATE_LIMIT", "AUTH", "INVALID_REQUEST",
    "PROTOCOL", "TRUNCATED", "PROVIDER_FATAL", "PROFILES_EXHAUSTED", "MODEL_NOT_FOUND", "EMPTY_REPLY",
    "AUTH_REQUIRED", "PLAN_PERMISSION_REQUIRED", "SUBSCRIPTION_LIMIT", "ACCESS_DENIED", "AUTH_STORAGE", "UNSUPPORTED",
})


def SafeModelIdentifier(value):
    return value if isinstance(value, str) and len(value) <= 128 and all(
        c.isalnum() or c in '_-.' for c in value) else None


class ModelError(DomainError):
    def __init__(self, code: str, *, cause_code: str | None = None, retry_after: float | None = None,
                 http_status=None, remote_code=None, request_id=None):
        safe_code = code if isinstance(code, str) and code in _MODEL_ERROR_CODES else "PROVIDER_FATAL"
        super().__init__(safe_code, "Model request failed")
        self.cause_code = (cause_code if isinstance(cause_code, str) and cause_code in _MODEL_ERROR_CODES
                           else "PROVIDER_FATAL" if cause_code is not None else None)
        self.retry_after = None
        self.http_status = http_status if type(http_status) is int else None
        self.remote_code = SafeModelIdentifier(remote_code)
        self.request_id = SafeModelIdentifier(request_id)
        if type(retry_after) in (int, float) and safe_code in {"RATE_LIMIT", "TRANSPORT", "TIMEOUT"}:
            try:
                seconds = float(retry_after)
                if isfinite(seconds) and seconds >= 0:
                    self.retry_after = seconds
            except OverflowError:
                pass


def RemoteError(status=None, remote_code=None, request_id=None, retry_after=None, *, siwc=False):
    """Stable transport classification; raw remote messages never cross this boundary."""
    remote_code = remote_code if isinstance(remote_code, str) else None
    if remote_code in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible"}:
        code = "SUBSCRIPTION_LIMIT"
    elif remote_code in {"chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"}:
        code = "PLAN_PERMISSION_REQUIRED"
    elif remote_code == "subscription_sharing_invalid_user":
        code = "AUTH_REQUIRED"
    elif remote_code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable", "server_error"}:
        code = "TRANSPORT"
    elif remote_code == "rate_limit_exceeded":
        code = "RATE_LIMIT"
    elif remote_code == "subscription_sharing_unsupported_capability":
        code = "INVALID_REQUEST"
    elif remote_code == "subscription_sharing_route_not_supported":
        code = "ACCESS_DENIED"
    elif status in {401, 403}:
        code = ("AUTH_REQUIRED" if status == 401 else "ACCESS_DENIED") if siwc else "AUTH"
    elif status == 429:
        code = "RATE_LIMIT"
    elif status == 404 and remote_code in (None, "model_not_found", "deployment_not_found", "endpoint_not_found", "DeploymentNotFound"):
        code = "MODEL_NOT_FOUND"
    elif status == 408:
        code = "TIMEOUT"
    elif (status is not None and status >= 500) or status == 409:
        code = "TRANSPORT"
    else:
        code = "INVALID_REQUEST" if status is not None else "PROVIDER_FATAL"
    return ModelError(code, http_status=status, remote_code=remote_code, request_id=request_id,
                      retry_after=retry_after)


class Provider(Protocol):
    """Synchronous IO respecting context deadline/cancel, including explicit batches.

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

    @staticmethod
    def BuildMessage(reply, profile):
        return Message("assistant", reply.content or "", reply.calls)


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
