# P5 实施与验收记录

实施日期：2026-10-10。工作树：`C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`，分支：`codex/framework-refactor`。依据 `tdd-refactor-plan.md` 第 9 节与 `lean-design.md`。P5 已完成；原 E 工作区及既有六份未提交设计文档未修改或纳入本阶段提交。

## 最终行为

ReviewScheduler 只有一个独立后台 worker，capacity 限制待处理会话数；运行 job 独立占一项。同会话待处理上界取最大值，运行 ReviewJob 不变。Schedule 验证完整已提交 Turn 边界；TaskProcess 只在成功 FinishTurn 后读取该任务的准确末尾 ReviewThrough 并调度，避免读取整个会话时混入新聊天。后台满容量/停止中不改写已经提交的主回复。GetStatus/CLI review 提供 running/pending/已处理上界、稳定失败代码和停止时的 cancelled_through，不展示复盘正文。

ReviewProcess 从成功水位获取固定 ReviewRange，读取最新情景记录，通过既有 ModelRuntime.Run 与四项 MemoryReviewStrategy API 运行。主业务/召回/复盘没有第二套模型循环。复盘使用独立 Event、run_id/deadline 和 ephemeral 范围，不创建主 Task，不调用 TaskProcess/Prepare，不写主聊天、冻结 Prompt 或工具历史。

固定复盘工具集为 memory_search/write/update/delete。每笔变更必须引用此范围内的真实 message IDs；模型不能提供 access、evidence 或 write_key。MemoryMgr 在 Store 分派前限制复盘写入 episodic 来源。Write 将来源、可信证据、类别和 NFKC/空白规范后的正文派生为稳定 key；换调用 ID、重新调度及扩大尚未成功范围时，相同证据/事实写入仍幂等。规范化不承诺语义等价去重。Update 使用真实不透明版本 CAS，冲突保留原内容且可见，后续尝试读新版本再决定操作。

只有 Run 完成、所有工具成功且至少一个变更有实际成功确认时才 AdvanceReview。普通 final/无变更为 NO_WRITES；部分失败、unknown、取消、迭代耗尽或冲突均不推进水位。已有成功写入保留，下一次调度从原水位重读，没有无限自动 retry。工具错误不暴露后端文本/凭据。

memory.sqlite3 的现有删除 tombstone 字段保留删除版本和选定可信证据回执。仅复盘可查询含回执的记录，回执暴露真实 record_id、原版本和证据；同版本/同证据的重复 Delete 可以确认已完成效果。不同证据、旧 CAS 冲突和普通管理操作不能假称确认。正常检索/画像不显示已删除事实，稳定 Write key tombstone 仍阻止复活。Write/Update/Delete 成功但 app 水位提交前中断均有恢复测试，两个库没有跨库原子事务。

PersistenceStore 增加 ReviewThrough/ReviewRange/ReviewWatermark/AdvanceReview。上下界须为同会话完整终态 Turn，工具批次/结果不被切断，正在执行聊天不能进入复盘。水位比较与提交在 BEGIN IMMEDIATE 事务内完成，真实双连接竞争只有一个成功，不覆盖更高进度。

启动只绑定启用复盘所需 profile/fallback，装配 ReviewScheduler 后再启动主 worker。policies.review.enabled 默认 false，显式 true 时校验可写情景来源；capacity/timeout 等非法值在发布配置前失败。停止先排空主 worker，再取消复盘运行/待处理并等待真实 IO 返回，最后关闭借用资源。OpenAIProvider 增加受控连接锁；Provider 与记忆 Store 的锁等待消耗同一 deadline，获得锁后再次检查，不在过期后发新 IO。

## 观察到的 Red / Green / Refactor

| 行为组 | 实际 Red | Green / Refactor |
| --- | --- | --- |
| 完整 Turn/固定范围/水位 CAS | 6 failed：薄 API 返回 UNSUPPORTED | 真实 SQLite 边界与事务；含 P2 持久化 19 passed |
| 可信变更/部分失败/无写入/稳定重试 | 8 failed, 11 passed：复盘流程尚未执行写入 | 复盘工具和同一 Run；含已有记忆合同 51 passed |
| 独立 worker/coalesce/停止实际 IO | 3 failed：后台未执行，状态未终结 | 有界待处理队列和独立控制信号；含任务/记忆流程 38 passed |
| 配置与共享 SDK 连接 | 4 个业务失败：非法 capacity 被发布，第二个 Send 没有等连接锁 | 校验 capacity/情景来源，共享小 controlled_lock；P5 + Provider 48 passed |
| 删除后水位中断 | 1 failed：成功删除后重试无法确认效果 | 复用 tombstone 作为可信删除回执；含记忆合同 71 passed |
| 同会话运行和待处理同时停止 | 1 failed：待处理取消上界未公开 | 保留 cancelled_through，运行退出后仍可查 |

