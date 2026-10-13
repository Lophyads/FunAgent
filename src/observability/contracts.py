"""Internal observation values; never part of RunContext or business signatures."""
from dataclasses import dataclass
from collections.abc import Mapping
from hashlib import sha256


def TraceId(task_id):
    return sha256(("funagent:task:" + task_id).encode("utf8")).hexdigest()[:32]


@dataclass(frozen=True)
class NodeRef:
    task_id: str
    trace_id: str
    span_id: str


@dataclass(frozen=True)
class UsageSnapshot:
    status: str = "unknown"
    raw: Mapping | None = None


@dataclass(frozen=True)
class ObservationRecord:
    target: NodeRef
    operation: str
    occurred_at: float
    fields: Mapping


DEFAULTS = {
    "queue_capacity": 1024,
    "queue_max_bytes": 16777216,
    "max_payload_bytes": 1048576,
    "max_trace_payload_bytes": 16777216,
    "flush_interval_ms": 200,
    "shutdown_timeout": 5.0,
}
