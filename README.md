# FunAgent

FunAgent 是单机、单应用进程的 Agent。CLI 和浏览器共享持久会话、异步任务、人工澄清/单次批准与统一记忆；主业务、召回、后台复盘共用一个 ReAct 内核。

需要 Python 3.11–3.13。发行包只有 `funagent`，命令为 `funagent` 或 `python -m funagent`。

## 安装与运行

在源码或解压后的 sdist 目录运行：

```powershell
uv sync --locked
uv run funagent --conf-dir examples/refactor-cli --check-config
uv run funagent --conf-dir examples/refactor-cli
```

也可以安装 wheel：

```powershell
python -m pip install dist/funagent-0.2.0-py3-none-any.whl
funagent --conf-dir <配置目录>
```

配置目录必须含五份 TOML 文件：`model.conf`、`tool.conf`、`memory.conf`、`log.conf`、`serv.conf`。示例随 sdist 提供；安装 wheel 后可以复制这些配置到独立目录。数据库路径相对配置目录，workspace 为启动时的当前目录或显式绝对路径。使用新的配置目录和 `app.sqlite3` / `memory.sqlite3`；历史 API、配置与数据库没有兼容或自动迁移。

`serv.conf` 的 `mode` 选择 `cli`、`web` 或 `both`。浏览器示例：

```powershell
uv run funagent --conf-dir examples/refactor-web
```

打开终端显示的地址（默认 http://127.0.0.1:8080）。`both` 同时提供 CLI 和 HTTP；CLI `quit` 关闭整个应用。关闭或刷新浏览器后仍可查询原任务，只有显式取消才发送取消请求。Ctrl+C 停止应用：先停止准入、取消任务/人工等待与复盘，再等 worker/实际 IO 返回并关闭共享连接。

## 离线体验

`examples/refactor-cli` 和 `examples/refactor-web` 使用离线 DemoProvider，无需模型凭据。CLI 支持：

```text
new 我的会话
sessions
submit <session_id> hello
wait <task_id>
history <session_id>
get <task_id>
cancel <task_id>
pending <task_id>
answer <task_id> <interaction_id> <答案>
quit
```

`wait` 期间可以直接回答澄清/批准。发送 `tool: 内容` 使用 echo；`ask: 问题` 请求澄清；`read: 文件路径` 请求单次批准后读取工作区文件。浏览器提供同一闭环。

`examples/refactor-review` / `refactor-web` 启用本地记忆与独立复盘：`remember: 我喜欢蓝色` 完成后后台写入情景事实，`memory: 蓝色` 在后续轮次读取实际证据。`examples/refactor-memory` 展示外部只读语义来源；它与情景事实共用 MemoryMgr。CLI 还提供 `memory search/list/write/update/delete` 和 `review <session_id>`；网页仅暴露会话、任务与人工答复。

## 真实模型

OpenAI 兼容 Chat Completions 驱动是可选依赖：

```powershell
uv sync --locked --extra openai
uv run --extra openai funagent --conf-dir examples/refactor-openai
```

设置配置指定的凭据环境变量（默认 `OPENAI_API_KEY`），填写 `model.conf` 的模型名称和端点。配置不保存密钥。未选的可选驱动无需安装；所选驱动缺失会报 `DRIVER_UNAVAILABLE`，缺凭据报 `CONFIG_CREDENTIAL`。Provider 单次 IO，SDK 自动重试关闭；有界 retry/fallback 由公共内核处理。

## 开发与交付验证

```powershell
uv sync --locked --extra dev --extra openai
uv run --extra dev --extra openai pytest tests_refactor
uv build --out-dir dist
python scripts/release_smoke.py --wheel dist/funagent-0.2.0-py3-none-any.whl --sdist dist/funagent-0.2.0.tar.gz
```

测试默认使用脚本模型、受控 transport 和临时 SQLite，不连接真实模型或知识库。release smoke 在临时独立环境安装 wheel 和解压后的 editable 包，从无关目录运行 cli/web/both，检查安装入口、页面静态资源、工具/人工答复/记忆闭环与资源关闭。

真实 Chromium 操作 smoke 另需 Node、Playwright 和 Chrome/Chromium：

```powershell
python scripts/refactor_browser_smoke.py --installed --node <node路径> --playwright <playwright目录> --browser <浏览器路径> --artifact-dir <结果目录>
```

`--installed` 验证已安装包；省略时验证源码。脚本会输出桌面/移动截图与操作结果，并删除临时数据库。P7 实施与测量报告见 [architecture/p7-implementation.md](architecture/p7-implementation.md)。仓库内 `mcp_server/MODULAR-RAG` 是独立知识库项目，不进入 FunAgent 发行包。
