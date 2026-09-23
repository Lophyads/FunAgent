from __future__ import annotations

import io
import json
import logging
import multiprocessing
import threading
from pathlib import Path

import pytest

from agentcore import (
    AgentConfig, GatewayRuntime, LoggingConfig, ModelRoute,
    NormalizedModelResponse, ToolCall, configure_logging,
)
from log.logging_system import log_context
from log.logs_cli import _follow, list_logs, tail_logs
from agentcore.observability import ObservabilityHub
from conftest import ScriptedTransport, scripted_provider_stack


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_turn_logs_raw_session_and_milestones(
    config_factory, agent_factory, final_response, tmp_path,
):
    config = config_factory(system_prompt="private-system-prompt")
    with configure_logging(config) as handle:
        with agent_factory(
            config, transports={"primary": ScriptedTransport(final_response)}
        ) as agent:
            result = agent.run_turn_blocking(
                "private-user-body", session_id="caller-chosen-session"
            )
        assert result.status == "completed"
        assert handle.runtime is not None and handle.runtime.flush(2)
    raw = _text(tmp_path / "logs" / "agent.log")
    assert "[caller-chosen-session] agentcore.conversation_loop: turn.started" in raw
    assert "turn.admitted" in raw
    assert "provider.request" in raw
    assert "provider.response" in raw
    assert "turn.finalized" in raw
    assert "turn.ended" in raw
    assert "private-system-prompt" not in raw
    assert "private-user-body" not in raw
    assert "turn.phase_started" not in raw
    out = io.StringIO()
    tail_logs(config, session="caller-chosen-session", component="provider", output=out)
    assert "provider.response" in out.getvalue()
    assert "turn.finalized" not in out.getvalue()


def test_debug_previews_and_redaction(
    config_factory, agent_factory, tmp_path,
):
    config = config_factory().model_copy(update={
        "logging": LoggingConfig(level="DEBUG"),
    })
    transport = ScriptedTransport(
        NormalizedModelResponse(
            tool_calls=[ToolCall(
                id="clock-1", name="current_time",
                arguments='{"timezone":"UTC","password":"private-tool-secret"}',
            )],
            finish_reason="tool_calls",
        ),
        NormalizedModelResponse(content="finished", finish_reason="stop"),
    )
    with configure_logging(config) as handle:
        with agent_factory(config, transports={"primary": transport}) as agent:
            result = agent.run_turn_blocking(
                "hello password:private-user-secret", session_id="debug-session"
            )
        assert result.status == "completed"
        assert handle.runtime is not None and handle.runtime.flush(2)
    raw = _text(tmp_path / "logs" / "agent.log")
    assert "turn.user_preview" in raw
    assert "tool.arguments_preview" in raw
    assert "turn.request_assembled" in raw
    assert "provider.request_messages" in raw
    assert "content_preview" in raw
    assert "private-user-secret" not in raw
    assert "private-tool-secret" not in raw
    assert "turn.phase_started" in raw


def test_tool_message_validation_failure_logs_safe_details(
    config_factory, agent_factory, tmp_path,
):
    config = config_factory()
    transport = ScriptedTransport(NormalizedModelResponse(
        content="",
        tool_calls=[ToolCall(
            id="invalid-content-1",
            name="current_time",
            arguments='{"timezone":"UTC","password":"private-tool-secret"}',
        )],
        finish_reason="tool_calls",
    ))

    with configure_logging(config) as handle:
        with agent_factory(
            config, transports={"primary": transport},
        ) as agent:
            result = agent.run_turn_blocking(
                "trigger invalid tool response", session_id="tool-validation"
            )
        assert handle.runtime is not None and handle.runtime.flush(2)

    agent_log = _text(tmp_path / "logs" / "agent.log")
    error_log = _text(tmp_path / "logs" / "errors.log")
    assert result.status == "failed"
    assert result.completion_reason == "turn_failed"
    assert "tool.round_preparing" in agent_log
    assert "'content_type': 'str'" in agent_log
    assert "'content_length': 0" in agent_log
    assert "'content_preview': ''" in agent_log
    assert "response_content_present=True" in agent_log
    assert "response_content_length=0" in agent_log
    assert "tool.message_validation_failed" in error_log
    assert "assistant tool message must not include text content" in error_log
    assert "turn.unhandled_error" in error_log
    assert "Traceback" in error_log
    assert "private-tool-secret" not in agent_log + error_log


def test_root_logger_routes_and_restores(config_factory, tmp_path):
    config = config_factory()
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    old_factory = logging.getLogRecordFactory()
    logger = logging.getLogger("external.extension")
    with configure_logging(config) as handle:
        assert logging.getLogRecordFactory() is not old_factory
        with log_context(config, "plain-session"):
            logger.info("plain event password:secret-value postgresql://user:private-pass@db")
        logger.warning("unbound event")
        assert handle.runtime is not None and handle.runtime.flush(2)
    raw = _text(tmp_path / "logs" / "agent.log")
    assert "[plain-session] external.extension: plain event" in raw
    assert "secret-value" not in raw
    assert "private-pass" not in raw
    assert "unbound event" in raw
    assert logging.getLogRecordFactory() is old_factory
    assert root.handlers == old_handlers
    assert root.level == old_level


