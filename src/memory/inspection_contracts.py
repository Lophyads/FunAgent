"""Public inspection query values; service identifiers stay in the adapter."""
from dataclasses import dataclass


@dataclass(frozen=True)
class InspectionQuery:
    source_id: str
    dataset_name: str | None = None
    limit: int = 50
    cursor: str | None = None
    data_id: str | None = None
    refresh: bool = False
    max_nodes: int = 300
    depth: int = 1
    seed_node_id: str | None = None
