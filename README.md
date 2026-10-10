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
- 可选前置知识检索：独立规划、查询改写和有界扩展；默认复用主模型，主模型接收证据并隐藏知识工具。

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

`web_search` 子进程使用 UTF-8，并读取现有 HTTPS/ALL 代理（包括 Windows
系统代理）；可在 `.env` 中用 `DDGS_PROXY` 覆盖搜索代理。搜索失败的工具结果
保留有界错误详情，并遮蔽代理 URL 中的凭据。`web_extract` 使用现有 HTTP
CONNECT 代理和 `NO_PROXY` 设置，连接前校验并固定目标 IP，HTTPS 仍校验原域名
证书。如果 DNS 返回 `198.18.0.0/15` 的代理 Fake-IP，会给出明确诊断；需在代理
中让相关域名返回真实 IP。此时不要直接开启 `allow_private_web_addresses`。

可运行 uv run agentcore tools doctor --config agentcore.toml 查看各后端是否可用
；该命令不下载或升级程序。

工具组之外，可通过 `[session] enabled_tools` 单独启用工具，通过 `disabled_tools`
禁用工具；两者使用精确注册名称，MCP 工具例如 `mcp__rag__echo`。工具组和单工具的
禁用项均优先于启用项，启用后仍需通过权限、宿主能力和可用性检查：

```toml
[session]
enabled_toolsets = ["default", "memory"]
enabled_tools = ["write_file"]
disabled_tools = ["calculator"]
permissions = ["file.write"]
```

调用 API 时可通过 `SessionBuildRequest` 覆盖本轮策略；下一轮未传入时使用配置默认值。

## 前置知识检索

在 `agentcore.toml.example` 中配置并启用 `tools.mcp_servers.rag`，安装 MCP 可选依赖
`uv sync --extra mcp`，再设置 `[knowledge] enabled = true`。后台绑定的查询工具必须
已发现、已注册，且服务配置为 `requires_approval = false`。

`[knowledge.planner]` 可以省略，规划请求会复用首选主模型路由。规划使用独立的
Prompt、有限历史、无工具的 JSON 输出和 `planner_timeout_seconds`；也可配置独立
planner 路由，使用另一个模型或相同型号的不同端点。

每个 Turn 首次回答前只准备一次资料。事实记忆和知识库共用一次规划调用，分别判断
是否检索。程序按启用来源组装规划 Prompt，仅包含对应规则和输出字段；来源开关不交给
模型判断，输出中的禁用来源字段由程序归零，执行时再次限制可用来源。
知识库是否检索遵循规划器的 `need_knowledge`，允许跳过；模式信息不进入 Prompt 或模型输入。
知识库按主要查询、同义表达和原问题检索。合并去重后，有来源的证据
追加到本轮真实 user 消息的 API 请求副本，system 和原始历史保持稳定；RAG 工具同时从主模型 schema 与 handler
映射隐藏，复用通用 `disabled_tools` 策略，显式启用 RAG 工具也不会重新暴露。
规划失败时，各已启用来源只用原问题检索一次。资料只在本轮请求中出现，工具循环和
模型重试复用同一结果，下一轮重新准备；证据不写入持久化消息或后台复盘历史。
恢复执行也基于最近真实用户问题重新检索，不恢复上一次召回快照。临时资料退出后，
其附加位置之后的 KV 可能需要重算，不保证完整历史缓存命中。
后台记忆复盘默认使用 NoopKnowledgeProvider。

记忆复盘使用专用 System Prompt、完整会话历史、执行时读取的最新记忆及仅含
`memory` 的工具视图；复盘消息不写回主会话历史。

默认适配器绑定 Modular RAG 的 `query_knowledge_hub`，优先读取 version=1 的
完整结构化片段；旧引用 JSON 仍可使用，但会标记 `snippet_only`。实际配置默认
不自动启用此功能；上线前应评估真实知识库的漏检率、引用质量和调用延迟。

自定义接入可向 `AgentCore(..., knowledge_provider=...)` 注入同步 KnowledgeProvider，
该参数替换整个资料准备流程，由自定义 Provider 提供两类来源的结果；
默认 RetrievalKnowledgeProvider 也可组合自定义 KnowledgeRetriever，复用其规划、
预算、合并和渲染逻辑。

## Web 前端

```powershell
uv run agentcore web --config agentcore.toml --port 7777
```

打开 `http://127.0.0.1:7777`。WebHost、Gateway 与 AgentCore 在同一个前台进程中运行，
仅监听本机；原生 HTML/CSS/ES Modules 随 Python 包提供，无需 Node 构建。

- ChatDock：新建/继续会话、真实流式文本、工具结果、澄清、审批和取消。
- Overview：当前会话的轮次、阶段流程、模型尝试、工具状态、耗时、usage 和事件时间线。
- Memory：user/note/episode 记录新增、替换和删除，保存时检查记录版本；冲突保留草稿。
- Data：四张 SQLite 业务表的只读分页、筛选、排序、行详情和 JSON 展开。
- 工具、连接、模型、配置：当前进程的脱敏只读视图，修改配置文件后重启生效。

