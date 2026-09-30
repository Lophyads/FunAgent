# FunAgent

FunAgent 是一个 Python 3.11+ 的同步 Agent 内核。完整 ConversationLoop 在 daemon turn
worker 中运行；异步 API 只负责提交任务并等待 Future，不在业务 EventLoop 上执行
Provider、SQLite、策略、工具或生命周期逻辑。

当前实现包括：

- Session 仅冻结 Prompt；工具定义在每个 Turn 开始时重新解析，Memory recall 只在新 Session bootstrap 时执行。
- Provider Resolver、API Mode Adapter 与单次请求 Transport 三层解耦。
- 显式 Phase orchestrator，Provider retry 不增加 Agent iteration。
- SQLite schema v10：Session、消息、Session Lease 和投递义务四张业务表。
- 单调 fencing token、独立 heartbeat 线程和 stale-owner 写入拒绝。
- 顺序工具执行、Pydantic 参数校验、guard、approval 与未知副作用恢复语义。
- GatewayRuntime 启动恢复、内部恢复 Turn、Delivery Runtime 和 inbound gate。
- 隔离的 Observer/EventSink 可观测性路径及集中敏感信息脱敏。
- Web 搜索/提取、文本 browser、AX-only computer_use 与跨 CLI/Gateway 的 clarify/审批交互。
- 可选 MCP Client：stdio/Streamable HTTP、多服务工具发现、注册及同步调用。

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

## MCP 工具

安装可选依赖并使用 MCP 配置启动：

```powershell
uv sync --extra mcp
uv run --extra mcp agentcore chat --config agentcore.toml
```

在配置中启用 `mcp` 工具集，并配置一个或多个服务：

```toml
[session]
enabled_toolsets = ["default", "memory", "mcp"]

[tools.mcp_servers.local]
command = "python"
args = ["E:/mcp_servers/server.py"]
connect_timeout_seconds = 15
tool_timeout_seconds = 60
requires_approval = true
include = ["search_*", "get_*"]
exclude = ["get_secret_*"]

[tools.mcp_servers.local.env]
API_TOKEN = "env:MCP_LOCAL_TOKEN"

[tools.mcp_servers.remote]
url = "https://example.com/mcp"
required = false

[tools.mcp_servers.remote.headers]
Authorization = "env:MCP_REMOTE_AUTHORIZATION"
```

`command`/`url` 恰选其一，分别使用 stdio/Streamable HTTP。`args`/`env` 只用于
stdio，`headers` 只用于 HTTP；完整值 `env:VAR` 引用环境变量，缺失会在连接前报错。
Authorization 变量包含完整的 `Bearer ...` 值。stdio cwd 使用初始化的工作区目录。

`enabled` 默认 true，`required` 默认 false；可选服务启动失败不影响其他服务，
必需服务失败会回滚初始化。工具目录在启动时完整分页发现；修改配置或目录后重启。
工具名称为 `mcp__服务名__工具名`，长名称会稳定截短。include/exclude 使用原始工具名
的通配符，排除优先；未配置 include 时纳入所有工具。

MCP 工具默认要求审批，宿主没有审批能力时会隐藏。受信任服务可显式设置
`requires_approval = false`。调用沿用现有参数校验、Guard、审批和执行结果；
远端 JSON Schema 支持文档内引用，不读取外部 Schema。文本优先、结构化结果回退，
二进制内容仅返回类型提示；远端 `is_error` 记录为工具执行失败。

连接由进程级工具服务共享，Session reset 不关闭连接或重置远端状态。
不同服务可并发等待 I/O，首版同服务串行。同回合工具保持顺序执行。
超时/取消不保证远端副作用撤销，已提交工具不自动重放；断线后重启恢复。
SDK Client 在长期连接任务中打开和关闭，退出先等待 RPC/连接清理，再停止后台循环。
工作区路径限制不会自动限制外部 MCP 服务的访问权限。

当前支持 MCP Python SDK 2.2+；暂不提供 OAuth、旧 SSE、热刷新、自动重连、
sampling/elicitation、输入续传、远端 resources/prompts 或多模态落盘。
没有启用 MCP 时无需安装该 extra，也不会导入 SDK 或启动 MCP 线程。

MCP 验收使用本地服务，不访问真实模型：

```powershell
uv run --extra dev --extra mcp pytest tests/test_mcp_client.py tests/test_mcp_integration.py
```

## 文件记忆

