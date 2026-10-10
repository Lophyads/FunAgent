# P3 工具与人工交互 CLI

在重构工作树根目录运行。此配置使用 DemoProvider，不访问模型网络或凭据。echo 回显数据；read_workspace 实际读取会话工作区文件，每次调用须人工批准；ask_user 请求澄清并恢复原任务。

```powershell
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-cli
```

输入 `new 我的会话` 获得 session_id，再执行：

```text
submit <session_id> hello
wait <返回的 task_id>
submit <session_id> tool: echo me
wait <返回的 task_id>
get <task_id>
history <session_id>
cancel <task_id>
sessions
quit
```

`submit` 返回 ID，模型与工具在主 worker 执行。`get` 显示已提交状态和完整回复；`wait` 查询至终态。`history` 读取完整 Turn，展示工具 ID、结果和副作用状态。`cancel` 对 running 任务只发出请求，真实 IO 返回前仍占容量；queued 取消不写 user。退出会先停止准入、取消并等待 worker，再关闭资源。

`serv.conf` 中 `capacity` 包含 running 与 queued；`max_iterations`、`task_timeout`、`system_prompt` 是本阶段新增可选设置。相对数据库路径以配置目录为基准，默认是此目录下新 schema 的 `app.sqlite3`，再次启动能查询已完成任务。主历史不会保存系统 Prompt 文本或 ephemeral 消息；Prompt 单独冻结在会话记录中。

`--check-config` 仅校验五份 TOML 文件，不启动连接或线程。浏览器接入在 P6 实施。

P3 演示在同一会话执行：

```text
submit <session_id> read: pyproject.toml
wait <task_id>
yes
submit <session_id> ask: 你希望阅读哪个文件？
wait <task_id>
architecture/p2-implementation.md
```

`wait` 一边查询任务，一边显示 pending；批准输入 yes/no，澄清输入正文。错误答复保留原请求，可继续回答。输入 `/cancel` 或输入流 EOF 会取消原任务。也可通过 `pending <task_id>` 查看请求，再执行 `answer <task_id> <interaction_id> <正文或yes/no>`。答复直接唤醒原等待点，不占主队列、不新建 user Turn。重复、过期或不属于当前 pending 的答复报 `INTERACTION_STALE`。

`serv.conf` 的 workspace 是创建会话时固定的可信目录，`.` 指启动目录；新会话保存规范化绝对路径。读取参数只有相对 path，禁止路径穿越、绝对路径、目录链接越界、Windows 设备/数据流路径；仅读取 UTF-8 普通文件，最多 65,536 字节。read_workspace 的 `requires_approval = true` 在 ToolMgr 分派前检查，批准只用于当次调用和参数，不产生长期授权。工具 schema 使用 JSON Schema 2020-12，只允许文档内引用；非法参数在 handler 执行前拒绝。

`interaction_timeout` 是单个人工请求上限，本配置为 30 秒；实际期限还受整次任务 task_timeout 约束。人工请求单独过期会生成无副作用 error 工具结果，模型可以继续；任务取消或总 deadline 到期保持控制语义。review/recall 不能调用人工工具。关闭时先取消和唤醒等待，再等待主 worker 实际退出，最后关闭 Provider/Store。
