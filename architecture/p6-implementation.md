# P6 实施与验收记录

实施日期：2026-10-10。工作树：`C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`，分支：`codex/framework-refactor`。依据 `tdd-refactor-plan.md` 第 10 节和 `lean-design.md`。P6 已完成，P7 尚未实施。原 E 工作区源码和既有六份未提交设计文档保持原样，不纳入本阶段提交。

## 最终行为

`api/http.py` 包含薄 HTTPController 和一个 HttpServer。HTTPController 只做输入转换、调用已有公共 API 和白名单序列化；不访问数据库实现、不调用 Provider 或 ModelRuntime，不运行模型/工具、不等待任务终态。Submit 成功返回 202 和 task_id，模型执行仍在 funagent-main。断开连接、关闭监听或刷新页面不触发 Cancel；重新连接查询同一 task_id 获得权威快照。

实现全部 P6 最小路由：

| 入口 | 成功响应 / 业务 API |
| --- | --- |
| POST /sessions | 201，会话公开快照；SessionMgr.Create |
| GET /sessions | 200，items/next_cursor；SessionMgr.List |
| GET /sessions/{id}/messages | 200，原始主历史；SessionMgr.ReadHistory |
| POST /sessions/{id}/tasks | 202/task_id；TaskMgr.Submit |
| GET /tasks/{id} | 200，task_id/session_id/status/result/时间；TaskMgr.Get |
| POST /tasks/{id}/cancel | 200，真实状态/requested；TaskMgr.Cancel |
| GET /tasks/{id}/interactions | 200，当前请求；InteractionMgr.Pending |
| POST /interactions/{id}/answer | 200/answered；InteractionMgr.Resolve |

列表复用 limit/cursor 与 Store 的稳定分页；历史以完整 Turn 分页，保留工具请求/结果配对。创建会话只接受 title；提交任务只接受 text；人工答复接受 task_id/run_id/answer，批准必须是布尔值。workspace 来自可信启动配置。access、record_scope、handler、profile、prompt 等额外字段在业务调用前被拒绝，不能通过 HTTP 改写策略。澄清答案仍在原任务等待点恢复，不经过主队列、不增加 user Turn。

公开会话不含 workspace 或冻结 Prompt，任务结果只含 final/reason/error_code，不自动序列化上下文、凭据、profile 或后台状态。缺失对象 404、非法参数/JSON 400、满容量 429、停止/未就绪 503、重复/过期/错 run 的答复 409。后端异常文本和未知错误代码统一脱敏。JSON 最多 65536 字节，拒绝重复字段、非 JSON、非法长度与 Transfer-Encoding；HTTP 请求 socket 有 5 秒超时。变更请求拒绝外源 Origin，页面静态资源含 CSP/no-store/nosniff，所有用户/模型文字使用 textContent 展示。

页面提供新建/选择会话、提交、完整历史、当前任务状态、取消、人工回答和批准/拒绝。每会话最近 task_id 保存在 localStorage，刷新和会话切换后查询原任务；旧页面请求不能更新新会话。终态停止 polling，人工输入在活动 polling 中保留。桌面和手机宽度可操作，无横向溢出。页面只读取主历史，后台 review/recall 的临时文本不会出现；主模型主动输出的事实/引用仍属于正常主回复。

启动入口独占 `_http_server` 资源。cli 不建 listener；web 启动后等待；both 启动 listener 并运行 CLI，业务管理器均只装配一次。停机先关闭 HTTP 准入并等请求线程结束，再取消/排空主 worker、复盘 worker，然后关闭人工等待、记忆、主库、模型和工具资源。HTTP bind/thread 启动失败会清理已装配资源，可以再次启动。没有新增 EventHub、渠道、UI 业务包装器或第二套会话/模型引擎。

## 观察到的 Red / Green / Refactor

| 行为组 | 实际 Red | Green / Refactor |
| --- | --- | --- |
| HTTP 路由、异步提交、断线、白名单和错误 | 26 failed；可收集的最薄声明返回 UNSUPPORTED | HTTPController/HttpServer 与三份静态资产；26 passed |
| web/both、原任务人工答复、记忆隔离、启动清理 | 14 failed, 1 passed；启动仍拒绝 web/both | 唯一 listener 接入启动/Stop，复用现有业务 API |
| 真实浏览器会话切换 | Chrome 在切回原会话后等待人工输入控件超时 | 按会话保存 task_id，并以请求版本防止旧响应污染当前页面；切换/刷新/原任务恢复通过 |
| 过多查询参数与非法方法 | 1 failed, 1 passed；查询解析 ValueError 被转换为 500 | 解析错误转 INVALID_REQUEST；非法方法固定 405 |

