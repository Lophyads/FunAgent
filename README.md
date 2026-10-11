# FunAgent

FunAgent 是单机、单应用进程的 Agent。CLI 和浏览器共享持久会话、异步任务、人工澄清/单次批准与统一记忆；主业务、召回、后台复盘共用一个 ReAct 内核。

需要 Python 3.11–3.13。发行包名为 `funagent`，Python 包为 `src`，命令为 `funagent` 或 `python -m src`。

源码按 `src.api`、`src.memory`、`src.model`、`src.persistence`、`src.task`、`src.tool` 组织；配置、公共契约、会话和应用生命周期位于 `src.frame`。旧 `funagent.*` 导入应改为对应的 `src.*` / `src.frame.*` 路径。目录迁移后执行 `uv sync --locked` 更新 editable 安装和命令入口。

TDD00–TDD12 已验收：完整离线 909 项、wheel/sdist 独立安装各 896 项行为合同、隔离 Supabase 12 项真实合同通过。范围、命令和限制见 [逐项迁移验收](docs/migration-acceptance.md)、[TDD12 记录](tests_refactor/TDD12.md)；当前接口见 [API 参考](docs/api-reference.md)。D03/D05 继续待裁决，用户远程项目未部署。

## 安装与运行

项目根目录的五份配置已从 `E:/FunAgent/agentcore.toml` 迁移。使用 Bash（Windows 可用 Git Bash，也可在 PowerShell 直接调用 `bash`）启动旧配置对应的 Web 服务：

```bash
bash start.sh
# 只校验五份配置，不连接外部服务：
bash start.sh --check-config
```

脚本定位到项目根目录，并通过 `uv run --locked` 安装所需的 `openai`、`mcp`、`supabase`、`embedding` extras，再运行 `funagent --conf-dir <项目根目录>`。Web 地址沿用旧入口的 `http://127.0.0.1:7777`。凭据继续从根目录 `.env` 或进程环境读取：`OPENAI_API_KEY`、`SUPABASE_SECRET_KEY`、`EMBEDDING_API_KEY`。

Web 已迁入原版七个页面与三栏布局：Overview、Memory、Data、工具、连接、模型、配置。15 个静态资源随 Python 包安装，使用原生 ES Modules，无需 Node 构建。保留主题、导航和对话折叠、宽度拖动/键盘调节、排队、逐轮停止、澄清与审批；刷新恢复历史和任务状态。Overview 展示当前 TaskQueue、MemoryPrepare、ModelRuntime、ToolDispatch、InteractionMgr、持久化及 EventMgr 的真实事件，重启后缺失的时间线与指标显示未知。

Memory 的用户事实、长期笔记、历史事件对应 profile/note/episode，选择 facts 策略中首个可管理读取的情景来源。新增直接写入，修改和删除先展示绑定内容与版本的确认弹窗；拒绝、过期、版本冲突保留草稿。Data 只读浏览当前主库七张表，默认每页 50 行、最多 200 行，预览和详情截断均有标记。可选持久化后端未实现 DataReadStore 时明确显示 UNSUPPORTED。迁移验证命令与截图见 [Web 迁移验收](docs/web-migration.md)。

PowerShell 的 `bash.exe` 若进入 WSL，脚本会调用 Windows 的 `uv.exe` 并把配置目录转为 Windows 路径，继续使用 Windows 虚拟环境和 RAG 解释器。无需在 WSL 另装 uv；Windows uv 所在目录需在 PATH 中。WSL 启动时的 localhost 代理提示属于 WSL 网络配置，脚本运行的应用使用 Windows 环境。

`model.conf` 保留 DeepSeek 路由与三次尝试（两次重试）；`tool.conf` 保留 default/mcp/memory 工具集、澄清工具及 RAG 三个免批准的只读工具；`memory.conf` 保留 Supabase 项目、namespace、0.3 相似度阈值、每三轮复盘和 Qwen3-Embedding-8B 的完整 4096 维。RAG 继续使用 `E:/FunAgent/mcp_server/MODULAR-RAG` 的独立虚拟环境，通过 `cwd` 和 `-X utf8` 保留旧 PYTHONPATH/UTF-8 行为。自动知识召回仍关闭，主模型可以直接调用 RAG 工具。

