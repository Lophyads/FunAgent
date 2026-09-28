"""Public API. Execution layers are loaded lazily to keep leaf modules independent."""

from importlib import import_module

from .config import load_config
from log.logging_system import LoggingHandle, configure_logging
from .interfaces import (
    AllowAllGuard,
    HostCapabilities, TurnHostContext, HumanApprovalRequired,
    ApprovalDecision,
    Approver,
    CancellationToken,
    GuardDecision,
    MemoryProvider,
    MemoryUpdater,
    NoopMemoryProvider,
    NoopMemoryUpdater,
    NoopReviewScheduler,
    ReviewScheduler,
    ToolContext,
    ToolGuard,
    ToolInvocation,
    TurnCancelled,
    TurnSnapshot,
)
from .interaction import (
    InteractionBroker,
    ApprovalPayload, ApprovalResponse, ClarifyPayload, ClarifyResponse, Question,
    InteractionPort, InteractionRequest,
)
from .lease import (
    LeaseAcquireTimeout,
    LeaseHeartbeatService,
    SQLiteTurnLeaseStore,
    TurnLeaseStore,
)
from .models import (
    AgentConfig,
    CanonicalModelRequest,
    LeaseFence,
    LeaseHandle,
    LeasePolicy,
    LoggingConfig,
    MemoryItem,
    Message,
    ModelRoute,
    NormalizedModelResponse,
    RetryPolicy,
    SessionDefaults,
    TimeoutPolicy,
    ToolServiceConfig,
    ToolCall,
    ToolOutcome,
    TurnError,
    TurnResult,
    TurnTrigger,
    UserInput,
    WorkerPolicy,
)
from .observability import (
    AgentEvent,
    EventEnvelope,
    EventSink,
    LifecycleObserver,
    NullEventSink,
    ObservabilityHub,
)
from .prompt_builder import (
    PromptBuildRequest,
    StaticSystemPromptBuilder,
    SystemPromptBuilder,
    SystemPromptSnapshot,
)
from .provider_runtime import (
    ApiModeAdapter,
    ApiModeRegistry,
    AttemptContext,
    ChatCompletionsAdapter,
    EnvironmentCredentialResolver,
    NormalizedProviderError,
    OpenAICompatibleProviderResolver,
    OpenAIHttpTransport,
    ProviderConfigurationError,
    ProviderResolver,
    ProviderRuntime,
    StaticProviderResolver,
    Transport,
    WireRequest,
    WireResponse,
    default_api_mode_registry,
)
from .storage import LeaseLostError, SQLiteStore, StorageError
from tools.registry import ToolEntry, ToolExecutionPolicy, ToolRegistry
from .approval import ApprovalGate, ApprovalGrantStore
from tools.lifecycle import CleanupIssue, CleanupReport, SessionResourceKey, SessionResourcesBusyError
from tools.config import ToolSystemConfig, WorkspaceConfig
from tools.runtime import initialize_tool_system, get_tool_registry, get_tool_services, shutdown_tool_system
from tools.toolsets import (
    DefaultToolsetResolver,
    ToolsetRequest,
    ToolsetResolver,
)