拒绝批准文案的初次测试误写为 Approval denied，既有行为为 Approval declined；浏览器失败冒烟曾误期望 PROFILES_EXHAUSTED，AUTH 按策略直接 fatal。两项修正均为测试预期纠正，不算业务 Red。Refactor 保持工具/任务/记忆 API 不变；只提取字段验证、分页和白名单编码 helper。

## 验收结果

环境为既有 Python 3.12.13：`E:/FunAgent/.venv/Scripts/python.exe`。pytest 从重构工作树 src 导入，无真实远程模型/知识连接和凭据。

- P6 两份测试：43 passed，真实 loopback HTTP、临时 SQLite 和共享 worker；覆盖异步提交、完整结果、CLI 可查浏览器任务、正确工具配对、断线重查、人工批准/拒绝/澄清、取消/过期/重复/错任务答复、参数限权、错误脱敏、分页和启动失败。
- P0–P6 全套：342 passed in 17.80s；原有 299 项通过，增加 43 项。pytest.main 返回后除主线程外线程列表为空。
- 真实 Chrome 154.0.8037.98 + Playwright：10 组页面操作通过，零 pageerror。新建/选择→正常回复→工具→刷新同一 pending→切换回原会话→答复→真实文件读取批准/拒绝→取消→显示失败→后台记忆更新/下一轮召回→桌面/手机布局。XSS 样例以纯文本显示。
- 浏览器脚本使用临时五份配置、app/memory SQLite、实际 HTTP 和主/复盘 worker；退出后浏览器上下文/进程、应用线程、连接与临时目录全部清理。失败显示用受控 SmokeProvider 注入 AUTH，其余使用既有 DemoProvider。
- 内置浏览器控制和默认 shell 因 Windows sandbox helper 的 setup refresh 错误无法启动；经已授权的沙箱外执行，用既有 Chrome/Playwright 完成真实页面检查，未把环境故障当业务 Red。未安装新浏览器/依赖。
- examples/refactor-web 五份配置校验通过；git diff --check、旧架构引用扫描通过。

重现：

```powershell
& E:/FunAgent/.venv/Scripts/python.exe -m pytest tests_refactor/test_http.py tests_refactor/test_web_flow.py
& E:/FunAgent/.venv/Scripts/python.exe -m pytest tests_refactor
$env:PYTHONPATH = "src"
& E:/FunAgent/.venv/Scripts/python.exe -m funagent --conf-dir examples/refactor-web
```

真实浏览器脚本 `scripts/refactor_browser_smoke.py` 另需已有 Node、Playwright、Chrome；参数说明见 `examples/refactor-web/README.md`。本次截图和 JSON 操作记录位于：

`C:/Users/ql136/.codex/visualizations/2026/10/10/01a1243d-415c-7d73-b37a-04f2e015ba02/` 的 `p6-browser.png`、`p6-browser-mobile.png`、`p6-browser-results.json`。

## 代码量与资源所有权

生产 Python 按 src/funagent 的 .py 文件/非空行实测，含 docstring/DTO/SQL：P5 HEAD 32 模块 / 2938 行；P6 33 模块 / 3176 行，增加 1 模块 / 238 行。另有三份 HTML/CSS/JS 静态资产；浏览器脚本属于验收设施。

| 所有者 | 状态/资源 | 关闭规则 |
| --- | --- | --- |
| global_server | 唯一 HttpServer | 先关闭监听和请求线程，再关闭业务 worker/共享资源 |
| HttpServer | 一个 HTTP serve 线程、监听 socket；临时 request 线程 | 请求不持有模型/工具，socket 超时有界；Close 等实际 handler 返回 |
| HTTPController | 无业务状态 | 数据转换和公开 API 调用 |
| 浏览器页面 | 当前会话、每会话最近 task_id、polling 和请求版本 | 刷新/关闭不取消任务；Cancel 必须显式调用 |
| 既有 TaskMgr/ReviewScheduler/MemoryMgr/ModelRuntime/ToolMgr/DbMgr | 共用现有实例和连接 | 保留 P2–P5 资源借用和排空规则 |

## 剩余明确需求

P7 的旧架构删除、依赖清理、新包独立发行和安装冒烟尚未实施。P6 使用完整结果 polling；当前无 token 流明确需求，未引入 streaming。记忆管理没有新增产品操作需求，本阶段不增加 HTTP 记忆 CRUD 页面/路由。
