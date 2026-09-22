# FunAgent mini-Hermes

FunAgent 是一个 Python 3.11+ 的同步 Agent 内核。完整 ConversationLoop 在 daemon turn
worker 中运行；异步 API 只负责提交任务并等待 Future，不在业务 EventLoop 上执行
Provider、SQLite、策略、工具或生命周期逻辑。

当前实现包括：

- 不可变 Session Prompt/Toolset Snapshot，Memory recall 只在新 Session bootstrap 时执行。
- Provider Resolver、API Mode Adapter 与单次请求 Transport 三层解耦。
- 显式 Phase orchestrator，Provider retry 不增加 Agent iteration。
- SQLite schema v5：消息、工具执行、模型 attempt 与跨进程 Session Lease 账本。
- 单调 fencing token、独立 heartbeat 线程和 stale-owner 写入拒绝。
- 顺序工具执行、Pydantic 参数校验、guard、approval 与未知副作用恢复语义。
- 隔离的 Observer/EventSink 可观测性路径及集中敏感信息脱敏。

详细设计见
[mini-hermes-development-design.md](docs/mini-hermes-development-design.md)，验收矩阵见
[mini-hermes-test-acceptance-plan.md](docs/mini-hermes-test-acceptance-plan.md)。

## 快速开始

复制配置，创建同目录 `.env`，然后启动同步 CLI：

```powershell
Copy-Item agentcore.toml.example agentcore.toml
Set-Content .env 'OPENAI_API_KEY=your-key'
uv sync --extra dev
uv run agentcore chat --config agentcore.toml
```

`load_config()` 自动加载配置文件同目录的 `.env`，已有进程环境变量优先。route 通过
`credential_ref = "env:OPENAI_API_KEY"` 引用密钥；密钥值不会写入 TOML、Session
Snapshot、SQLite 或观测事件。

CLI 支持 `/new`、`/session` 和 `/exit`。恢复已有会话：

```powershell
uv run agentcore chat --config agentcore.toml --session <session-id>
```

## Python API

同步宿主：

```python
from agentcore import AgentCore, default_tool_catalog, load_config

config = load_config("agentcore.toml")
with AgentCore(config, default_tool_catalog()) as agent:
    result = agent.run_turn_blocking("Calculate 12 * 8")
    print(result.final_text)
```

异步宿主的 facade 不承载状态机：

```python
import asyncio

from agentcore import AgentCore, default_tool_catalog, load_config


async def main() -> None:
    async with AgentCore(load_config("agentcore.toml"), default_tool_catalog()) as agent:
        result = await agent.run_turn("What time is it in Asia/Shanghai?")
        print(result.final_text)


asyncio.run(main())
```

可通过 keyword-only `session_request` 显式创建 Session Snapshot。既有 Session 再传入
不同 request 会 fail closed，不会因当前配置变化替换旧 Prompt 或 Toolset。

## 自定义工具

进程级 `ToolCatalog` 描述已安装实现，`ToolsetResolver` 决定某个 Session 能看到哪些
工具。每个实现必须提供稳定的 `tool_id` 与 `implementation_version`：

```python
from pydantic import BaseModel, Field

from agentcore import ToolCatalog, ToolContext, ToolSpec


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def greet(args: GreetArgs, context: ToolContext):
    return {"message": f"Hello, {args.name}!"}


catalog = ToolCatalog()
catalog.register(ToolSpec(
    tool_id="example.greet",
    implementation_version="1",
    name="greet",
    description="Greet a person by name.",
    args_model=GreetArgs,
    handler=greet,
    side_effects="none",
))
```

`AgentCore` 构造后 Catalog 冻结。异步 handler 会被拒绝；异步服务需要由集成方提供显式
同步适配器。工具超时表示 Core 停止等待，不表示线程被强制终止。已进入只读 handler
后结果为 `unknown/none`；已进入可能有副作用的 handler 后结果为
`unknown/unknown`。未完成工具永不自动重放。

## Provider 扩展

扩展接入点为 `ProviderResolver`、`ApiModeRegistry`、`ApiModeAdapter` 和 `Transport`。
`Transport.send()` 严格执行一次网络发送，SDK 内部 retry 被禁用；重试、Retry-After、
cooldown、fallback 和当前 Turn route 粘滞由 ConversationLoop 统一控制。

## 结果语义

`TurnResult.status == "completed"` 仅表示模型正常结束本轮。业务工具是否成功需结合
`completion_reason`、`tool_outcomes`、`provider_attempts` 和 `route_attempts` 判断。
`context_version` 在 admission 前失败时为 `None`。

## 配置

- `[session]`：新 Session 的 profile、surface、skills、toolsets 与 permissions。
- `[lease]`：TTL、heartbeat 周期和跨进程等待上限。
- `[workers]`：turn/operation daemon worker 数量及有界队列。
- `[timeouts]`：总预算和工具、策略、memory、observer、review、finalize 上限。
- `[[routes]]`：有序 primary/fallback 路由。

MVP 不包含 Gateway、Cron、Subagent、MCP、Delivery Runtime、上下文压缩、多模态或
exactly-once 工具保证。

## 验收

验收命令记录在测试方案中。测试默认使用脚本化 Provider/Adapter/Transport，不访问真实
模型服务。
