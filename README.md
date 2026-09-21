# Fun Agent Core

一个小而完整的 AgentCore MVP，参考 Hermes Agent 的执行链路，但只保留核心能力：

- OpenAI-compatible Chat Completions 与有序 fallback
- 同步 `AgentTurn` 状态机运行在有界 daemon worker 中，不占用业务 EventLoop
- SQLite 追加式会话持久化、schema 迁移和中断恢复
- 固定工具快照、Pydantic 参数校验、guard、approval 与工具执行账本
- 可插拔的同步 memory、middleware 和 review scheduling 接口

## 快速开始

要求 Python 3.11–3.13 和 [uv](https://docs.astral.sh/uv/)。

```powershell
Copy-Item agentcore.toml.example agentcore.toml
Set-Content .env 'OPENAI_API_KEY=your-key'
uv sync --extra dev
uv run agentcore chat --config agentcore.toml
```

`load_config()` 自动加载 `agentcore.toml` 同目录下的 `.env`。已经存在的系统或
PowerShell 环境变量优先，不会被 `.env` 覆盖；`.env` 已加入 `.gitignore`。
密钥只从 `api_key_env` 指定的环境变量读取，不会写入配置快照或 SQLite。

恢复已有会话：

```powershell
uv run agentcore chat --config agentcore.toml --session <session-id>
```

CLI 是同步入口，不创建 EventLoop，支持 `/new`、`/session` 和 `/exit`。

## Python API

异步宿主继续使用兼容 API。`run_turn()` 只提交 worker 并等待 Future，完整 AgentTurn
在 worker 线程执行：

```python
import asyncio

from agentcore import AgentCore, ToolRegistry, load_config, register_builtin_tools


async def main() -> None:
    config = load_config("agentcore.toml")
    tools = register_builtin_tools(ToolRegistry())

    async with AgentCore(config, tools) as agent:
        result = await agent.run_turn("What time is it in Asia/Shanghai?")
        print(result.final_text)


asyncio.run(main())
```

同步程序使用 `run_turn_blocking()`；它同样通过 Dispatcher 执行，不会在调用者线程
直接运行 AgentTurn：

```python
from agentcore import AgentCore, default_tool_registry, load_config

with AgentCore(load_config("agentcore.toml"), default_tool_registry()) as agent:
    result = agent.run_turn_blocking("Calculate 12 * 8")
    print(result.final_text)
```

## 自定义工具

核心扩展协议是同步的。Async Provider、middleware 和 tool handler 会在注册或构造阶段
被拒绝；异步服务应由集成方提供明确的同步适配器。

```python
from pydantic import BaseModel, Field

from agentcore import ToolContext, ToolRegistry, ToolSpec


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def greet(args: GreetArgs, context: ToolContext):
    return {"message": f"Hello, {args.name}!", "session_id": context.session_id}


registry = ToolRegistry()
registry.register(ToolSpec(
    name="greet",
    description="Greet a person by name.",
    args_model=GreetArgs,
    handler=greet,
    side_effects="none",
))
```

工具必须在构造 `AgentCore` 前注册。Agent 初始化时冻结 registry 和 tool schema，
之后注册会抛出 `RuntimeError`。自定义工具默认 `side_effects="possible"`；只有明确只读
的工具才应设为 `none`。

工具 handler 在独立的有界 operation worker 中运行。超时表示 Core 停止等待，并不
表示 Python 线程已被强制终止。已进入可能产生副作用的 handler 后发生超时或取消时，
结果会记录为 `unknown`，迟到结果不会覆盖已持久化的状态，也不会自动重放。

## 结果与恢复语义

`TurnResult.status == "completed"` 只表示模型正常结束本轮，不代表用户业务目标一定成功。
请结合 `completion_reason`、`tool_outcomes`、`provider_attempts` 和 `route_attempts` 判断执行情况。

assistant tool calls 与 `pending` 执行记录会原子落库；每个工具结果也会立即持久化。恢复时：

- 尚未进入 handler 的调用记为 `not_executed/not_applied`。
- 已进入 handler 但没有可靠结果的调用记为 `unknown`。
- 不完整调用永不自动重放。

## 配置和安全边界

`agentcore.toml` 只保存非敏感配置。多个 `[[routes]]` 按顺序组成 primary/fallback 链。
`[workers]` 控制 turn/operation worker 和总队列容量；`[timeouts]` 控制总预算及各扩展点
超时。OpenAI SDK 内置重试被禁用，所有重试与 fallback 由 Core 统一计数和执行。

当前版本仅支持文本、非流式响应和单进程内的同 session FIFO。它不提供 AsyncBridge、
多模态、上下文压缩、插件发现、向量数据库、跨进程 session 锁或 exactly-once 保证。

## 测试

```powershell
uv run pytest
```

测试默认使用脚本化 Fake Transport，不访问真实模型服务。