当前标签页的会话保存在 sessionStorage，主题和面板偏好保存在 localStorage。
切换页面、刷新或断线不会取消任务；停止按钮只取消指定轮次。
SSE 断线重连会重放事件或用运行快照校准；服务重启后从数据库读取历史，旧时间线不重建。
提交去重仅覆盖当前进程的活跃与最近 200 个完成轮次；服务重启后先核对历史再重发。
自定义 Transport 没有 stream() 时继续使用 send()，CLI 默认行为保持不变。

记忆默认开启，使用本地 SQLite；网页、工具和后台复盘共享统一 MemoryStore。
文件更新影响新会话，已有会话继续使用保存的提示词快照。模型正文属于当前用户，
诊断事件仍经过 ObservabilityHub 脱敏，配置视图不返回密钥及 env / headers 值。

离线演示使用临时数据库、临时记忆与模拟流式模型，不需要 API 密钥：

```powershell
uv run python examples/web_offline_demo.py --port 7778
```

发送「计算」「确认」「审批」分别体验工具调用、澄清和审批；其他文本展示流式回复。
示例退出后删除临时数据。运行 `uv run pytest` 验证后端与现有功能。

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

### 接入本地 Modular RAG

本机 `agentcore.toml` 已启用 `mcp`，并通过 `[tools.mcp_servers.rag]` 接入
`E:/FunAgent/mcp_server/MODULAR-RAG`。迁移到其他机器时，可复制
`agentcore.toml.example` 中的 Modular RAG 配置并修改路径。

连接使用 RAG 项目自己的 `.venv/Scripts/python.exe`，启动模块
`src.mcp_server.server`，通过 `PYTHONPATH` 定位源码。同步安装环境后，
`mcp-server` 命令也启动同一 stdio 服务。
RAG 服务自行读取其 `config/settings.yaml`、`.env` 和当前注册的结构化索引；
FunAgent 不需要复制这些文件或安装 RAG 依赖。

当前只允许以下三个知识库读取工具，无需逐次审批：

- `mcp__rag__list_collections`：查看集合和条目数量。
- `mcp__rag__query_knowledge_hub`：混合检索，返回片段和来源引用。
- `mcp__rag__get_document_summary`：按文档 ID 获取摘要。

运行不调用 Agent 模型的连接检查；加上 `--query` 时会调用 RAG 配置的 embedding：

```powershell
uv run --extra mcp python examples/mcp_smoke.py --config agentcore.toml
uv run --extra mcp python examples/mcp_smoke.py --config agentcore.toml --query "知识库的核心功能和架构是什么？" --collection knowledge_hub
uv run --extra mcp agentcore chat --config agentcore.toml
```

在聊天中可输入：“先列出知识库集合，再在 knowledge_hub 中检索 MCP 和 Agent
客户端扩展，回答时标注来源。”已运行的进程需要重启以加载新连接配置。
`required = true` 会使连接失败直接中止初始化，避免悄悄缺少知识库工具。

本机 RAG 使用 MCP SDK 1.30，FunAgent 使用 2.2。SDK 自动协商旧协议时，RAG
可能输出一次 `server/discover` 校验警告；随后完成 `initialize` 并正常处理调用。
集合数量统计来自 Chroma 条目数，通常表示切分后的 chunk 数量。
检索依赖 RAG 现有 embedding 配置；本机指向 `http://127.0.0.1:1234/v1`，
运行检索前需保持该模型服务可用。

## 数据库记忆

CLI/Web 默认启用本地 SQLite，存放于配置文件旁的 `memories/memory.db`；
无需云服务或 embedding。`[memory] enabled = false` 可关闭记忆，
`review_every_turns = 0` 仅关闭后台复盘。普通对话的模型 schema 和 handler 隐藏
`memory`，显式启用也不能调用；Web 管理接口和后台复盘继续使用数据库。
数据库是唯一记忆来源：`user` 保存偏好和稳定个人信息，
`note` 保存长期笔记，`episode` 保存历史事件。原始对话仍由会话数据库保存。

Web 支持添加、查询、修改和删除，返回字符串 ID 和整数版本；修改和删除按版本检查冲突。
新会话在 `memory_max_chars` 预算内仅加载 `user` 并冻结 system；已有会话保留原快照，
不会自动重写。每轮规划按需召回 `note/episode`，事实查询使用空格分隔的关键词。
SQLite 使用现有 FTS5/BM25 和 CJK 子串匹配；Supabase 使用现有 embedding 搜索。
事实召回不依赖 `[knowledge] enabled=true`。

`[agent] memory_recall_top_k=5` 控制每类候选数量及合并后最多注入的条数：保留后端排序，
交替合并并按 ID 去重。`memory_recall_max_chars=4000` 控制临时事实参考块的字符预算，
与用户画像预算独立；设为 0 可停止事实召回。资料只追加到请求副本，不增加历史消息，
重复命中时会在本轮重新提供。准备阶段使用 `knowledge.timeout_seconds`，记忆搜索批次
使用 `timeouts.memory_seconds`；单源失败保留另一来源，取消及运行预算耗尽会正常中断。

