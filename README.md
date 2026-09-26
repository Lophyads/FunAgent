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
- Web 搜索/提取、文本 browser、AX-only computer_use 与跨 CLI/Gateway 的 clarify/审批交互。

## 快速开始

CLI 在同一进程内通过 GatewayRuntime 使用 AgentCore，无需启动独立服务：

```powershell
Copy-Item agentcore.toml.example agentcore.toml
uv sync --extra dev
uv run agentcore chat --config agentcore.toml
# 恢复已有 Session
uv run agentcore chat --config agentcore.toml --session <session-id>
```

在配置文件同目录的 `.env` 中设置模型密钥。load_config() 自动加载该文件，
已有环境变量优先；route 通过 `credential_ref = "env:OPENAI_API_KEY"` 引用密钥。

CLI 支持 `/new`、`/session`、`/exit`。主线程消息泵负责 clarify 和审批输入，
同步 AgentTurn 在 daemon worker 中运行。交互期间 EOF 或 Ctrl+C 会取消当前任务；
退出时进程入口依次关闭自有 Gateway、共享工具服务和日志。
`/new` 只有在旧 Session 资源重置成功后才切换，不删除历史或永久授权。

默认工具集会纳入可用的 web_search、web_extract、browser 九个文本动作、
computer_use 和 clarify。搜索使用隔离的 DDGS 子进程；长网页写入工作区
.funagent/artifacts/，可通过 read_file 阅读。browser 需显式安装兼容的
agent-browser，桌面工具需显式安装并启动 cua-driver。FunAgent 不自动安装或
升级外部程序；不可用工具会从当轮模型 schema 隐藏。computer_use 首版只提供
文本 AX capture，拒绝 SOM/vision 和图像型 capture_after。默认拒绝 Web/browser
访问本地及私有地址。

可运行 uv run agentcore tools doctor --config agentcore.toml 查看各后端是否可用
；该命令不下载或升级程序。

## 诊断日志

CLI 在进程入口配置脱敏的诊断日志，默认写到配置目录下的 `logs/`：
`agent.log` 记录普通执行，`errors.log` 记录 WARNING 及以上，
平台 Gateway 宿主可使用 `configure_logging(config, mode="gateway")`，同时写入 `gateway.log`。文件按大小轮转；日志故障不会改变 Turn 结果。
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
DEBUG 仅保留内容长度和已隐藏的预览占位符，不记录用户输入、网页正文、
工具参数或人工答复。仍应限制日志目录访问权限。
旧 JSONL 日志首次启动时归档到 logs/legacy-jsonl/，查看命令只读取新文本日志。
嵌入 Python 的 `AgentCore` 不自动配置文件日志；需要时先调用
`configure_logging(config)` 并在关闭 AgentCore 后关闭返回的 handle。

## Python API

进程入口显式初始化工具系统，再创建 Agent。初始化时自动发现一次 `tools/*_tool.py`；
相同配置可重复初始化，配置冲突会报错，关闭后不能再次初始化。

```python
from agentcore import (
    AgentCore, ToolSystemConfig, load_config,
    initialize_tool_system, shutdown_tool_system,
)

config = load_config("agentcore.toml")
initialize_tool_system(ToolSystemConfig.from_agent_config(config))
try:
    with AgentCore(config) as agent:
        result = agent.run_turn_blocking("Calculate 12 * 8")
        print(result.final_text)
finally:
    report = shutdown_tool_system(timeout=5)
    if not report.completed:
        print(report.issues)
```

异步宿主可使用 `async with AgentCore(config)` 和 `await agent.run_turn(...)`，
初始化与全局关闭仍由进程入口负责。Agent 不接受工具注册表或服务注入，也不会关闭共享服务。

通过 `session_request` 指定 Session 身份、权限和可信工作区请求。
既有 Session 的 profile/surface/platform 不允许改变，Prompt 保持原样；
Registry 更新只影响后续 Turn。直接调用默认没有人工交互能力；需要交互时传入
`TurnHostContext(HostCapabilities(...), interaction_port)`。

## Gateway、交互与回复投递

