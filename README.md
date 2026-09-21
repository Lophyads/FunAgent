# Fun Agent Core

一个小而完整的 AgentCore MVP，参考 Hermes Agent 的执行链路，但只保留核心能力：

- OpenAI-compatible Chat Completions 与有序 fallback
- Async-first 模型/工具循环
- SQLite 追加式会话持久化和崩溃恢复
- 固定工具快照、Pydantic 参数校验、guard 与 approval
- 可插拔的 memory、middleware 和 background review 接口

## 快速开始

要求 Python 3.11–3.13 和 [uv](https://docs.astral.sh/uv/)。

```powershell
Copy-Item agentcore.toml.example agentcore.toml
Set-Content .env 'OPENAI_API_KEY=your-key'
uv sync --extra dev
uv run agentcore chat --config agentcore.toml
```

`load_config()` 会自动加载 `agentcore.toml` 同目录下的 `.env`。已存在的系统或
PowerShell 环境变量优先，不会被 `.env` 覆盖；`.env` 已加入 `.gitignore`。

恢复已有会话：

```powershell
uv run agentcore chat --config agentcore.toml --session <session-id>
```

CLI 支持 `/new`、`/session` 和 `/exit`。

## Python API

```python
import asyncio

from agentcore import AgentCore, ToolRegistry, load_config, register_builtin_tools


async def main() -> None:
    config = load_config("agentcore.toml")
    tools = ToolRegistry()
    register_builtin_tools(tools)

    async with AgentCore(config, tools) as agent:
        result = await agent.run_turn("What time is it in Asia/Shanghai?")
        print(result.final_text)


asyncio.run(main())
```

工具必须在构造 `AgentCore` 前注册。Agent 初始化时会冻结 registry 和 tool schema；之后注册会抛出 `RuntimeError`。

## 自定义工具

```python
from pydantic import BaseModel, Field

from agentcore import ToolContext, ToolRegistry, ToolSpec


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


async def greet(args: GreetArgs, context: ToolContext):
    return {"message": f"Hello, {args.name}!", "session_id": context.session_id}


registry = ToolRegistry()
registry.register(ToolSpec(
    name="greet",
    description="Greet a person by name.",
    args_model=GreetArgs,
    handler=greet,
))
```

设置 `requires_approval=True` 的工具只有在配置了 `Approver` 且返回允许时才会执行。工具异常、拒绝和参数错误都会成为结构化 `role=tool` 结果，不会破坏消息序列。

## 配置和安全边界

`agentcore.toml` 只保存非敏感配置。每条 route 使用 `api_key_env` 指向环境变量；变量既可由系统提供，也可来自配置文件同目录的 `.env`。密钥不会写入 SQLite。多个 `[[routes]]` 按顺序组成 primary/fallback 链。

当前版本仅支持文本、非流式响应和单进程内的同 session 串行 turn。它不包含多模态、上下文压缩、插件发现、向量数据库、真实后台 worker 或 HTTP 服务。

## 测试

```powershell
uv run pytest
```

测试默认使用脚本化 Fake Transport，不访问真实模型服务。
