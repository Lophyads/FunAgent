# FunAgent

FunAgent 是一个 Python 3.11+ 的同步 Agent 内核。完整 ConversationLoop 在 daemon turn
worker 中运行；异步 API 只负责提交任务并等待 Future，不在业务 EventLoop 上执行
Provider、SQLite、策略、工具或生命周期逻辑。

当前实现包括：

- Session 仅冻结 Prompt；工具定义在每个 Turn 开始时重新解析，Memory recall 只在新 Session bootstrap 时执行。
- Provider Resolver、API Mode Adapter 与单次请求 Transport 三层解耦。
- 显式 Phase orchestrator，Provider retry 不增加 Agent iteration。
- SQLite schema v8：Session、消息、Session Lease 和投递义务四张业务表。
- 单调 fencing token、独立 heartbeat 线程和 stale-owner 写入拒绝。
- 顺序工具执行、Pydantic 参数校验、guard、approval 与未知副作用恢复语义。
- GatewayRuntime 启动恢复、内部恢复 Turn、Delivery Runtime 和 inbound gate。
- 隔离的 Observer/EventSink 可观测性路径及集中敏感信息脱敏。

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

## 诊断日志

CLI 和 Gateway 默认将脱敏的诊断日志写到配置目录下的 `logs/`：
`agent.log` 记录普通执行，`errors.log` 记录 WARNING 及以上，
Gateway 还写 `gateway.log`。文件按大小轮转；日志故障不会改变 Turn 结果。
主链路记录 Turn 开始与结束、Provider 尝试和响应、工具轮次与单个工具结果；
DEBUG 级别另记录请求组装时的消息和工具数量，以及相位边界。
关键诊断由各模块的具名 logger 显式写入；生命周期事件的发布不会自动生成日志。
`[logging]` 可调整级别、目录、队列容量与各文件大小及备份数。

```powershell
uv run agentcore logs list --config agentcore.toml
uv run agentcore logs agent --config agentcore.toml -n 100 --level WARNING
uv run agentcore logs agent --config agentcore.toml --session <session-id> --since 1h
uv run agentcore logs gateway --config agentcore.toml -f
```

日志查看还支持按组件筛选。原始 Session ID 写入文本日志；默认 INFO 只记录执行摘要。
DEBUG 可记录截断并脱敏的用户输入和工具参数预览，启用时应限制日志目录访问权限。
旧 JSONL 日志首次启动时归档到 logs/legacy-jsonl/，查看命令只读取新文本日志。
嵌入 Python 的 `AgentCore` 不自动配置文件日志；需要时先调用
`configure_logging(config)` 并在关闭 AgentCore 后关闭返回的 handle。

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

可通过 keyword-only `session_request` 指定新 Session 的身份及当前工具权限。
既有 Session 的 profile/surface/platform 不允许改变，Prompt 保持原样；当前配置和
Catalog 的工具变更只影响后续 Turn。

## Gateway 与回复投递

有外部投递目标时使用 `GatewayRuntime`。Adapter 必须以确定性的
`obligation_id` 作为幂等键；仅在目标确认成功后返回 `True`。未确认回复保留在
SQLite 中，下一次启动先重试投递，再安排需要模型继续处理的 Session。

```python
from agentcore import GatewayRuntime, load_config


class ChatAdapter:
    destination = "chat:room"

    def __init__(self, client):
        self.client = client

    def send(self, *, obligation_id: str, destination: str, text: str) -> bool:
        response = self.client.send_text(
            destination, text, idempotency_key=obligation_id
        )
        return response.acknowledged


with GatewayRuntime(load_config("agentcore.toml"), ChatAdapter(client)) as gateway:
    result = gateway.run_turn_blocking("Hello")
```

普通 `AgentCore` 仍返回 `TurnResult` 给调用方；它没有外部投递目标时不会创建
delivery obligation。`GatewayRuntime.wait_for_recovery()` 可用作启动恢复的等待屏障。

## 自定义工具

进程级 `ToolCatalog` 描述可用实现，`ToolsetResolver` 决定每个 Turn 能看到哪些
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

`AgentCore` 构造时冻结初始 Catalog；调用 `agent.update_tools(specs)` 可原子发布新定义，
仅影响后续 Turn，进行中的 Turn 继续使用已解析的工具快照。Session 只冻结 system prompt。
异步 handler 会被拒绝；异步服务需要由集成方提供显式
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

SQLite v8 仅包含 `sessions`、`messages`、`session_leases` 和
`delivery_obligations` 四张业务表。历史顺序由 `messages.id` 决定；崩溃恢复从
Session 尾消息和过期 lease 判断。v8 不迁移旧库，升级前须停止旧进程并显式删除旧
SQLite 文件及其 WAL/SHM sidecar；旧会话数据不可恢复。

## 配置

- `[session]`：新 Session 的 profile、surface、skills、toolsets 与 permissions。
- `[lease]`：TTL、heartbeat 周期和跨进程等待上限。
- `[workers]`：turn/operation daemon worker 数量及有界队列。
- `[timeouts]`：总预算和工具、策略、memory、observer、review、finalize 上限。
- `[[routes]]`：有序 primary/fallback 路由。

当前版本不包含 HTTP Gateway 服务、Cron、Subagent、MCP、上下文压缩、多模态或
exactly-once 工具保证。

## 验收

验收命令记录在测试方案中。测试默认使用脚本化 Provider/Adapter/Transport，不访问真实
模型服务。
