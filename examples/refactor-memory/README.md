# P4 统一记忆 CLI 示例

在重构工作树根目录运行。此配置使用离线 DemoProvider 和实际本地 HTTP 知识查询，不需要模型凭据。知识端点是可检查的示例文档；情景画像使用独立 `memory.sqlite3`，主历史使用 `app.sqlite3`。

先在一个终端启动只读示例知识服务：

```powershell
& E:\FunAgent\.venv\Scripts\python.exe examples/refactor-memory/knowledge_server.py
```

再在另一个终端运行 CLI：

```powershell
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-memory
```

依次输入；`new`、`submit` 和 `memory write/update` 返回的 ID/版本用在下一步：

```text
memory write facts profile 用户喜欢中文
memory search * 中文
memory list facts profile
new 记忆演示
submit <session_id> memory: 中文
wait <task_id>
history <session_id>
memory update facts <record_id> <version> 用户现在喜欢英文
submit <session_id> hello
wait <task_id>
memory delete facts <record_id> <更新后的version>
quit
```

`memory: <查询>` 是离线 DemoProvider 的召回演示语法：一次 Prepare 通过只读 memory_search 查询授权来源，主 Run 展示真实记录及 citation。普通问候的 Prepare 不检索；真实模型由同一策略自行决定是否使用检索工具，不需要这个前缀。更换 `model.conf` 为 `examples/refactor-openai/model.conf` 并设置对应环境凭据即可联调模型。

memory.conf 的 `kind`、`capabilities`、`read_access/write_access` 声明可信来源与访问范围。CLI 是本地管理入口；主任务与召回只获配置来源的读权限。模型工具只有 text/sources/kind/types/limit，不接受 access、evidence 或 write_key；CLI 写入使用可信的单次 action 标识与证据。update/delete 必须提供来源的不透明版本，冲突返回 CONFLICT。

每轮直接读取最新 profile 类别的情景事实，然后最多一次 Prepare。召回 messages、查询正文和模型摘要不写主历史，证据只注入本次主 Run，基础 Prompt 保持冻结。跨来源结果交错保留各自顺序，不比较 score；max_records/max_chars 限制真实证据预算。模型 final 文本不成为新事实。来源失败/子超时可返回部分证据与 warnings；父取消或总 deadline 继续终止任务。

将 knowledge.endpoint 改为真实固定只读查询地址，可使用 `credential_env` 引用 Bearer 凭据。请求 JSON 是 `{"text":"问题","types":[],"limit":10}`；响应 JSON 是 `{"records":[{"record_id":"doc-1","type":"document","content":"真实正文","citation":"https://...","version":"可选来源版本"}]}`。引用须由查询来源提供；非法/重复/伪造来源记录被拒绝，score 不参与跨源排序。

宿主也可在 Start 前注册 `global_server.MEMORY_QUERIES[callback_id] = factory`；factory(settings) 只在启动调用一次，返回 `query(MemoryQuery, RunContext) -> list[dict]`。该固定 callback 可绑定宿主已有的只读 MCP 查询；需要遵守 deadline、取消和线程安全合同。未建设通用绑定容器或第二套模型循环。

已有 refactor/refactor-cli/refactor-openai 示例显式 `enabled = false`，可管理记忆并直接读取画像；选用本示例开启按需召回。`--check-config` 只校验配置。后台复盘在 P5 开发。
