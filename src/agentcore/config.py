from __future__ import annotations

import tomllib
from pathlib import Path

from dotenv import load_dotenv

from .models import AgentConfig


def load_config(path: str | Path) -> AgentConfig:
    config_path = Path(path).expanduser().resolve()
    # Keep secrets beside the selected config instead of depending on the
    # process working directory. Explicit process variables always win.
    load_dotenv(config_path.parent / ".env", override=False)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    agent = raw.get("agent", {})
    if not isinstance(agent, dict):
        raise ValueError("[agent] must be a TOML table")
    payload = {
        **agent,
        "retry": raw.get("retry", {}),
        "workers": raw.get("workers", {}),
        "timeouts": raw.get("timeouts", {}),
        "lease": raw.get("lease", {}),
        "logging": raw.get("logging", {}),
        "session_defaults": raw.get("session", {}),
        "tools": raw.get("tools", {}),
        "workspace": raw.get("workspace", {}),
        "routes": raw.get("routes", []),
    }
    config = AgentConfig.model_validate(payload)
    if not config.database_path.is_absolute():
        config = config.model_copy(
            update={"database_path": (config_path.parent / config.database_path).resolve()}
        )
    if not config.logging.directory.is_absolute():
        logging_config = config.logging.model_copy(
            update={"directory": (config_path.parent / config.logging.directory).resolve()}
        )
        config = config.model_copy(update={"logging": logging_config})

    workspace = config.workspace.model_copy(update={
        name: (config_path.parent / value).resolve()
        for name in ("default_cwd", "allowed_root")
        if (value := getattr(config.workspace, name)) is not None
    })
    return config.model_copy(update={
        "approval_store_path": config_path, "workspace": workspace,
    })