def test_multi_directory_context_isolation(config_factory, tmp_path):
    first = config_factory()
    second = first.model_copy(update={
        "database_path": tmp_path / "second" / "agentcore.db",
    })
    logger = logging.getLogger("agentcore.test_module")
    with configure_logging(first) as one, configure_logging(second) as two:
        with log_context(first, "first-session"):
            logger.info("first event")
        with log_context(second, "second-session"):
            logger.info("second event")
        logger.info("ambiguous event")
        assert one.runtime is not None and one.runtime.flush(2)
        assert two.runtime is not None and two.runtime.flush(2)
    first_raw = _text(tmp_path / "logs" / "agent.log")
    second_raw = _text(tmp_path / "second" / "logs" / "agent.log")
    assert "first event" in first_raw and "second event" not in first_raw
    assert "second event" in second_raw and "first event" not in second_raw
    assert "ambiguous event" not in first_raw + second_raw


def test_duplicate_configuration_shares_writer(config_factory):
    config = config_factory()
    first = configure_logging(config)
    second = configure_logging(config)
    runtime = first.runtime
    try:
        assert runtime is not None and runtime is second.runtime
        assert first.close()
        with log_context(config, "shared"):
            logging.getLogger("agentcore.test").info("still open")
        assert runtime.flush(2)
    finally:
        second.close()
    assert not runtime._writer.is_alive()


def test_observability_publish_does_not_log(config_factory, tmp_path):
    config = config_factory()
    with configure_logging(config) as handle:
        hub = ObservabilityHub()
        try:
            hub.publish("turn.admitted", trace_id="event-only", session_id="event-only")
            assert handle.runtime is not None and handle.runtime.flush(2)
        finally:
            hub.close(2)
    assert not (tmp_path / "logs" / "agent.log").exists()


def test_gateway_logs_delivery_with_session(
    config_factory, final_response, tmp_path,
):
    config = config_factory()
    resolver, api_modes, _ = scripted_provider_stack(
        config, {"primary": ScriptedTransport(final_response)}
    )

    class Adapter:
        destination = "test:logs"
        def send(self, *, obligation_id: str, destination: str, text: str) -> bool:
            return True

    with GatewayRuntime(
        config, Adapter(), provider_resolver=resolver, api_modes=api_modes
    ) as gateway:
        result = gateway.run_turn_blocking("private-input", session_id="gateway-session")
        assert result.status == "completed"
    raw = _text(tmp_path / "logs" / "gateway.log")
    assert "runtime.ready" in raw
    assert "[gateway-session] gateway.delivery: delivery.delivered" in raw
    assert "private-input" not in raw


def test_legacy_jsonl_is_archived_and_ignored(config_factory, tmp_path):
    config = config_factory()
    directory = tmp_path / "logs"
    directory.mkdir()
    legacy = '{"schema":1,"event":"old-secret"}\n'
    (directory / "agent.log").write_text(legacy, encoding="utf-8")
    (directory / "agent.log.1").write_text(legacy, encoding="utf-8")
    with configure_logging(config) as handle:
        with log_context(config, "new-session"):
            logging.getLogger("agentcore.test").info("new entry")
        assert handle.runtime is not None and handle.runtime.flush(2)
    batches = list((directory / "legacy-jsonl").iterdir())
    assert len(batches) == 1
    assert (batches[0] / "agent.log").read_text(encoding="utf-8") == legacy
    assert (batches[0] / "agent.log.1").read_text(encoding="utf-8") == legacy
    out = io.StringIO()
    tail_logs(config, output=out)
    assert "new entry" in out.getvalue()
    assert "old-secret" not in out.getvalue()


def test_writer_failure_does_not_change_turn(
    config_factory, agent_factory, final_response, monkeypatch,
):
    config = config_factory()
    with configure_logging(config) as handle:
        runtime = handle.runtime
        assert runtime is not None

        class FailingHandler:
            def emit(self, record):
                raise OSError("disk full")
            def close(self):
                pass

        monkeypatch.setattr(runtime, "_handler_for", lambda name: FailingHandler())
        with agent_factory(
            config, transports={"primary": ScriptedTransport(final_response)}
        ) as agent:
            result = agent.run_turn_blocking("hello", session_id="write-error")
        assert runtime.flush(2)
        assert runtime.write_errors > 0
    assert result.status == "completed"


def test_queue_saturation_and_bounded_close(config_factory, monkeypatch):
    config = config_factory().model_copy(update={
        "logging": LoggingConfig(queue_capacity=16),
    })
    handle = configure_logging(config)
    runtime = handle.runtime
    assert runtime is not None
    entered = threading.Event()
    release = threading.Event()

    class BlockingHandler:
        def emit(self, record):
            entered.set()
            release.wait(3)
        def close(self):
            pass

    monkeypatch.setattr(runtime, "_handler_for", lambda name: BlockingHandler())
    logger = logging.getLogger("agentcore.test")
    with log_context(config, "queue-session"):
        logger.info("first")
        assert entered.wait(2)
        for index in range(100):
            logger.info("queued %s", index)
    assert runtime.dropped_records > 0
    assert not handle.close(timeout=0.01)
    release.set()
    runtime._writer.join(3)
    assert not runtime._writer.is_alive()


