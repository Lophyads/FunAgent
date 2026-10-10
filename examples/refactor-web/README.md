# P6 离线浏览器示例

从重构工作树根目录运行：

```powershell
$env:PYTHONPATH = "src"
python -m funagent --conf-dir examples/refactor-web
```

打开启动输出的 http://127.0.0.1:8080，新建或选择会话，发送消息并查看完整回复。此示例使用 DemoProvider，不连接远程模型或知识库。五份配置沿用新格式，工作区是启动时的当前目录，主库和情景记忆库保存在此配置目录。

- 普通文本：返回 `Demo: ...`。
- `tool: 内容`：通过 echo 工具返回。
- `ask: 请选择颜色`：页面输入答案后恢复原任务。
- `read: examples/refactor-web/note.txt`：批准或拒绝本次实际文件读取。
- `remember: 我喜欢蓝色`：主回复提交后后台复盘写入情景画像。
- `memory: 蓝色`：下一轮读取实际记忆证据。页面只显示主历史，不显示复盘或召回临时对话。

等待人工答复时刷新或切换会话不会取消任务；回到原会话可继续答复。页面的“取消任务”按钮才发送取消请求。关闭浏览器不会停止应用，终端 Ctrl+C 停止 web 模式并排空 worker。

将 serv.conf 的 mode 改为 `both` 可同时使用 CLI 和浏览器，共享会话、任务、人工请求、记忆和模型资源；CLI quit 关闭整个应用。`cli` 模式不创建 HTTP 监听资源。监听 host/port 沿用 serv.conf，本示例使用本机监听。

P6 路由：POST/GET /sessions；GET /sessions/{id}/messages；POST /sessions/{id}/tasks（202/task_id）；GET /tasks/{id}；POST /tasks/{id}/cancel；GET /tasks/{id}/interactions；POST /interactions/{id}/answer。最后一项 JSON 包含 task_id、run_id、answer；批准为布尔值，澄清为文本。列表返回 items/next_cursor，查询参数为 limit/cursor。

页面按会话在 localStorage 保存最近 task_id，刷新后从服务端读取权威快照；终态任务停止 polling。没有 token streaming 或全局事件 Hub。

真实浏览器冒烟可通过 scripts/refactor_browser_smoke.py 运行，另需已有 Node、Playwright 和 Chrome/Chromium，使用 --node、--playwright、--browser、--artifact-dir 指定路径。脚本创建临时配置和两份 SQLite 库，检查页面操作后关闭浏览器和所有应用资源，不使用此示例的持久数据。