初次并行主流程测试误将 Provider 输入 ModelRequest 当成 RunInput，读取了不存在的 evidence 字段；修正为检查编码后的 system message，属于测试替身错误，不算业务 Red。Refactor 把记忆 Store 与 Provider 共用的连接锁 helper 移到公共契约，固定工具定义/处理路径保持小函数，不增加容器、IO pool、通用恢复引擎或跨库事务。

## 验收结果

环境为既有 Python 3.12.13：`E:/FunAgent/.venv/Scripts/python.exe`。pytest 从当前重构工作树 src 导入新包。

- P5 `test_review.py`：40 passed。涵盖真实双数据库、CAS 竞争、固定范围、主任务并行、幂等、部分副作用、取消/unknown、写入/更新/删除水位中断恢复、后台异常后继续和启动故障清理。
- P0–P5 全套：299 passed，既有 P0–P4 的 259 项保留。全套逆序也通过；pytest 返回后 `funagent-*` 线程列表为空。
- 真实 OpenAI SDK MockTransport：等连接锁时期限耗尽，过期调用不发送 HTTP；默认无远程模型/知识连接与真实凭据。
- CLI 独立子进程冒烟：new → remember 主任务 → review 状态 → profile 查询 → 下一轮 recall → history → quit。app.sqlite3 两条 Task、四条主消息、零条后台工具记录，水位 2；memory.sqlite3 一条画像事实；退出码 0，临时目录/连接/进程清理完成。
- `examples/refactor-review` 配置校验通过。最终 git diff --check 和旧依赖扫描通过；新包无旧 agentcore/gateway/knowledge、Lease 或专用重复模型调用栈引用。

重现命令：

```powershell
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_review.py
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-review
```

离线操作见 `examples/refactor-review/README.md`。DemoProvider 的 remember: 仅演示事实提取；真实模型通过相同的工具边界执行。

## 代码量与资源所有权

生产 Python 按 src/funagent 的 `.py` 文件/非空行实测，含 docstring、SQL 和 DTO：P4 HEAD 30 模块 / 2,572 行；P5 32 模块 / 2,938 行，增加 2 模块 / 366 行。新增模块仅为复盘业务/工具和调度器，循环本身无改动。

| 所有者 | 新增状态/资源 | 关闭规则 |
| --- | --- | --- |
| ReviewScheduler | 一条 review 线程；Condition；有界 pending 会话队列；运行 job/Event；每会话状态 | Stop 停止准入，标记 pending 取消，发运行 cancel，实际 worker 退出后清除单例 |
| 每个 ReviewProcess | 固定范围、可信证据、局部模型 messages/工具结果 | ephemeral；退出不保存后台正文 |
| SQLiteStore | app 库 review_watermarks 表 | FinishTurn/AdvanceReview 各自原子；两 worker 退出后关闭连接 |
| SQLiteMemoryStore | 现有 tombstone 中可信删除版本/证据回执；无额外连接/线程 | 每个操作事务和受控锁；两 worker 退出后关闭 |
| OpenAIProvider | 每实例一个连接锁 | 锁等待受 deadline/cancel 控制；Close 等实际 Send 返回 |
| TaskMgr/MemoryMgr/ModelRuntime/ToolMgr | 保留既有共享实例和所有权；新增固定复盘工具绑定 | 主/复盘资源借用结束后关闭 |

## 剩余明确需求

P6 浏览器接入、P7 旧体系删除与独立发行尚未实施。无变更 final 当前保守地报告 NO_WRITES 并保留水位，不把模型正文当持久化证明。真实远程模型的事实质量属于后续实际配置联调；本阶段确定性验证真实 SQLite 和受控 Provider，不假称语义去重或外部跨崩溃 exactly-once。
