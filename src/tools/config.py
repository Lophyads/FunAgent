from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from agentcore.interfaces import Approver
    from agentcore.models import AgentConfig


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str | None = None
    url: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool = True
    required: bool = False
    connect_timeout_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    requires_approval: bool = True

    @model_validator(mode="after")
    def validate_transport(self):
        if (self.command is None) == (self.url is None):
            raise ValueError("MCP server requires exactly one of command or url")
        if self.command is not None:
            if not self.command.strip():
                raise ValueError("MCP command must not be blank")
            if self.headers:
                raise ValueError("MCP headers are only supported for HTTP")
        else:
            endpoint = urlsplit(self.url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
                raise ValueError("MCP url must be an absolute HTTP(S) URL")
            if self.args or self.env:
                raise ValueError("MCP args/env are only supported for stdio")
        return self


class ToolServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allow_private_web_addresses: bool = False
    allow_private_browser_addresses: bool = False
    artifact_max_bytes: int = Field(default=2_000_000, ge=16_384, le=10_000_000)
    search_timeout_seconds: float = Field(default=30, gt=0, le=300)
    browser_idle_seconds: float = Field(default=900, gt=0, le=86_400)
    clarify_timeout_seconds: float = Field(default=3600, gt=0, le=86_400)
    approval_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    interaction_capacity: int = Field(default=128, ge=1, le=10_000)
    availability_ttl_seconds: float = Field(default=30, gt=0, le=300)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_cwd: Path | None = None
    allowed_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ToolSystemConfig:
    tools: ToolServiceConfig = field(default_factory=ToolServiceConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    approval_store_path: Path | None = None
    approver: Approver | None = None

    @classmethod
    def from_agent_config(cls, config: AgentConfig) -> ToolSystemConfig:
        return cls(config.tools, config.workspace, config.approval_store_path)

    def normalized(self, *, startup_cwd: Path | None = None) -> ToolSystemConfig:
        startup = startup_cwd or Path.cwd().resolve()
        cwd = (startup / (self.workspace.default_cwd or startup).expanduser()).resolve()
        root = (startup / (self.workspace.allowed_root or cwd).expanduser()).resolve()
        if not cwd.is_dir() or not root.is_dir():
            raise ValueError("workspace paths must be existing directories")
        if not cwd.is_relative_to(root):
            raise ValueError("default_cwd is outside allowed_root")
        path = self.approval_store_path
        if path is not None:
            path = (startup / path.expanduser()).resolve()
            if not path.is_file():
                raise ValueError("approval_store_path must be an existing TOML file")
        return replace(
            self, workspace=WorkspaceConfig(default_cwd=cwd, allowed_root=root),
            approval_store_path=path,
        )
