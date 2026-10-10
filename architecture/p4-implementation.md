# P4 实施与验收记录

实施日期：2026-10-10。工作树：`C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`，分支：`codex/framework-refactor`。依据 `tdd-refactor-plan.md` 第 8 节与 `lean-design.md`。P4 已完成；原 E 工作区及既有六份未提交设计文档未修改或纳入本阶段提交。

## 最终行为

MemoryMgr 统一 semantic/episodic 来源、可信访问范围和能力分派。GetProfile/Search/List/Write/Update/Delete 直接访问 Store，不调用模型。来源由 memory.conf 声明 kind、capabilities、read_access/write_access；只读或不具备能力的操作在 Store 分派前返回 UNSUPPORTED，未授权返回 DENIED。模型检索工具只接受查询、来源/类别过滤和数量，不能提交 access/evidence/write_key。

默认情景实现 SQLiteMemoryStore 使用独立 memory.sqlite3；真实 Unicode 查询使用 NFKC/casefold 和字面子串匹配，不将 `%/_` 当 SQL 通配符。List 使用绑定来源/类别的稳定 seq 游标。Write 的来源内稳定 key、原始 payload 指纹与 UNIQUE 键在 BEGIN IMMEDIATE 事务中实现原子幂等；同 key 不同内容冲突。Update/Delete 使用不透明版本比较和单事务修改；已删除记录保留 key tombstone，旧写入不能重新生成已删除事实。公开错误隐藏 SQL、路径和外部响应。

CallbackMemoryStore 使用启动绑定一次的固定只读 query callback，校验实际记录 ID、来源、类型、正文和 citation；重复/伪造来源/缺引用数据被拒绝。默认 http_json callback 使用标准库 urllib，对固定端点发送 JSON 查询，接收真实记录；环境凭据仅启动时读取，传输错误与不合法响应转换为稳定公开错误。默认不引入 MCP SDK 或旧 KnowledgeProvider/RetrievalPlanner。宿主可以在启动映射注册一个固定 MCP query callback，连接所有权由宿主保留，停止借用后关闭。

MemoryRetrievalStrategy 只有四项配置 API，通过既有 ModelRuntime.Run 与固定只读 memory_search 工具运行。Prepare 创建局部 ephemeral 输入和独立 run_id，继承父 cancel Event 并缩短 deadline；管理器只短暂锁定证据账本，不跨 Run/Search 持有全局锁。收集的证据必须来自此次真实 Search 返回的记录，模型 final/摘要/伪造引用全部忽略。多来源结果保留来源内顺序并交错，不比较跨源 score；真实引用去重与 max_records/max_chars 预算采用共享小 helper。

来源失败保留其他来源记录，并返回 source_status/warnings。来源局部超时和召回子 deadline 允许部分/空降级；查询中子超时也保留此前成功来源的实际记录。父 cancel/总 deadline 在返回主 Run 前再次检查并继续传播。受控并发测试证明两次 Prepare 的查询和证据不串用，召回工具回访 MemoryMgr 不自锁；Callback 的共享连接锁等待也消耗 deadline，过期不再发新 IO。

TaskProcess 在 BeginTurn 后直接读取最新 profile，然后每轮一次 Prepare，将真实画像和召回证据仅注入本次主 RunInput。MainStrategy 和唯一 ReAct 主循环保持原职责。基础 Prompt 仍冻结；app.sqlite3 主历史不存召回 messages、工具调用/查询正文或模型摘要。更新画像后，下一主轮立即看到新内容。

启动绑定主模型和启用召回所需 profile/回退、固定检索工具和配置来源；TaskMgr worker 最后启动。启动失败关闭已经创建的记忆 Store；Stop 先停止/取消并等待主 worker，再关闭 MemoryMgr 所有的 Store 和其他资源。没有新增 worker/IO pool/复盘调度器。

CLI 增加 memory search/list/write/update/delete，通过公共 MemoryMgr 调用，返回可用于 CAS 的记录 ID/版本；写入 key 与证据来自可信的本地管理 action。新 `examples/refactor-memory` 可启动实际只读 HTTP 示例知识服务和 CLI，同时查询真实情景事实与外部文档；DemoProvider 的 `memory:` 前缀只服务离线演示。已有三组示例显式 disabled recall，仍支持直接画像/管理；本示例开启召回。

## 观察到的 Red / Green / Refactor

