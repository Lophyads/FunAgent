"""Gateway interaction adapter sketch.

The platform must authenticate the replying user and map them to the same
FunAgent session before calling handle_authenticated_reply. The reply is not a
new Agent turn; it wakes the existing turn by opaque request ID.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from tools.lifecycle import remaining

from agentcore import (
    GatewayRuntime, InteractionRequest, ToolSystemConfig,
    initialize_tool_system, shutdown_tool_system,
)


class PlatformInteractionPort:
    def __init__(
        self, notify: Callable[[str, str, str, dict[str, Any]], None],
    ) -> None:
        self.notify = notify

    def present(self, request: InteractionRequest) -> None:
        self.notify(
            request.session_id, request.request_id,
            request.kind, request.to_dict()["payload"],
        )


def handle_authenticated_reply(
    gateway: GatewayRuntime, *, session_id: str,
    request_id: str, response: Any,
) -> bool:
    """Return False for a wrong Session, expired, cancelled, or stale request."""
    return gateway.resolve_interaction(session_id, request_id, response)


# Example construction after creating your authenticated platform adapter:
#
# port = PlatformInteractionPort(platform.send_interaction)
# initialize_tool_system(ToolSystemConfig.from_agent_config(config))
# gateway = GatewayRuntime(config, delivery_adapter, interaction_mode="push", interaction_port=port)
# recovery = gateway.attach_session(authenticated_session_id)
# submission = gateway.submit_turn("Hello", authenticated_session_id)
# # Completion does not mean the platform acknowledged delivery.
# # Do not send submission.future.result().final_text yourself.
# ...
# deadline = time.monotonic() + config.timeouts.finalize_seconds
# for close in (gateway.close_blocking, shutdown_tool_system):
#     try:
#         report = close(timeout=remaining(deadline))
#         if not report.completed:
#             print(report.issues)
#     except Exception as exc:
#         print(f"Cleanup failed: {type(exc).__name__}")
#
# Clarify reply: {"answers": {"q0": "chosen option or free text"}}
# Approval reply: {"decision": "once" | "session" | "always" | "deny"}
