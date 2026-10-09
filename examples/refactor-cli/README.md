# P2 离线 CLI

在重构工作树根目录运行。此配置使用明确的 DemoProvider，不访问网络或凭据，echo 是无副作用的演示工具。

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

`--check-config` 仅校验五份 TOML 文件，不启动连接或线程。CLI 暂无人工答复和浏览器；分别由 P3/P6 实施。