迁移采用新运行时支持的配置项：队列容量 128、任务预算 300 秒、主循环上限 8、记忆分支预算各 4000 字符、DEBUG 日志。旧 worker 数量、lease、细分 observer/policy/finalize 超时、全局工具结果截断、重试延迟上限/jitter、浏览器专用参数和独立复盘 SQLite 检查点在新架构中没有对应配置；复盘检查点由新主数据库保存。澄清与批准共用 `interaction_timeout = 300`，旧澄清单独的 3600 秒不能分别配置。新日志的普通/错误文件共用 5 MiB、3 份轮转设置。

根配置使用新的 `agentcore.db`，路径相对新项目，不导入旧数据库。Supabase 使用新版 `funagent_memory_*` RPC；配置迁移不部署远程 SQL，也不迁移旧记忆表。首次使用前，按 [SQL 部署说明](sql/supabase/README.md) 在选定数据库部署新版 RPC（支持 4096 维），并确保 LM Studio 的 `http://127.0.0.1:1234/v1/embeddings` 服务及 RAG 环境可用。原来的 `memory.sqlite.path` 仅用于旧架构检查点，因此未创建第二个记忆源。

在源码或解压后的 sdist 目录运行：

```powershell
uv sync --locked
uv run funagent --conf-dir examples/refactor-cli --check-config
uv run funagent --conf-dir examples/refactor-cli
```

也可以安装 wheel：

```powershell
python -m pip install dist/funagent-0.2.0-py3-none-any.whl
python -m src.frame.distribution --list
python -m src.frame.distribution refactor-cli ./my-conf
funagent --conf-dir ./my-conf
```

配置目录必须含五份 TOML 文件：`model.conf`、`tool.conf`、`memory.conf`、`log.conf`、`serv.conf`。wheel 和 sdist 都携带与源码一致的示例；上述导出命令只创建新目录，已有目录返回 CONFLICT，不覆盖用户配置。Python 资源入口为 `src.frame.distribution.AvailableExamples/ExampleFiles/CopyExample`，从无关 cwd 也能使用。数据库路径相对配置目录，workspace 为启动时的当前目录或显式绝对路径。使用新的配置目录和 `app.sqlite3` / `memory.sqlite3`；历史 API、配置与数据库没有兼容或自动迁移。

`serv.conf` 的 `mode` 选择 `cli`、`web` 或 `both`。浏览器示例：

```powershell
uv run funagent --conf-dir examples/refactor-web
```

打开终端显示的地址（默认 http://127.0.0.1:8080）。`both` 同时提供 CLI 和 HTTP；CLI `quit` 关闭整个应用。关闭或刷新浏览器后仍可查询原任务，只有显式取消才发送取消请求。Ctrl+C 停止应用：先停止准入、取消任务/人工等待与复盘，再等 worker/实际 IO 返回并关闭共享连接。

真实 OpenAI Chat Completions profile 可设置 `streaming = true`（见 `examples/refactor-openai`）。浏览器通过 `GET /tasks/{id}/events` 接收 SSE 增量；重试/降级会重置被弃 attempt，刷新和慢读用有界快照恢复。`Last-Event-ID` 格式为 `<task_id>:<seq>`，不能跨任务重放。临时正文只用于展示，完整最终回复与任务终态由同一次数据库提交保存；断开 SSE 只释放订阅。停止应用会先唤醒订阅、等待 HTTP 请求线程退出，再等实际模型 IO 归还借用并关闭客户端。默认未启用流式的 profile 保持普通请求；所选驱动缺流式能力时报 `UNSUPPORTED`。

## 离线体验

`examples/refactor-cli` 和 `examples/refactor-web` 使用离线 DemoProvider，无需模型凭据。CLI 支持：

```text
new 我的会话
sessions
submit <session_id> hello
submit <session_id> --request-key retry-001 hello
wait <task_id>
history <session_id>
get <task_id>
cancel <task_id>
pending <task_id>
answer <task_id> <interaction_id> <答案>
quit
```