示例配置启用 `[file_memory]` 与 `memory` toolset。配置文件旁的
`memories/MEMORY.md` 和 `memories/USER.md` 保存长期笔记与稳定用户事实；
不适合直接作为指令。`memory` 工具支持 `list/add/replace/remove`，其中
`replace/remove` 走现有人工审批。新会话读取文件并冻结提示词，当前会话写入后仍
使用原提示词。每 `review_every_turns` 个完成的普通用户轮次会排入后台复盘；设为
`0` 可关闭。后台复盘仅可 `list/add`，且不写入主会话历史。进程退出时尚未完成的
复盘不会补跑。旧版按子目录保存的记忆不会自动合并；升级前如已写入记忆，
请先将需要保留的条目整理到记忆目录下的两个文件中。直接嵌入 `AgentCore` 时可使用 `file_memory.FileMemoryFeature`
安装工具并传入 `prompt_builder`、`review_scheduler`，在关闭 Agent 后关闭 Feature。

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

通过 `session_request` 指定本轮工具集、权限和可信工作区请求。
既有 Session 的 Prompt 保持原样；
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

Registry 和 ToolServices 在同一进程内共享，浏览器和桌面资源按 session_id 区分。
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
Handler 从 `context.workspace`、`context.interactions` 和 `context.services`
获取本次调用的依赖；有状态后端通过 `context.resource_key` 复用实例。
`check_fn` 接收只读 `ToolAvailabilityContext`。
候选工具依次经过 toolset、权限、宿主能力和后端可用性检查。

显式管理更新调用 `get_tool_registry().replace_all(entries)`，
整批校验成功后才发布。进行中的 Turn 使用已加载的 ToolEntry。
异步 Handler 被拒绝；工具超时只表示 Core 停止等待。
调用结束或超时后，`context.check_active()` 会拒绝继续使用该上下文。
后端操作持有实例锁直到操作退出；未完成工具永不自动重放。

工作区通过 `[workspace] default_cwd/allowed_root` 配置，相对路径相对 TOML 文件目录解析。
可信宿主可通过 `SessionBuildRequest.workspace` 指定目录，不能越过 allowed_root。
不切换进程 cwd；目录随会话保存并跨 Turn 继承，变更目录无需重置浏览器。

## Provider 扩展

扩展接入点为 `ProviderResolver`、`ApiModeRegistry`、`ApiModeAdapter` 和 `Transport`。
`Transport.send()` 严格执行一次网络发送，SDK 内部 retry 被禁用；重试、Retry-After、
cooldown、fallback 和当前 Turn route 粘滞由 ConversationLoop 统一控制。

## 结果语义

`TurnResult.status == "completed"` 仅表示模型正常结束本轮。业务工具是否成功需结合
`completion_reason`、`tool_outcomes`、`provider_attempts` 和 `route_attempts` 判断。

SQLite v10 仍包含 `sessions`、`messages`、`session_leases` 和
`delivery_obligations` 四张业务表。历史顺序由 `messages.id` 决定；崩溃恢复从
Session 尾消息和过期 lease 判断。升级前停止旧进程；现有 v8 数据库会自动增加
`sessions.workspace` 字段，v8/v9 的历史会话身份列保留原值但不再使用；新数据库
不创建这些列。更早的非兼容 schema 仍会被拒绝。

工具后端自行按稳定的资源键复用浏览器/桌面，不再为每个 Turn 登记工具资源。
workspace 随会话配置保存，切换目录不清空浏览器。闲置回收按后端实际操作时间计算；
运行中的操作受后端锁保护。接口与复盘接入见[执行与工具边界](docs/conversation-loop-session-boundary.md)。

## 配置

新增 tools 配置控制 Web/browser 私有地址策略、artifact 大小和交互等待时限；
approvals.always 仅在人工选择永久授权后原子写入。

- `[session]`：新 Session 的 toolsets 与 permissions。
- `[lease]`：TTL、heartbeat 周期和跨进程等待上限。
- `[workers]`：turn/operation daemon worker 数量及有界队列。
- `[timeouts]`：总预算和工具、策略、memory、observer、review、finalize 上限。
- `[[routes]]`：有序 primary/fallback 路由。

当前版本提供同步 CLI 和可嵌入的 GatewayRuntime；不包含独立网络 Gateway、TUI、Cron、
Subagent、上下文压缩、多模态或 exactly-once 工具保证。

## 验收

验收命令记录在测试方案中。测试默认使用脚本化 Provider/Adapter/Transport，不访问真实
模型服务。
