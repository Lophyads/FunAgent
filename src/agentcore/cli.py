from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Sequence

from gateway import GatewayRuntime
from log.logging_system import configure_logging
from tools.config import ToolSystemConfig
from tools.lifecycle import SessionResourcesBusyError, remaining
from tools.runtime import initialize_tool_system, shutdown_tool_system

from .config import load_config
from .interaction import InteractionRequest, Question
from .models import TurnResult
from .workers import TurnSubmission


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcore", description="FunAgent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat = subparsers.add_parser("chat", help="chat through an in-process Gateway")
    chat.add_argument("--config", type=Path, default=Path("agentcore.toml"))
    chat.add_argument("--session", help="resume an existing session")
    logs = subparsers.add_parser("logs", help="view local diagnostic logs")
    logs.add_argument("name", nargs="?", default="agent",
                      choices=("agent", "errors", "gateway", "list"))
    logs.add_argument("--config", type=Path, default=Path("agentcore.toml"))
    logs.add_argument("-n", type=int, default=100, dest="count")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--level")
    logs.add_argument("--session")
    logs.add_argument("--component")
    logs.add_argument("--since")
    tools = subparsers.add_parser("tools", help="diagnose optional tool backends")
    tools.add_argument("action", choices=("doctor",))
    tools.add_argument("--config", type=Path, default=Path("agentcore.toml"))
    return parser


def _print_result(result: TurnResult) -> None:
    if result.warnings:
        print(f"warning: {len(result.warnings)} diagnostic warning(s)", file=sys.stderr)
    if result.final_text:
        print(f"assistant> {result.final_text}")
    if result.status != "completed":
        code = result.error.get("code", "turn_failed") if result.error else "turn_failed"
        print(f"error[{result.status}]: {code}", file=sys.stderr)


def _pump(
    gateway: GatewayRuntime, submission: TurnSubmission[TurnResult], session_id: str,
) -> None:
    try:
        while not submission.future.done():
            request = gateway.next_interaction(session_id=session_id, timeout=0.1)
            if request is not None:
                _answer_interaction(gateway, request)
        _print_result(submission.future.result())
    except (KeyboardInterrupt, EOFError):
        submission.cancel()
        gateway.detach_session(session_id)
        raise


def _cleanup_step(name, action, deadline: float) -> None:
    try:
        report = action(timeout=remaining(deadline))
        for issue in getattr(report, "issues", ()):
            print(
                f"warning: {name} cleanup incomplete: {issue.resource}/{issue.error_code}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"warning: {name} cleanup failed: {type(exc).__name__}", file=sys.stderr)


def _chat(args) -> int:
    config = load_config(args.config)
    logging_handle = configure_logging(config)
    gateway = None
    initialized = False
    try:
        initialize_tool_system(ToolSystemConfig.from_agent_config(config))
        initialized = True
        gateway = GatewayRuntime(
            config, interaction_mode="poll", recovery_policy="attached",
        )
        gateway.start_blocking()
        return _chat_loop(gateway, args.session)
    finally:
        deadline = time.monotonic() + config.timeouts.finalize_seconds
        if gateway is not None:
            _cleanup_step("gateway", gateway.close_blocking, deadline)
        if initialized:
            _cleanup_step("tools", shutdown_tool_system, deadline)
        _cleanup_step("logging", logging_handle.close, deadline)


def _chat_loop(gateway: GatewayRuntime, requested_session: str | None) -> int:
    session_id = requested_session or str(uuid.uuid4())
    print(f"session: {session_id}")
    print("commands: /new, /session, /exit; Ctrl+C cancels the active turn")
    try:
        recovery = gateway.attach_session(session_id)
        if recovery is not None:
            _pump(gateway, recovery, session_id)
        while True:
            line = input("you> ")
            command = line.strip().lower()
            if command in {"/exit", "/quit"}:
                return 0
            if command == "/session":
                print(f"session: {session_id}")
                continue
            if command == "/new":
                session_id = _new_session(gateway, session_id)
                continue
            if line.strip():
                _pump(gateway, gateway.submit_turn(line, session_id), session_id)
    except EOFError:
        print()
        return 0
    except KeyboardInterrupt:
        print()
        return 130


def _new_session(gateway: GatewayRuntime, current_session: str) -> str:
    try:
        report = gateway.reset_session_resources(current_session)
    except SessionResourcesBusyError:
        print("session still has running tools; retry /new later", file=sys.stderr)
        return current_session
    if not report.completed:
        print("session resources are still closing; retry /new", file=sys.stderr)
        return current_session
    gateway.detach_session(current_session)
    session_id = str(uuid.uuid4())
    recovery = gateway.attach_session(session_id)
    print(f"session: {session_id}")
    if recovery is not None:
        _pump(gateway, recovery, session_id)
    return session_id


def _read_answer(question: Question) -> str | list[str]:
    print(f"\nquestion> {question.question}")
    for index, choice in enumerate(question.choices, start=1):
        suffix = " (Recommended)" if index == 1 and len(question.choices) > 1 else ""
        print(f"  {index}. {choice}{suffix}")
    text = input("answer> ").strip()

    def choice(value):
        if value.isdecimal() and 1 <= int(value) <= len(question.choices):
            return question.choices[int(value) - 1]
        return value

    if question.multi_select:
        return [choice(item.strip()) for item in text.split(",") if item.strip()]
    return choice(text)


def _answer_interaction(gateway: GatewayRuntime, request: InteractionRequest) -> None:
    def pending() -> bool:
        return any(
            r.request_id == request.request_id
            for r in gateway.pending_interactions(request.session_id)
        )

    while pending():
        if request.kind == "approval":
            payload = request.payload
            print(f"\napproval> {payload.tool_name} [{payload.scope}]")
            print("choices: " + ", ".join(payload.choices))
            response = {"decision": input("approval> ").strip().lower()}
        else:
            answers = {}
            for question in request.payload.questions:
                if not pending():
                    break
                answers[question.qid] = _read_answer(question)
            response = {"answers": answers}
        if gateway.resolve_interaction(request.session_id, request.request_id, response):
            return
        if pending():
            print("invalid response; please try again", file=sys.stderr)
    print("request expired or cancelled; response was not accepted", file=sys.stderr)


def _show_logs(args: argparse.Namespace) -> int:
    from .config import load_config
    from log.logs_cli import list_logs, tail_logs

    config = load_config(args.config)
    if args.name == "list":
        list_logs(config)
        return 0
    try:
        tail_logs(
            config, args.name, count=args.count, follow=args.follow,
            level=args.level, session=args.session,
            component=args.component, since=args.since,
        )
    except ValueError as exc:
        print(f"agentcore logs: {exc}", file=sys.stderr)
        return 2
    return 0


def _diagnose_tools(config_path: Path) -> int:
    from .config import load_config
    from tools.config import ToolSystemConfig
    from tools.browser_backend import AgentBrowserBackend
    from tools.computer_backend import CuaCliBackend

    config = ToolSystemConfig.from_agent_config(load_config(config_path)).normalized()
    print(json.dumps({
        "web_search": importlib.util.find_spec("ddgs") is not None,
        "browser": AgentBrowserBackend.available(),
        "computer": CuaCliBackend.available(refresh=True),
        "workspace": str(config.workspace.default_cwd),
    }, ensure_ascii=False, indent=2))
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "chat":
            return _chat(args)
        if args.command == "logs":
            return _show_logs(args)
        if args.command == "tools":
            return _diagnose_tools(args.config)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"agentcore: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