`wait` 期间可以直接回答澄清/批准。发送 `tool: 内容` 使用 echo；`ask: 问题` 请求澄清；`read: 文件路径` 请求单次批准后读取工作区文件。浏览器提供同一闭环。

`examples/refactor-review` / `refactor-web` 启用本地记忆与独立复盘：这两个离线示例显式设 `every_turns = 1`，`remember: 我喜欢蓝色` 完成后后台写入情景事实，`memory: 蓝色` 在后续轮次读取实际证据。自动复盘默认每 10 个完整 completed 用户轮触发，0 关闭；计数去重且持久化，无新事实且无失败操作时也推进水位。此前两条已复盘文本只供理解指代，不能作为新写入 evidence。`examples/refactor-memory` 展示外部只读语义来源；它与情景事实共用 MemoryMgr。CLI 还提供 `memory search/list/write/update/delete/confirm/export` 和 `review <session_id>`。网页的“记忆管理”可按来源、类型、主体、时间和来源标记搜索或分页列出记录；编辑/删除先展示绑定记录与版本的确认内容，再执行 CAS。“运行诊断”显示工具、连接、模型、生效配置和队列状态，查询不会调用模型。

TDD01 已完成接口与职责拆分：公共域入口只绑定协议对象，主任务、召回和复盘继续共用 Runner.Run；具体 Store/Runtime/Registry 从各自实现模块导入。开发装配方式、实际 Red/Green 和 371 项完整离线验收见 [TDD01 记录](tests_refactor/TDD01.md)。后续业务迁移仍按 [TDD 计划](docs/tdd-refactor-migration-plan.md)推进。

TDD02 已实现域内工厂、主存储选择、配置旁 `.env`、日志和启动回滚，验收见 [TDD02 记录](tests_refactor/TDD02.md)。默认主存储使用 SQLite 和 `options.database`；显式选择可在 `serv.conf` 末尾添加以下内容，此时可省略 `database`：

```toml
[options.persistence]
kind = "sqlite"
[options.persistence.settings]
path = "app.sqlite3"
```

TDD03 已完成工具自动注册、不可变选择快照、会话权限、按 action 的单次批准和后端装配框架，验收见 [TDD03 记录](tests_refactor/TDD03.md)。`tool.conf` 只选择已注册工具、后端和策略，不再接受 `definitions`、handler 名称或参数 schema。schema 与 handler 工厂由受信任的 `src/tool/builtins/*_tool.py` 声明；启动按模块名排序扫描并调用 `register_tools(registrar)`，全部候选通过校验后才绑定。新增模块无需修改启动文件或工具名字表。

用途既可使用示例中的名称数组，也可组合工具集与 enable/disable。disable 始终优先；模块声明的 purpose、权限、capability/check_fn 继续收紧选择，执行前再次检查。例如：

```toml
[toolsets]
files = ["read_workspace"]

[purposes.main]
toolsets = ["demo", "files"]
enable = ["clarify"]
disable = ["read_workspace"]

[session_access]
default = ["workspace.read", "memory.facts.read"]
"<session_id>" = []
```

替换原 `[purposes]` 中的 `main` 数组后再使用 `[purposes.main]`。会话 ID 的权限覆盖 default，未配置任何权限时为空。工具扩展通过 `ToolEntry` 声明身份、`ToolDefinition`、接收已绑定 backend 的 handler 工厂和纯策略；`ActionPolicy` 可分别返回 `ToolDecision("allow")`、`"deny"`、`"approval"`。`tools.<name>.requires_approval = true` 可加严批准，不能放宽模块的拒绝或批准策略。批准只用于绑定 task/run/call、操作与规范化参数摘要的这一次调用，拒绝、过期、改参或重放不会执行副作用。