| 行为组 | 实际 Red | Green / Refactor |
| --- | --- | --- |
| 两种 Store、Unicode、引用、幂等/CAS/分页 | **10 failed**，可收集 API 尚未实现查询/写入 | 真实 SQLite 事务/唯一键、固定 callback 规范；**10 passed** |
| 统一分派、能力/权限、真实证据、来源失败、控制传播 | **8 failed**，薄 MemoryMgr 返回 UNSUPPORTED | 可信来源预检、此次检索账本、同一 Run；两组测试 **18 passed** |
| 主任务、CLI 管理、启动清理 | 缺失 CLI 命令/记忆装配和不可用驱动未拒绝；首次模型替身装配遗漏 fallback，属于测试装配问题，未当作业务 Red | 主任务一次 Prepare、最新画像、生命周期和 CLI 接入；P4 三份测试 **21 passed** |
| 配置/并发/期限边界 | **9 failed, 65 passed**：非法能力/空路径/范围/预算发布，越权召回缺诊断，查询中子超时丢失已成功来源证据 | 强化配置校验、显式拒绝 warnings、超时前保留真实结果；含主流程 **77 passed** |
| 真实 HTTP/SDK 合同 | 新查询驱动受控 transport 和实际 loopback HTTP 验证；超大 bytes 参数默认 ID 导致 Windows 临时路径错误，属于测试环境问题 | 短参数 ID，真实 OpenAI SDK 的 recall→main；HTTP 套件 **9 passed** |

Refactor 将记录序列化、可信证据去重/预算和受控连接锁收缩为小 helper；无字符串服务容器、通用 PortBinding、复制的模型 retry 或第二套 ReAct 循环。测试检查使用的 SQLite 连接显式 closing，CLI 冒烟临时目录正常清理。

## 验收结果

在已有受支持的 **Python 3.12.13** 环境 `E:/FunAgent/.venv/Scripts/python.exe`，通过 pytest pythonpath 从当前重构工作树导入新包，未导入原 E 源码。

- P0–P4：**259 passed**，其中新增 P4 记忆测试 **44** 项，新增配置边界 **7** 项；无 warning。既有 P0–P3 **208** 项全部保留并通过。
- SQLite integration：真实查询/稳定游标、跨两个连接的并发幂等及 CAS、失败回滚、两个数据库历史隔离。
- 实际 loopback HTTP 查询 + OpenAI SDK MockTransport：recall 与 main 均走同一 Run，主输入含两类实际证据及 citation，没有模型伪造摘要。
- 独立 CLI 子进程：memory write/search → new/submit/wait/history/quit 成功，返回两类记录；原始 app.sqlite3 工具表为 **0** 条，进程和示例服务退出、临时目录清理完成。
- 检查配置冒烟、全套逆序执行及 git diff --check 结果在本阶段提交前验证；未访问真实远程模型/知识服务或使用实际凭据。

重现命令（重构工作树根目录）：

```powershell
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_memory_store_contract.py tests_refactor/test_memory_prepare.py tests_refactor/test_memory_flow.py tests_refactor/test_memory_http.py
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-memory --check-config
```

实际双来源 CLI 操作步骤与外部 JSON/callback 合同见 `examples/refactor-memory/README.md`。

## 代码量与资源所有权

生产 Python 按 src/funagent 内 `.py` 文件/非空行实测，含 docstring、SQL 与公开 DTO：P3 HEAD **21 模块 / 1,833 行**；P4 **30 模块 / 2,572 行**，新增 **9 模块 / 739 行**。新增域为记忆 DTO/协议、两个真实 Store、固定 HTTP query、管理器、召回策略/工具及共享验证 helper；没有旧包依赖。直接外部依赖仍是已用的 jsonschema 与 OpenAI SDK，其余新增实现使用标准库。

| 所有者 | 本阶段新增状态/资源 | 关闭规则 |
| --- | --- | --- |
| MemoryMgr | 固定来源/Store 映射；正在 Prepare 的可信检索账本及短 Lock | 主 worker 退出后关闭各 Store；账本在 Prepare finally 移除 |
| SQLiteMemoryStore（每 source 一个实例） | 独立连接、实例 Lock；数据库记录/版本/key tombstone | 每操作有界等待和事务；使用者退出后关闭连接 |
| CallbackMemoryStore | 启动绑定 query、实例 Lock、closed 标记 | 等实际查询返回后关闭；宿主 callback 连接由原所有者关闭 |
| HTTPMemoryQuery | 固定 endpoint/timeout/可选凭据与 transport | 每次响应以 with 关闭，无永久客户端/工作线程 |
| 每个 recall Run | messages、cancel/deadline、迭代/工具结果、真实查询/记录 | 局部输入，ephemeral；不写主库、不修改主 messages |
| TaskMgr / DbMgr / ModelRuntime | 保留 P3 的一个主 worker、主连接与 Provider 实例 | Stop 等 worker 真实退出后关闭借用资源 |

## 剩余明确需求

P5 的独立复盘 worker、固定完整 Turn 上界、水位/事实写入工具与失败恢复尚未实施。P6 浏览器和 P7 旧体系删除/独立发行仍按原计划推进。真实远程知识服务需要遵守示例中的查询协议或提供固定只读 callback；确定性验收使用实际本地 HTTP 和受控模型 transport。没有增加未经需求验证的通用插件体系、永久授权或额外并发主 worker。