def test_follow_reopens_after_rotation(tmp_path, monkeypatch):
    import log.logs_cli as logs_cli

    path = tmp_path / "agent.log"
    first = "2026-09-23 00:00:00,000 INFO agentcore.test: first\n"
    second = "2026-09-23 00:00:01,000 INFO agentcore.test: second\n"
    path.write_text(first, encoding="utf-8")
    output = io.StringIO()
    calls = 0

    class StopFollow(Exception):
        pass

    def rotate(_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            path.rename(tmp_path / "agent.log.1")
            path.write_text(second, encoding="utf-8")
        else:
            raise StopFollow

    monkeypatch.setattr(logs_cli.time, "sleep", rotate)
    filters = {"level": None, "session": None, "component": None, "cutoff": None}
    with pytest.raises(StopFollow):
        _follow(path, filters, output=output)
    assert "second" in output.getvalue()


def _write_logs_from_process(directory: str, barrier) -> None:
    root = Path(directory)
    config = AgentConfig(
        database_path=root / "state.db",
        logging=LoggingConfig(
            directory=root / "logs", queue_capacity=10000,
            agent_max_size_mb=1, agent_backup_count=2,
        ),
        routes=(ModelRoute(
            name="primary", provider="test", model="test",
            credential_ref="env:TEST_KEY",
        ),),
    )
    barrier.wait(timeout=15)
    with configure_logging(config) as handle:
        with log_context(config, "multi-process"):
            logger = logging.getLogger("agentcore.process")
            for index in range(4000):
                logger.info("record %s %s", index, "x" * 256)
        assert handle.runtime is not None and handle.runtime.flush(30)


@pytest.mark.multiprocess
def test_two_processes_rotate_valid_text_lines(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(target=_write_logs_from_process, args=(str(tmp_path), barrier))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
    assert all(process.exitcode == 0 for process in processes)
    paths = sorted((tmp_path / "logs").glob("agent.log*"))
    assert any(path.name == "agent.log.1" for path in paths)
    assert len(paths) <= 3
    for path in paths:
        if path.name.endswith(".lock"):
            continue
        assert path.stat().st_size <= 1_050_000
        assert all(
            " INFO [multi-process] agentcore.process: record " in line
            for line in path.read_text(encoding="utf-8").splitlines()
        )



def test_redaction_failure_drops_record(config_factory, monkeypatch, tmp_path):
    import log.logging_system as logging_system

    config = config_factory()
    with configure_logging(config) as handle:
        runtime = handle.runtime
        assert runtime is not None
        monkeypatch.setattr(
            logging_system, "_redact_text",
            lambda value: (_ for _ in ()).throw(RuntimeError("redactor failed")),
        )
        with log_context(config, "sensitive-session"):
            logging.getLogger("agentcore.test").warning("password:secret")
        assert runtime.flush(2)
        assert runtime.redaction_errors == 1
    assert "secret" not in _text(tmp_path / "logs" / "agent.log")


def test_migration_failure_preserves_legacy_file(
    config_factory, monkeypatch, tmp_path,
):
    import log.logging_system as logging_system

    config = config_factory()
    directory = tmp_path / "logs"
    directory.mkdir()
    old = directory / "agent.log"
    old.write_text('{"schema":1,"event":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(
        logging_system, "_archive_legacy",
        lambda directory: (_ for _ in ()).throw(OSError("archive unavailable")),
    )
    handle = configure_logging(config)
    assert handle.runtime is None
    assert old.read_text(encoding="utf-8") == '{"schema":1,"event":"old"}\n'
    listed = io.StringIO()
    list_logs(config, output=listed)
    assert "agent\t0 file(s)" in listed.getvalue()


def test_multiline_record_filtered_as_one(config_factory, tmp_path):
    config = config_factory()
    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "agent.log").write_text(
        "2026-09-23 00:00:00,000 ERROR [one] agentcore.test: failed\n"
        "Traceback (most recent call last):\n"
        "  RuntimeError: redacted\n"
        "2026-09-23 00:00:01,000 INFO [two] agentcore.test: done\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    tail_logs(config, level="ERROR", session="one", output=out)
    assert "Traceback" in out.getvalue()
    assert "done" not in out.getvalue()


def test_operation_worker_inherits_log_context(
    config_factory, agent_factory, final_response, tmp_path,
):
    config = config_factory()
    with configure_logging(config) as handle:
        with agent_factory(
            config, transports={"primary": ScriptedTransport(final_response)}
        ) as agent:
            with log_context(config, "operation-session"):
                agent.call_operation(
                    lambda: logging.getLogger("extension.tool").info("from worker"),
                    timeout=2, cancel_event=None, deadline=None,
                )
        assert handle.runtime is not None and handle.runtime.flush(2)
    assert "[operation-session] extension.tool: from worker" in _text(
        tmp_path / "logs" / "agent.log"
    )