CLI 的产品链路为 CLI → GatewayRuntime → AgentCore。CLI 负责输入和展示；
GatewayRuntime 负责 Session 接入、交互路由、恢复和可选投递，不包含模型循环。

- 进程入口初始化工具系统、配置日志并创建 Gateway；退出时共享一个总预算顺序清理。
- CLI 使用 poll 模式，调用 submit_turn 后等待原始 TurnSubmission，按 Session 消费交互。
- clarify/审批答复经 resolve_interaction 直接唤醒 Broker，不进入 Turn FIFO。
- attach 返回的恢复 submission 使用同一消息泵；未完成工具不会自动重放。
- detach 会取消对应 Session 的任务和交互，但不等于重置共享工具资源。
- 本地 CLI 直接展示 TurnResult，不创建终端投递义务。

平台宿主可使用 push 模式及同步 InteractionPort，并按需要配置 DeliveryAdapter。
投递通过 obligation_id 幂等处理，失败后只重试发送、不重跑模型或工具。
示例见 [examples/interaction_host.py](examples/interaction_host.py)，完整契约见
[CLI 经 Gateway 接入设计](docs/funagent-cli-gateway-development-design.md)。

Registry 和 ToolServices 在同一进程内共享，资源按数据库命名空间/profile/session_id 隔离。
替换 Agent 不释放 Session 资源；关闭 Gateway 不关闭其他使用者借用的进程服务。
全局 shutdown 由最外层进程入口负责，不由借用 Gateway 的界面负责。

## 自定义工具

`ToolRegistry` 保存实现定义；`toolsets.py` 决定候选工具；
`model_tools.py` 自动发现、校验并发布整批定义，再按本轮权限和能力生成模型工具列表。

将下面的模块放入 `src/tools/greet_tool.py`，下次进程初始化时自动发现：

```python
from pydantic import BaseModel, Field
from agentcore import ToolContext, ToolEntry, ToolRegistry


class GreetArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def greet(args: GreetArgs, context: ToolContext):
    context.check_active()
    return {"message": f"Hello, {args.name}!"}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(ToolEntry(
        tool_id="example.greet", implementation_version="1",
        name="greet", description="Greet a person by name.",
        args_model=GreetArgs, handler=greet, side_effects="none",
    ))
```

注册代码只声明固定元数据，不捕获 Agent、目录或服务实例。
Handler 从 `context.workspace`、`context.interactions` 和 `context.resources`
获取本次调用的能力；`check_fn` 接收只读 `ToolAvailabilityContext`。
候选工具依次经过 toolset、权限、宿主能力和后端可用性检查。

显式管理更新调用 `get_tool_registry().replace_all(entries)`，
整批校验成功后才发布。进行中的 Turn 使用已加载的 ToolEntry。
异步 Handler 被拒绝；工具超时只表示 Core 停止等待。
真实 Handler 退出前保留资源使用记录；未完成工具永不自动重放。

工作区通过 `[workspace] default_cwd/allowed_root` 配置，相对路径相对 TOML 文件目录解析。
可信宿主可通过 `SessionBuildRequest.workspace` 指定目录，不能越过 allowed_root。
不切换进程 cwd；已有资源时变更目录返回 `SessionWorkspaceConflict`，须先重置资源。

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

新增 tools 配置控制 Web/browser 私有地址策略、artifact 大小和交互等待时限；
approvals.always 仅在人工选择永久授权后原子写入。

- `[session]`：新 Session 的 profile、surface、skills、toolsets 与 permissions。
- `[lease]`：TTL、heartbeat 周期和跨进程等待上限。
- `[workers]`：turn/operation daemon worker 数量及有界队列。
- `[timeouts]`：总预算和工具、策略、memory、observer、review、finalize 上限。
- `[[routes]]`：有序 primary/fallback 路由。

当前版本提供同步 CLI 和可嵌入的 GatewayRuntime；不包含独立网络 Gateway、TUI、Cron、
Subagent、MCP、上下文压缩、多模态或 exactly-once 工具保证。

## 验收

验收命令记录在测试方案中。测试默认使用脚本化 Provider/Adapter/Transport，不访问真实
模型服务。