每 10 个成功完成的普通用户轮次排入独立后台复盘，可用 `review_every_turns` 调整。
复盘只处理尚未完成复盘的区间，允许 `list/search/add`，不改写主会话历史、不调用知识检索。
进度持久化，失败或取消不推进；队列仍在进程内，退出时未完成的任务会在后续触发时处理，
不会在启动时自动补跑。复盘逐条写入，不承诺整个批次原子提交。

Web 对话到复盘写入、记忆召回到模型上下文的用例与人工验收步骤见
[tests/MEMORY_WEB_CASES.md](tests/MEMORY_WEB_CASES.md)。运行
`uv run --extra dev pytest tests/test_web_memory_flow.py` 可通过真实本地 HTTP
接口和临时 SQLite 验证两条链路，模型回复使用离线脚本。

文件模式已移除。旧 `[file_memory]` 配置会提示迁移；先显式导入旧目录的 § 分隔条目，
再将配置替换为示例中的 `[memory]` 和 `[memory.sqlite]`。导入保留原文件，重复执行会去重。
`USER.md/MEMORY.md` 只能显式导出为阅读快照，修改导出文件不会更新数据库。

```powershell
uv run agentcore memory import-legacy --config agentcore.toml --directory memories
uv run agentcore memory export --config agentcore.toml --directory memory-export
```

导出拒绝覆盖已有文件。嵌入 `AgentCore` 时使用 `memory.MemoryFeature` 安装工具，
传入其 `prompt_builder`、`memory_retriever=feature.retriever`、`review_scheduler`，在关闭 Agent 后关闭 Feature；
Feature 接受注入的 MemoryStore，未来后端只需实现统一协议和工厂分支。

### Supabase 与本地 embedding

安装可选依赖 `uv sync --extra supabase`，在 Supabase SQL 编辑器显式执行
[sql/supabase/001_memory.sql](sql/supabase/001_memory.sql)。首次部署前将脚本的
`vector(1024)` 改成模型实际维度；配置的 `dimensions` 必须一致。SQL 不会自动迁移已有表。
在配置文件旁的 `.env` 设置 `SUPABASE_SECRET_KEY`，然后选择后端：

```toml
[memory]
backend = "supabase"
review_every_turns = 10

[memory.supabase]
url = "https://YOUR_PROJECT.supabase.co"
credential_ref = "env:SUPABASE_SECRET_KEY"
namespace = "personal-agent"
timeout_seconds = 20
min_similarity = 0.3 # 示例阈值，按实际模型验证

[memory.supabase.embedding]
base_url = "http://127.0.0.1:1234/v1"
model = "YOUR_LOCAL_EMBEDDING_MODEL"
dimensions = 1024
# credential_ref = "env:EMBEDDING_API_KEY" # 服务有鉴权时填写
```

用 `uv run --extra supabase agentcore web --config agentcore.toml` 启动；同时使用 MCP
时追加 `--extra mcp`。LM Studio 与 Ollama 共用兼容的 embeddings 客户端，Ollama
地址为 `http://127.0.0.1:11434/v1`；远程兼容服务同样设置 `base_url/model/credential_ref`。
无需鉴权的服务可以省略 embedding 凭据，不要求 `OPENAI_API_KEY`。
模型需要查询/文档前缀时，可设置 `query_prefix/document_prefix`，默认均为空。

Supabase HTTP 客户端沿用 HTTPX 的环境和系统代理设置。建立连接失败时最多尝试三次；
认证、读写或响应解析失败不会自动重试。持续出现 `ConnectError` 时检查网络、代理和 TLS
连接；该错误发生在连接层，重新执行初始化 SQL 无法解决。

Supabase 保存提炼出的记忆正文和向量，使用语义检索；正文与向量同步提交，更新和删除
继续检查记录版本。模型或前缀变化会拒绝混用向量，需要显式重建。服务端需保持同名
模型权重一致，客户端不能检测权重被悄悄替换。云端故障不会转写到 SQLite。

复盘仍使用现有后台队列，本地 `[memory.sqlite].path` 只更新复盘进度，不缓存云端记忆；
原始会话仍保存在本地。切换 Supabase 项目或 namespace 后使用独立进度。旧 SQLite
记忆保留，切换后端不会自动迁移或双写。维护命令同样操作选定的后端。

SQL 仅授予 service_role 访问；服务密钥保存在 Python 宿主，浏览器使用现有本地 API。
namespace 用于数据范围筛选，服务密钥拥有者可以访问所有范围；多用户部署需另配身份授权。

离线验证新客户端：`uv run --extra dev --extra supabase pytest tests/test_supabase_memory.py`。
实际 PostgreSQL/pgvector SQL 验证使用独立的测试运行时，无需 Supabase 账号：

```powershell
npm install --prefix .funagent/sql-test-runtime --no-audit --no-fund @electric-sql/pglite @electric-sql/pglite-pgvector
node --test tests/supabase_sql.mjs
```

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
