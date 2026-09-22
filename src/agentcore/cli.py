from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .builtins import default_tool_catalog
from .config import load_config
from .core import AgentCore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcore", description="Run the AgentCore MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    chat = subparsers.add_parser("chat", help="start an interactive chat")
    chat.add_argument(
        "--config", type=Path, default=Path("agentcore.toml"), help="path to TOML configuration"
    )
    chat.add_argument("--session", help="resume an existing session id")
    return parser


def _chat(config_path: Path, requested_session: str | None) -> int:
    config = load_config(config_path)
    session_id = requested_session or str(uuid.uuid4())
    print(f"session: {session_id}")
    print("commands: /new, /session, /exit")

    with AgentCore(config, default_tool_catalog()) as agent:
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
            for warning in result.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if result.final_text:
                print(f"assistant> {result.final_text}")
            if result.status != "completed":
                message = result.error.message if result.error else "turn failed"
                print(f"error[{result.status}]: {message}", file=sys.stderr)


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "chat":
            return _chat(args.config, args.session)
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
