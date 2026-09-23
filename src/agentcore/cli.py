from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .builtins import default_tool_catalog
from .config import load_config
from log.logging_system import configure_logging
from log.logs_cli import list_logs, tail_logs
from .core import AgentCore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcore", description="Run the AgentCore MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat = subparsers.add_parser("chat", help="start an interactive chat")
    chat.add_argument(
        "--config", type=Path, default=Path("agentcore.toml"), help="path to TOML configuration"
    )
    chat.add_argument("--session", help="resume an existing session id")
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
    return parser


def _chat(config_path: Path, requested_session: str | None) -> int:
    config = load_config(config_path)
    session_id = requested_session or str(uuid.uuid4())
    print(f"session: {session_id}")
    print("commands: /new, /session, /exit")

    with configure_logging(config, mode="cli"), AgentCore(config, default_tool_catalog()) as agent:
        while True:
            try:
                line = input("you> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            command = line.strip().lower()
            if command in {"/exit", "/quit"}:
                return 0
            if command == "/session":
                print(f"session: {session_id}")
                continue
            if command == "/new":
                session_id = str(uuid.uuid4())
                print(f"session: {session_id}")
                continue
            if not line.strip():
                continue

            result = agent.run_turn_blocking(line, session_id=session_id)
            if result.warnings:
                print(
                    f"warning: {len(result.warnings)} diagnostic warning(s)",
                    file=sys.stderr,
                )
            if result.final_text:
                print(f"assistant> {result.final_text}")
            if result.status != "completed":
                code = result.error.code if result.error else "turn_failed"
                print(f"error[{result.status}]: {code}", file=sys.stderr)


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "chat":
            return _chat(args.config, args.session)
        if args.command == "logs":
            config = load_config(args.config)
            if args.name == "list":
                list_logs(config)
            else:
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
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"agentcore: {type(exc).__name__}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
