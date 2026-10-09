# P2 实施与验收记录

实施日期：2026-10-10。工作树：`C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`，分支：`codex/framework-refactor`。依据 `tdd-refactor-plan.md` 第 6 节与 `lean-design.md`；仅实现 P2，原 `E:/FunAgent` 源码和暂存修改未处理。

## 目标与最终行为

SQLite 主记录由 PersistenceStore/SQLiteStore 持有，DbMgr.GetInstance 直接返回协议对象；SessionMgr 无缓存。首次执行冻结基础 Prompt，冲突拒绝，同文本不重复 UPDATE。BeginTurn 同事务写 running 和唯一 user，并返回包含本轮 user 的新快照。

TaskMgr 拥有一个主 worker、有界准入和待执行队列。容量计所有已接收且未终结任务，包含 running；Submit 持准入锁登记与发布，发布异常补偿终结未发布登记、移除队列占位。注册失败不占容量。若存储持续失败导致补偿/终态也不能提交，停止准入，保留未决容量，不运行未发布登记、不宣称完成；停止后重启通过 RepairInterrupted 修复。没有任务接管、Lease、线程强杀或 IO pool。

TaskProcess 只执行读取/冻结/Begin/唯一主 Run/Finish。Prepare 尚未启用，没有空 MemoryMgr。ModelRuntime 的 main_history 工具请求整批先提交，再执行 handler；结果写失败使 Run failed，并保留已知真实效果供 Finish 补齐。ephemeral 不写主记录。Finish 在同事务保存成功 final 和 completed，失败/取消/limit 不添加虚构 final；取消与提交竞争以数据库已提交结果为准。

running Cancel 只设置 Event，实际 IO 返回前仍占执行位置；queued Cancel 直接终结，不写 user。重启将遗留 queued/running 标记 interrupted，已知工具结果保留，缺失结果为 unknown/unknown effect，不重放工具。历史以完整终结 Turn 为分页单元，limit 是软消息预算，游标固定快照上界，工具批次及其结果不被分页切断；执行中未配对 Turn 不进入公开历史页。

CLI 仅使用 ConfMgr 的接入配置、SessionMgr 与 TaskMgr 的应用 API。支持 new/sessions/submit/get/wait/cancel/history/quit，显示完整回复。生产装配注册明确的 demo/OpenAI Provider、demo/OpenAIChatAdapter 与无副作用 echo 演示 handler。真实驱动以受控 HTTP transport 验证，无网络、真实凭据或模型费用。

## 观察到的 Red / Green / Refactor

| 行为组 | 实际 Red | 最小 Green / 后续结果 |
| --- | --- | --- |
| Prompt 冻结、Begin 快照 | **2 failed**，薄声明 NotImplementedError | SQLite 会话/Turn 事务后 P0–P2 **109 passed** |
| 工具配对、final 事务、重启修复、分页 | **9 failed, 2 passed** | 请求/结果唯一键、具体事务与完整 Turn 游标后 **118 passed** |
| FIFO、容量、取消、发布失败、停机 | **9 failed**，TaskMgr 薄声明 | 单 worker + Event + main_history 记录后 **127 passed** |
| CLI、真实 SDK transport | **9 failed, 9 passed** | CLI 应用 API 与单次 SDK 驱动；SDK 首次加载暴露测试时间噪声，改用受控 monotonic 验证 timeout |
| 生产 CLI 装配与启动清理 | **4 failed, 18 passed** | 启动注册表、资源清理、离线配置后 **140 passed** |
| Review 的提交确认/存储故障停机边界 | **2 failed, 31 passed**；修复后仍有 **1 failed, 146 passed** 的停机确认竞态 | 已提交终态优先；先取消并 join，再报告未决记录并清理资源，**147 passed** |
| 真实 SDK→工具→SQLite 完整 Turn | 加入既有合同的组合验收，通过 | 最终 **148 passed** |

每组先观察缺失目标行为的可收集测试；沙箱启动故障不计业务 Red。SQLite rollback 由真实触发器 RAISE(ABORT) 注入，完整工具批次在 handler 内通过独立连接观察并在测试外断言。并发用 Event 与有限等待，测试 teardown 先等 worker，后关 Provider/Store。

绿色下提取实际重复的 thaw 编码辅助；DbMgr 不复制 Store CRUD；SessionMgr 不缓存；TaskMgr 不组装模型消息；三类用途仍只有已有的 ModelRuntime.Run。复盘水位 API 与 memory/interaction/web 资源按后续阶段引入，没有提前生成未使用接口。

## 最终验证

- 正常顺序：**148 passed**。
- 逆序执行：**148 passed**；进程内检查剩余 `funagent-*` worker 为 **[]**。
- 真实 CLI 子进程：Submit → 返回 task_id → wait → completed 完整回复 → 配对工具 history → quit → 重开数据库读取同一 completed，全部成功。
- 离线与真实 Provider 两份配置 `--check-config` 冒烟成功；真实配置模型名是明确占位值，纯校验不校验远端模型可用性。
- `git diff --check` 通过；新生产包无旧包、Lease/渠道、ProviderRunner、IO pool 或空 MemoryMgr 引用。
- 新生产包 **17 个 Python 模块、1,528 非空行**；P1 是 9 模块/653 非空行。本阶段新增持久事务、主 worker、用例/CLI 和两个可用驱动；测试/配置/文档不计入生产行数。

## 状态与资源所有权

| 所有者 | 状态/资源 | 生命周期 |
| --- | --- | --- |
| ConfMgr | 五份只读配置快照与生命周期锁 | 装配前加载；最后清除 |
| DbMgr → SQLiteStore | 1 个主 SQLite 连接、RLock；sessions/tasks/messages/tool_calls 表 | 操作自身提交/回滚；worker 退出后 Close |
| TaskMgr | 1 个主 worker、Condition、待执行 deque、非终态 context 映射与 running ID | 停止准入、取消全部 Event、等 worker 退出；无服务对象进入 context |
| ModelRuntime | profile/provider/adapter 固定映射 | 不保存对话；所有借用结束后按连接身份关闭 Provider |
| OpenAIProvider | 1 个 SDK/HTTP 客户端，每 provider ID 独立 | SDK retry 禁用；同步请求实际返回后才能关闭 |
| ToolMgr | 固定 schema/handler 注册表 | P2 仅无副作用 echo；无真实后端连接 |
| 每个 Run | 局部 messages、pending/seen、结果、usage、次数、profile | main_history 仅调用 DbMgr 的具体记录方法；ephemeral 不落主库 |
| CLI | 命令输入/输出与局部分页游标 | 不直接调用 Provider、Run 或数据库 |

PersistenceStore 隔离物理事务；Provider/Adapter 分别隔离 IO 与 wire；MainStrategy 只返回四项数据。直接依赖为标准库、新包内部模块与按需加载的现有 OpenAI SDK/HTTPX，没有导入旧源码。

## 重现与剩余需求

在重构工作树根目录运行：

```powershell
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_persistence.py tests_refactor/test_task_flow.py tests_refactor/test_cli.py
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-cli
```

配置与 CLI 说明在 `examples/refactor-cli/README.md`；真实 SDK 配置在 `examples/refactor-openai/README.md`。本阶段不向真实模型端点发请求。P3 的真实工具/schema/访问范围/人工交互、P4/P5 记忆与复盘、P6 浏览器、P7 独立发行和旧体系删除尚待实现。

阶段提交由本报告、代码、测试和示例配置共同形成；原有六份未提交设计文档不纳入本阶段提交。