__all__ = [
    "PreparedConversation", "ProviderRunner",
    "BackgroundReviewScheduler", "ReviewTask",
    "ApprovalGate", "ApprovalPayload", "ApprovalResponse", "ClarifyPayload", "ClarifyResponse",
    "Question", "CleanupIssue", "CleanupReport", "SessionResourceKey", "SessionResourcesBusyError",
    "ApprovalGrantStore", "InteractionBroker",
    "InteractionPort", "InteractionRequest", "ToolExecutionPolicy",
    "ToolServices",
    "AgentConfig", "AgentCore", "AgentEvent", "AllowAllGuard",
    "ConversationLoop", "TurnPersistence", "LeaseScope",
    "ApiModeAdapter", "ApiModeRegistry", "ApprovalDecision", "Approver",
    "AttemptContext", "CancellationToken",
    "CanonicalModelRequest", "ChatCompletionsAdapter",
    "DefaultToolsetResolver", "DeliveryAdapter", "DeliveryBatch",
    "DeliveryRuntime", "EnvironmentCredentialResolver", "GatewayRuntime",
    "EventEnvelope", "EventSink", "GuardDecision", "LeaseAcquireTimeout",
    "LeaseFence", "LeaseHandle", "LeaseHeartbeatService", "LeaseLostError",
    "LeasePolicy", "LifecycleObserver", "LoggingConfig", "LoggingHandle", "LoopState", "MemoryItem",
    "MemoryProvider", "MemoryUpdater", "Message", "ModelRoute",
    "NoopMemoryProvider", "NoopMemoryUpdater", "NoopReviewScheduler",
    "NormalizedModelResponse", "NormalizedProviderError", "NullEventSink",
    "ObservabilityHub", "OpenAICompatibleProviderResolver",
    "OpenAIHttpTransport", "OperationTimeoutError", "PhaseAction",
    "PhaseResult", "PromptBuildRequest", "ProviderConfigurationError",
    "ProviderResolver", "ProviderRuntime", "RetryPolicy", "ReviewScheduler",
    "RunBudgetExceeded", "SQLiteStore", "SQLiteTurnLeaseStore",
    "SessionBuildRequest", "SessionContextFactory",
    "SessionContextResolution", "SessionDefaults", "SessionRuntimeSnapshot",
    "StaticProviderResolver",
    "StaticSystemPromptBuilder", "StorageError", "SystemPromptBuilder",
    "SystemPromptSnapshot", "TimeoutPolicy", "ToolCall", "ToolEntry",
    "ToolContext", "ToolGuard", "ToolInvocation", "ToolOutcome",
    "ToolServiceConfig",
    "ToolExecution", "ToolExecutor", "ToolRegistry", "ToolRoundRunner",
    "ToolsetRequest", "ToolsetResolver", "Transport",
    "TurnCancelled", "TurnError", "TurnLeaseStore",
    "TurnPhase", "TurnQueueFullError", "TurnResult", "TurnRuntime",
    "TurnSnapshot", "TurnTrigger", "UserInput", "WireRequest", "WireResponse", "WorkerPolicy",
    "WorkerQueueFullError", "default_api_mode_registry",
    "load_config", "configure_logging", "ToolSystemConfig", "WorkspaceConfig",
    "initialize_tool_system", "get_tool_registry", "get_tool_services", "shutdown_tool_system",
    "HostCapabilities", "TurnHostContext", "HumanApprovalRequired",
]


_LAZY_EXPORTS = {
    "PreparedConversation": ".turn_preparation",
    "ProviderRunner": ".provider_runner",
    "BackgroundReviewScheduler": ".background_review",
    "ReviewTask": ".background_review",
    "ConversationLoop": ".conversation_loop",
    "TurnPersistence": ".turn_persistence",
    "LeaseScope": ".lease",
    "DeliveryAdapter": "gateway.delivery",
    "DeliveryBatch": "gateway.delivery",
    "DeliveryRuntime": "gateway.delivery",
    "GatewayRuntime": "gateway.gateway",
    "AgentCore": ".core",
    "TurnQueueFullError": ".workers",
    "WorkerQueueFullError": ".workers",
    "SessionBuildRequest": ".session_context",
    "SessionContextFactory": ".session_context",
    "SessionContextResolution": ".session_context",
    "SessionRuntimeSnapshot": ".session_context",
    "ToolServices": "tools.services",
    "ToolExecution": ".tool_execution",
    "ToolExecutor": ".tool_execution",
    "ToolRoundRunner": ".tool_round",
    "PhaseAction": ".turn_phase",
    "PhaseResult": ".turn_phase",
    "TurnPhase": ".turn_phase",
    "LoopState": ".turn_state",
    "OperationTimeoutError": ".turn_state",
    "RunBudgetExceeded": ".turn_state",
    "TurnRuntime": ".turn_state",
}


def __getattr__(name: str):
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
