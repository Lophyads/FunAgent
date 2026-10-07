#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
简单的 MCP Client：连接 Modular RAG MCP Server，调用 query_knowledge_hub 并显示响应。

用法（在项目根目录，用项目 .venv 跑）：
  # 列出所有工具
  python scripts/mcp_client.py --list-tools

  # 单次查询
  python scripts/mcp_client.py --query "RRF 融合是怎么做的" --top-k 5 --collection mmmarco_eval

  # 交互模式（循环输入问题，输入 exit 退出）
  python scripts/mcp_client.py --collection mmmarco_eval
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _server_python() -> str:
    """优先用项目 .venv 的 python（依赖齐全），否则回退当前解释器。"""
    if sys.platform == "win32":
        venv_py = _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = _REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def _server_params():
    """构造 stdio 方式的 MCP server 启动参数。"""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=_server_python(),
        args=["-m", "src.mcp_server.server"],
        cwd=str(_REPO_ROOT),
        # 不传 env：继承当前环境；server 会自己加载 .env
    )


def _print_tool_result(result) -> None:
    """打印 CallToolResult 的内容。"""
    if getattr(result, "isError", False):
        print("[ERROR] 工具返回错误：")
    for content in result.content:
        if hasattr(content, "text"):
            print(content.text)
        else:
            print(content)
    print()


async def list_tools() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("=" * 60)
            print(f"可用工具（{len(tools.tools)} 个）")
            print("=" * 60)
            for t in tools.tools:
                print(f"  - {t.name}")
                print(f"    描述: {t.description}")
                # 打印参数
                schema = getattr(t, "inputSchema", None) or {}
                props = schema.get("properties", {})
                required = schema.get("required", [])
                if props:
                    for pname, pinfo in props.items():
                        mark = " *" if pname in required else ""
                        ptype = pinfo.get("type", "?")
                        print(f"    {pname} ({ptype}){mark}: {pinfo.get('description', '')}")
                print()


async def run_query(query: str, top_k: int, collection: str | None) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    args: dict = {"query": query, "top_k": top_k}
    if collection:
        args["collection"] = collection

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("=" * 60)
            print(f"查询: {query}")
            if collection:
                print(f"集合: {collection}")
            print("=" * 60)
            result = await session.call_tool("query_knowledge_hub", args)
            _print_tool_result(result)


async def interactive(collection: str | None) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("已连接 MCP server。输入问题回车查询，输入 exit 退出。")
            while True:
                try:
                    query = input("\n> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not query:
                    continue
                if query.lower() in ("exit", "quit", "q"):
                    break
                args: dict = {"query": query, "top_k": 5}
                if collection:
                    args["collection"] = collection
                result = await session.call_tool("query_knowledge_hub", args)
                _print_tool_result(result)
    print("已退出。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Modular RAG MCP Client")
    ap.add_argument("--list-tools", action="store_true", help="列出所有工具")
    ap.add_argument("--query", "-q", help="查询问题（不传则进入交互模式）")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--collection", "-c", default=None, help="限定 collection（如 mmmarco_eval）")
    args = ap.parse_args()

    try:
        if args.list_tools:
            asyncio.run(list_tools())
        elif args.query:
            asyncio.run(run_query(args.query, args.top_k, args.collection))
        else:
            asyncio.run(interactive(args.collection))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
