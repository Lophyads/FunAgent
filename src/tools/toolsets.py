from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .tools import ToolCatalog, ToolHandler, ToolSpec


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ToolsetRequest:
    profile: str
    surface: str
    platform: str
    enabled_toolsets: tuple[str, ...]
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    tool_id: str
    implementation_version: str
    name: str
    schema_bytes: bytes
    handler: ToolHandler[Any]
    spec: ToolSpec[Any]

@dataclass(frozen=True, slots=True)
class ToolsetSnapshot:
    bindings: tuple[ToolBinding, ...]
    schema_bytes: bytes
    fingerprint: str

    def get(self, name: str) -> ToolBinding | None:
        return next((binding for binding in self.bindings if binding.name == name), None)

    def definitions(self) -> tuple[Mapping[str, Any], ...]:
        decoded = json.loads(self.schema_bytes.decode("utf-8"))
        return tuple(MappingProxyType(item) for item in decoded)

class ToolsetResolver(Protocol):
    def resolve(
        self, request: ToolsetRequest, catalog: ToolCatalog
    ) -> ToolsetSnapshot: ...


class DefaultToolsetResolver:
    def resolve(
        self, request: ToolsetRequest, catalog: ToolCatalog
    ) -> ToolsetSnapshot:
        enabled = frozenset(request.enabled_toolsets)
        specs = [
            spec for spec in catalog.specs()
            if spec.toolsets.intersection(enabled)
            and spec.required_permissions.issubset(request.permissions)
        ]
        return _snapshot_from_specs(specs)


def _snapshot_from_specs(specs: list[ToolSpec[Any]]) -> ToolsetSnapshot:
    ordered = sorted(specs, key=lambda spec: (spec.tool_id, spec.name))
    bindings = tuple(
        ToolBinding(
            tool_id=spec.tool_id,
            implementation_version=spec.implementation_version,
            name=spec.name,
            schema_bytes=canonical_json_bytes(spec.definition()),
            handler=spec.handler,
            spec=spec,
        )
        for spec in ordered
    )
    schema_bytes = canonical_json_bytes([spec.definition() for spec in ordered])
    return ToolsetSnapshot(
        bindings=bindings,
        schema_bytes=schema_bytes,
        fingerprint=hashlib.sha256(schema_bytes).hexdigest(),
    )


__all__ = [
    "DefaultToolsetResolver",
    "ToolBinding",
    "ToolsetRequest",
    "ToolsetResolver",
    "ToolsetSnapshot",
    "canonical_json_bytes",
]