后端由域内工厂按所选配置 ID 构造，共享实例只由启动所有者关闭。真实 MCP、terminal、web 和 HTTP AX computer_use 后端已在 TDD09 验收，支持 `ListTools`、include/exclude 和 `mcp_<server_id>__<规范化原名>`。动态名称的存在和冲突在装配时校验，`--check-config` 仅检查配置结构与静态引用；后端初始化与实际调用仍在正式 Start/运行时完成。

TDD04 已完成主模型 `memory` 的 list/search/add/replace/remove，以及结构化 `clarify`，验收见 [TDD04 记录](tests_refactor/TDD04.md)。CLI/OpenAI/记忆/复盘/Web 示例允许主模型使用这两个工具；事实来源仍需要配置的 `memory.facts.read/write` 权限。replace/remove 每次都请求绑定当前调用和 expected_version 的批准，批准后执行 CAS；后台仅暴露 list/search/add，绕过 schema 也拒绝改删。证据来自持久化的可信会话消息，write_key 由服务生成，模型不能自行授予权限或提交证据/key。

`clarify` 接收 1–5 个带 `id`、`prompt`、`kind` 的问题；kind 为 single/multiple/text，选择题另带 2–20 个唯一文本 options。回答格式为 `{"answers":[{"question_id":"color","value":"blue"}]}`，多选 value 是字符串数组，文本 value 是非空字符串。CLI 的 wait 逐题输入，多选输入 JSON 数组；手工 answer 输入完整 JSON。Web 使用同一问题结构和服务端校验。非法选项、重复 ID 和错误类型保留原请求；单独过期返回 INTERACTION_EXPIRED，任务取消/总期限分别返回 CANCELLED/DEADLINE。

TDD05 完成确定召回、真实 SQLite 检索、元数据、独立预算与画像冻结，验收见 [TDD05 记录](tests_refactor/TDD05.md)。`memory.conf` 中 `policies.recall` 只配置规划模型、timeout 和 history_max_chars；`policies.profile/facts/knowledge` 分别配置 enabled、sources、max_records、max_chars 和 timeout，knowledge 另有 1–3 次 max_queries。画像在会话首次 Prompt 初始化时读取并冻结；同会话更新记录后仍使用原快照，新会话读取最新画像，显式 memory 工具始终读取最新库。

召回最多一次无工具规划 Run，输入当前真实问题、最多六组有界历史及授权 source_description，输出独立的 need_memory/need_knowledge。事实查询固定 note/episode；知识按 query、variant、原问题去重限次。规划错误对已启用且授权的分支按原问题降级，父取消/总期限继续终止。状态与安全 warnings 即使没有记录也进入最后真实 user 的请求副本；system、持久历史和复盘原历史不受召回注入影响。三类预算按完整渲染字符数计算，长正文可截断并保留来源、引用和 truncated 标记。

SQLite 支持关键词 OR、FTS5/BM25、覆盖度/CJK 与事件时间排序；FTS5 不可用时使用回退并返回 FTS_UNAVAILABLE。记录支持 note/episode/profile/document、subject、event_at、created_at、updated_at、origin；event_at 与时间过滤使用带时区的 ISO 8601，输出统一 UTC。主 memory 工具可写入/筛选这些元数据，replace 缺省元数据保持原值，显式 null 清空。当前新架构的 memory_records 表会补齐字段和索引，保留现有记录/版本/write_key；旧项目 schema 仍不导入。

扩展实现由启动装配注册到 `src.persistence.factory.STORES`，构造函数接收冻结的 settings 和绝对配置目录，返回 PersistenceStore。只有所选 kind 会构造；未知 kind 报 `DRIVER_UNAVAILABLE`。

应用在创建驱动之前读取所选配置目录的 `.env`，进程环境变量优先，插值也遵循这个优先级。缺 `.env` 可以启动；最终缺凭据仍报 `CONFIG_CREDENTIAL`。停止或启动回滚后会移除本次加载的默认变量，外部修改过的值保留。

`log.conf` 的 `[options]` 支持 `level`、`file`、`error_file`、`max_bytes`、`backup_count`、`format` 和 `console`。文件路径相对配置目录；未设置普通日志文件时默认输出到 stderr。格式可引用 `session_id`、`task_id`、`run_id`、`purpose`、`error_code`；任务日志记录状态和安全失败码，不记录用户正文。普通、错误和启动失败日志均脱敏；停止后移除应用 handler 并恢复已有 logger 设置。例如：

