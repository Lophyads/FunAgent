from __future__ import annotations

from pathlib import Path
import os

import pytest

from agentcore import ModelResponse, load_config
from agentcore.cli import run_cli


def _write_config(path: Path, database_name: str = "state.db") -> None:
    path.write_text(
        f"""
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
provider = "test"
model = "test-model"
api_mode = "chat_completions"
api_key_env = "CLI_TEST_KEY"
""".strip(),
        encoding="utf-8",
    )


def test_load_config_resolves_database_relative_to_toml(tmp_path):
    config_path = tmp_path / "config" / "agentcore.toml"
    config_path.parent.mkdir()
    _write_config(config_path)

    config = load_config(config_path)

    assert config.database_path == (config_path.parent / "state.db").resolve()
    assert config.routes[0].api_mode == "chat_completions"


def test_load_config_reads_dotenv_from_config_directory(tmp_path, monkeypatch):
    config_path = tmp_path / "nested" / "agentcore.toml"
    config_path.parent.mkdir()
    _write_config(config_path)
    (config_path.parent / ".env").write_text(
        "CLI_TEST_KEY=loaded-from-dotenv\n", encoding="utf-8"
    )
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


def test_cli_chat_with_fake_transport(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    monkeypatch.setenv("CLI_TEST_KEY", "super-secret-do-not-persist")

    class FakeOpenAITransport:
        def __init__(self, route):
            self.route = route

        async def complete(self, request):
            return ModelResponse(content="CLI answer", finish_reason="stop")

        async def aclose(self):
            return None

    monkeypatch.setattr("agentcore.core.OpenAIChatTransport", FakeOpenAITransport)
    lines = iter(["hello from cli", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(lines))

    code = run_cli(["chat", "--config", str(config_path), "--session", "cli-session"])

    captured = capsys.readouterr()
    assert code == 0
    assert "session: cli-session" in captured.out
    assert "assistant> CLI answer" in captured.out
    database_bytes = (tmp_path / "state.db").read_bytes()
    assert b"super-secret-do-not-persist" not in database_bytes


def test_config_rejects_unsupported_api_mode(tmp_path):
    config_path = tmp_path / "agentcore.toml"
    _write_config(config_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'api_mode = "chat_completions"', 'api_mode = "responses"'
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_config(config_path)
