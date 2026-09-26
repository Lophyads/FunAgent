from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore import load_config
from agentcore.cli import run_cli


def _write_config(path: Path, database_name: str = "state.db") -> None:
    path.write_text(
        f'''\
[agent]
system_prompt = "CLI system"
database_path = "{database_name}"
max_iterations = 3

[retry]
max_attempts = 1
base_delay_seconds = 0
max_delay_seconds = 0
jitter_ratio = 0
max_retry_after_seconds = 0

[[routes]]
name = "primary"
provider = "openai"
model = "test-model"
api_mode = "chat_completions"
credential_ref = "env:CLI_TEST_KEY"
''', encoding="utf-8",
    )


def test_load_config_resolves_database_relative_to_toml(tmp_path):
    config_path = tmp_path / "config" / "agentcore.toml"
    config_path.parent.mkdir()
    _write_config(config_path)

    config = load_config(config_path)

    assert config.database_path == (config_path.parent / "state.db").resolve()
    assert config.routes[0].credential_ref == "env:CLI_TEST_KEY"


def test_load_config_reads_dotenv_from_config_directory(tmp_path, monkeypatch):
    config_path = tmp_path / "nested" / "agentcore.toml"
    config_path.parent.mkdir()
    _write_config(config_path)
    (config_path.parent / ".env").write_text("CLI_TEST_KEY=loaded-from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("CLI_TEST_KEY", raising=False)

    load_config(config_path)

    assert os.environ["CLI_TEST_KEY"] == "loaded-from-dotenv"


def test_dotenv_does_not_override_existing_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    (tmp_path / ".env").write_text("CLI_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("CLI_TEST_KEY", "from-process")

    load_config(config_path)

    assert os.environ["CLI_TEST_KEY"] == "from-process"


def test_cli_chat_can_be_exercised_without_a_real_provider(tmp_path, monkeypatch, capsys):
    from concurrent.futures import Future
    from tools.lifecycle import CleanupReport

    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)

    class FakeGateway:
        def __init__(self, config, **kwargs):
            self.config = config
        def start_blocking(self):
            pass
        def close_blocking(self, **kwargs):
            return CleanupReport()
        def attach_session(self, session_id):
            return None
        def submit_turn(self, text, session_id):
            future = Future()
            future.set_result(SimpleNamespace(
                warnings=[], final_text="CLI answer", status="completed", error=None,
            ))
            return SimpleNamespace(future=future)

    monkeypatch.setattr("agentcore.cli.GatewayRuntime", FakeGateway)
    lines = iter(["hello from cli", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(lines))
    code = run_cli(["chat", "--config", str(config_path), "--session", "cli-session"])
    captured = capsys.readouterr()
    assert code == 0
    assert "session: cli-session" in captured.out
    assert "assistant> CLI answer" in captured.out

def test_config_rejects_legacy_api_key_env(tmp_path):
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'credential_ref = "env:CLI_TEST_KEY"', 'api_key_env = "CLI_TEST_KEY"',
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)

def test_logging_config_resolves_relative_directory(tmp_path):
    config_path = tmp_path / "nested" / "agentcore.toml"
    config_path.parent.mkdir()
    _write_config(config_path)
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write("\n[logging]\ndirectory = \"diagnostics\"\nlevel = \"DEBUG\"\n")

    config = load_config(config_path)

    assert config.logging.directory == (config_path.parent / "diagnostics").resolve()
    assert config.logging.level == "DEBUG"


def test_cli_logs_list_and_filter(tmp_path, capsys):
    import logging
    from log.logging_system import configure_logging, log_context

    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    config = load_config(config_path)
    with configure_logging(config) as handle:
        with log_context(config, "cli-session"):
            logging.getLogger("agentcore.cli_test").info(
                "turn.finalized status=completed"
            )
        assert handle.runtime is not None and handle.runtime.flush(2)

    assert run_cli(["logs", "list", "--config", str(config_path)]) == 0
    assert "agent" in capsys.readouterr().out
    assert run_cli([
        "logs", "agent", "--config", str(config_path),
        "--session", "cli-session",
    ]) == 0
    captured = capsys.readouterr()
    assert "turn.finalized status=completed" in captured.out
    assert "[cli-session]" in captured.out


def test_cli_session_filter_without_existing_logs(tmp_path, capsys):
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)

    code = run_cli([
        "logs", "agent", "--config", str(config_path),
        "--session", "unknown",
    ])

    assert code == 0
    assert capsys.readouterr().out == ""


def test_cli_rejects_legacy_log_options(tmp_path):
    import pytest

    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    for option in ("--trace", "--json"):
        with pytest.raises(SystemExit) as exc:
            run_cli(["logs", "agent", "--config", str(config_path), option])
        assert exc.value.code == 2