```toml
[options]
level = "INFO"
file = "logs/app.log"
error_file = "logs/error.log"
max_bytes = 1048576
backup_count = 2
```

任务提交 `POST /sessions/{id}/tasks` 接收 `text` 和可选 `request_key`。键为 1–128 个字母、数字或 `_.:-`，仅在同会话内唯一；相同键、相同内容返回原任务，不占新容量。内容指纹统一 NFC 和换行，保留空格语义；异内容返回 `409 CONFLICT`。省略键表示新提交。请求键字段可在当前重构主库中增量添加，旧项目 schema 仍不兼容。浏览器在发请求前保存键和文本，响应丢失后重试或刷新会复用原键；任务登记后的发布失败会补偿或关闭准入，重启只修复状态。

管理权限来自 `tool.conf` 的 `[session_access] management`；未配置时采用 `default`。用户提交 source_id 仅选择来源，不能增加权限。`memory update/delete` 返回 `challenge_id` 与 `arguments_digest`，然后执行 `memory confirm <challenge_id> <arguments_digest> yes`（或 `no`）；确认前记录不变，挑战绑定记录、版本和内容，使用 `interaction_timeout` 过期，确认一次后失效，重启后失效，与模型工具批准隔离。

```text
tools doctor
connections
models
config
status
logs tail --lines 50 --filter task=abc
logs follow --source error --seconds 10
memory export facts "facts export.jsonl"
```

日志只读取本应用 `log.conf` 配置的 normal/error 文件；需先配置 file/error_file。tail 最多 1000 行、单次读取 256 KiB；follow 默认 10 秒、最多 60 秒，识别轮转，输出继续脱敏。导出以实际 List 分页写入 JSONL，保留内容、版本、引用、证据与元数据；独占创建，不覆盖目标，失败清理本次不完整文件。没有 import-legacy。

管理 HTTP：`GET /memory/sources`、`GET /memory/search?source_id=facts&text=中文`、`GET /memory/records?source_id=facts&limit=50`；过滤字段为 type/subject/origin/event_from/event_to，records 支持 cursor，limit 为 1–100。`POST /memory/changes` 接收 action（replace/remove）、source_id、record_id、expected_version 及可选 patch；replace 的 patch 接收 content 和可选 type/subject/event_at/origin。返回挑战后，`POST /memory/challenges/{challenge_id}/confirm` 只接收 arguments_digest 与 approve 布尔值。`GET /diagnostics/{tools|connections|models|config|status}` 只输出安全白名单与脱敏数据。

## 真实模型

OpenAI 兼容 Chat Completions 驱动是可选依赖：

```powershell
uv sync --locked --extra openai
uv run --extra openai funagent --conf-dir examples/refactor-openai
```

设置配置指定的凭据环境变量（默认 `OPENAI_API_KEY`），填写 `model.conf` 的模型名称和端点。配置不保存密钥。未选的可选驱动无需安装；所选驱动缺失会报 `DRIVER_UNAVAILABLE`，缺凭据报 `CONFIG_CREDENTIAL`。Provider 单次 IO，SDK 自动重试关闭；有界 retry/fallback 由公共内核处理。

TDD06 已完成模型错误、退避、参数纠错与同 Provider 并行，验收见 [TDD06 记录](tests_refactor/TDD06.md)。401/403、模型/端点缺失的 404、空回复可走所配置的 fallback_profiles，每次换 profile 重新编码；损坏身份、重复 call_id 和整体协议错误仍终止。429/网络/超时按 max_retries 指数退避并加入 jitter，指数等待最多 8 秒，合法 Retry-After 作为等待下限；等待可取消并计入总期限。单次本地等待最多 60 秒，服务端要求更久时结束该 Run，避免提前重发。

合法身份的 JSON/对象/schema 参数错误返回配对的 invalid_arguments，让模型用新 call_id 修正；错误参数在历史和后续请求中保存为空对象，handler 不执行，权限与禁止操作仍会拒绝。同一 provider_id 的主模型、召回、复盘可独立并行 IO；同轮仍先召回再调用主模型，主任务仍为一个 worker。关闭先拒绝新借用，全部实际 IO 返回后才关闭共享客户端。

## Supabase 记忆

TDD08 已提供 `store="supabase"` 的 episodic memory 和 OpenAI 兼容 Embedding HTTP 驱动。安装 `supabase` / `embedding` extras，参考 [完整示例](examples/refactor-supabase/README.md) 填写项目端点、namespace、模型、维度及 query/document 前缀。凭据通过配置目录 `.env` 或进程环境加载，配置只保存变量名。

TDD09 已补齐 13 个原工具和真实 MCP 动态工具。工具集包括 default（calculator/current_time）、workspace（目录、搜索、带行号读取、批准写入/补丁）、terminal、web、computer、memory 和 interaction。终端、网络和 AX 计算机操作通过显式 backend_id 绑定；写入、补丁、终端与可变桌面操作使用单次批准。使用与 AX 服务合同见 [完整工具示例](examples/refactor-tools/README.md)。

知识 MCP 使用同一启动连接的只读 callback，固定 query_knowledge_hub、collection/top_k 并校验 structuredContent 中的记录/引用；支持 stdio、Streamable HTTP 和 SSE。安装 mcp extra 后参考 [知识 MCP 示例](examples/refactor-mcp/README.md)。web extra 提供 keyless DDGS 与 PDF 提取；未选择的后端及 SDK 不影响默认启动。实际协议、工具 IO、取消/关闭和独立安装证据见 [TDD09 记录](tests_refactor/TDD09.md)。

[新 SQL/RPC 部署说明](sql/supabase/README.md) 包含权限、干净部署与真实后端验收命令。wheel 和 sdist 均携带同一迁移资源；`src.memory.deployment.SupabaseSQL()` 可从无关目录导出。写入先完成 embedding，再由 RPC 原子落盘；修改和删除使用版本 CAS，向量检索先过滤 namespace/source/profile。模型、维度或前缀升级需要显式维护重建，当前使用精确余弦检索。

## 开发与交付验证

```powershell
uv sync --locked --extra dev --extra openai --extra embedding --extra supabase --extra mcp --extra web
uv run --no-sync pytest tests_refactor
uv build --out-dir dist
uv run --no-sync python scripts/release_smoke.py --wheel dist/funagent-0.2.0-py3-none-any.whl --sdist dist/funagent-0.2.0.tar.gz --offline --tdd12-contracts --contract-artifact-dir docs/tdd12-evidence
```

测试默认使用脚本模型、受控 transport 和临时 SQLite，并排除 `supabase_integration`。release smoke 在两个临时环境分别安装 wheel 和直接从 sdist 构建的包，以 uv.lock 的约束安装依赖，从无关目录运行 cli/web/both、示例导出、页面、工具/人工答复/记忆闭环与资源关闭。`--tdd12-contracts` 对两个已安装包运行完整离线行为合同；构建与私密文件注入测试在源码 job 运行，安装 job 不复制 src。分组参数 `--tdd08/09/10/11-contracts` 保留。`--offline` 要求缓存已有构建和所选依赖，首次准备可省略。真实数据库验收使用 [隔离 Supabase 脚本](sql/supabase/README.md)，缺测试环境报错，不跳过。

真实 Chromium 操作 smoke 另需 Node、Playwright 和 Chrome/Chromium：

```powershell
python scripts/refactor_browser_smoke.py --installed --node <node路径> --playwright <playwright目录> --browser <浏览器路径> --artifact-dir <结果目录>
```

`--installed` 验证已安装包；省略时验证源码。脚本会输出桌面/移动截图与操作结果，并删除临时数据库。P7 实施与测量报告见 [architecture/p7-implementation.md](architecture/p7-implementation.md)。仓库内 `mcp_server/MODULAR-RAG` 是独立知识库项目，不进入 FunAgent 发行包。
