# P3 实施与验收记录

实施日期：2026-10-10。工作树：`C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`，分支：`codex/framework-refactor`。依据 `tdd-refactor-plan.md` 第 7 节与 `lean-design.md`。P3 已完成；原 `E:/FunAgent` 源码和暂存修改未处理，原有六份未提交设计文档未纳入阶段提交。

## 目标与最终行为

ToolMgr 的 Definitions 与 Execute 使用同一个固定注册表和可信 allowed_names；分派前使用 JSON Schema 2020-12 校验参数。ConfMgr 与 ToolMgr 启动共用 schema 校验，错误 schema 不发布半初始化状态；仅允许文档内引用，校验不获取远端 schema。jsonschema 从 MCP 可选依赖提升为核心依赖，未增加 SDK/网络调用栈。

ReadWorkspaceHandler 是实际可用的静态 handler。权限来自主任务的 RunContext.access 与可信会话 workspace，args 不能替换工作区或访问范围。SessionMgr.Create 验证目录并固定规范化绝对路径，后续 cwd 改变不会改变会话访问范围。读取仅接受相对路径，拒绝父目录穿越、绝对/驱动器/UNC/数据流路径及解析后越界的目录链接；只读取最多 65,536 字节的 UTF-8 普通文件。缺失、非普通、过大或非 UTF-8 文件生成无副作用的 error/invalid 结果。

注册配置的 `requires_approval = true` 将批准放在 ToolMgr 的参数校验与实际分派之间，绑定当前工具调用及不可变参数。肯定答复只允许本次 Execute 继续，拒绝/过期/取消不执行 handler；下一个调用重新请求批准。没有可由模型绕过的独立 approval token，也没有永久授权或授权数据库。

InteractionMgr 持有短期请求表、Lock、每个等待点的 Event 和期限。请求 DTO 关联 task_id/run_id；Resolve 校验身份、当前 pending、类型和有效期，直接唤醒原 worker，完全绕开主队列。成功回答、过期、取消与完成都清理请求，不累积完成请求历史。单次请求期限取 interaction_timeout 与父 Run deadline 的较早者；独立过期可返回普通工具错误，父取消/deadline 保持控制语义。review/recall 或没有主任务身份的上下文拒绝人工等待。

AskUserHandler 从公共 InteractionMgr API 获得澄清，结果作为同一个 Turn 的工具消息保存，不额外追加 user 或登记 Task。CLI 的 wait 边查询任务边显示和回答 pending；新增 pending/answer 命令支持手动答复。批准回答 yes/no；错误输入可重试；`/cancel` 或输入 EOF 取消当前任务。CLI Run 沿用同一输入函数，不建立输入线程/渠道/投递体系。

同一模型响应中的调用仍按确定顺序同步执行；未知、限权拒绝、schema 错误与普通失败都有配对工具结果，后续调用可继续。请求整批落库仍先于审批和 handler；模型 retry 只重发下一轮请求，不重放已处理的调用。ToolFailure 允许驱动明确报告 none/known/unknown effect；未分类异常保持 unknown/unknown，不将可能已发生的外部副作用标为未执行。本阶段只承诺本次响应一次 dispatch，不承诺外部跨崩溃 exactly-once。

所有 IO 在所属主 worker 直接执行。标准 Event/deadline 传入 handler；即使已取消或过期，真实调用返回前仍占执行位置，后续任务与 Provider/Store 关闭不会提前开始。工作区读取无共享连接，因此无需引入尚未使用的 ToolBackend/IO pool。Stop 先停止准入、请求取消并唤醒等待、join 主 worker，再清除 InteractionMgr 和关闭已有资源。

## 观察到的 Red / Green / Refactor

| 行为组 | 实际 Red | Green / Refactor |
| --- | --- | --- |
| schema 校验、冻结注册表 | **7 failed, 2 passed**：非法 args 实际进入 handler，错误 schema 未拒绝 | 同源 schema 编译/验证后，与此前测试 **157 passed** |
| 实际工作区读取、越界与已知失败效果 | **13 failed, 10 passed**：最薄 handler 尚未实现，已知失败被标 unknown | 静态只读 handler、固定访问范围、ToolFailure 效果确认后 **23 passed** |
| 人工等待、scope、过期/取消、单次批准 | **18 failed**：Wait 尚未实现、approval 未形成分派门禁 | 短期表/Event、直接 Resolve、每次调用审批后 tools + interaction **41 passed** |
| CLI/SQLite/生产装配组合 | **7 failed**：新 handler 尚未绑定生产入口 | 生产注册、可信 access、取消清理、CLI 答复泵后 **7 passed** |
| 相对工作区在 cwd 改变后的访问范围 | **1 failed, 58 passed**：读到另一个目录的文件 | 创建会话时固定绝对工作区；全套 **207 passed** |
| 最终资源清理与示例验收 | 既有合同的补充验收 | 故障装配、满队列答复、逆序/子进程验证后 **208 passed** |

组合测试中曾出现重复 TOML 字段的测试配置错误，已修正后重新观察业务 Red；该配置错误不计目标行为 Red。沙箱执行器初始化故障、文档读取编码问题也不计业务 Red。

绿色下复用 schema helper、沿用既有唯一 ModelRuntime.Run、让测试使用生产示例配置，避免复制配置。SessionMgr 仍无缓存；TaskMgr 只调度和取消；批准逻辑没有进入驱动；InteractionMgr 不调用模型、TaskProcess 或数据库。原 P0–P2 的原子事务、真实协议合同、重启 repair 与配对语义继续通过。

## 最终验证

- P3 指定测试：**60 passed**（test_tools 27、test_interaction 20、test_cli_interaction 13）。
- P0–P3 全套正序：**208 passed**；逆序：**208 passed**。
- 逆序进程内检查：剩余 `funagent-*` worker 为 **[]**；ConfMgr、DbMgr、TaskMgr、ModelRuntime、ToolMgr、InteractionMgr 的公共 GetInstance 均为 NOT_READY。
- 真实 CLI 子进程：新建会话 → read 请求 → wait 显示批准 → yes → 实际 UTF-8 内容 → ask 请求 → wait 回答 → 同一 Turn 完成 → history 配对 → quit → 重开 SQLite 查询两项 completed，全部成功。
- 离线和 OpenAI 两份 P3 配置 `--check-config` 冒烟通过；未向远端模型、知识库或其他真实服务发送请求。
- 正序混合工具批次验证 invalid/denied/error/ok 结果有序配对；受控模型 TRANSPORT retry 后 fault/read 各真实执行一次。
- Event 控制真实调用未返回时的取消/停机；受控 monotonic 验证父 deadline 与单次请求过期；没有用 sleep 赌副作用顺序或假退出。
- `git diff --check` 通过；新包无旧包导入、Lease/渠道、ProviderRunner 或 IO pool 引用。
- 新生产包 **21 个 Python 模块、1,833 非空行**；P2 为 17 模块/1,528 非空行。本阶段净增 4 模块/305 非空行；测试、配置和文档不计生产行数。

## 状态与资源所有权

| 所有者 | 状态/资源 | 生命周期 |
| --- | --- | --- |
| ConfMgr | 五份只读配置快照、已有生命周期锁 | 启动一次；最后清除 |
| ToolMgr | 固定定义/handler/schema validator 注册表、单次批准工具集合 | 分派无跨调用对话状态；主 worker 退出后清除 |
| InteractionMgr | 1 个请求表/Lock，每个 pending 的 Event/回答/错误/期限 | 回答/过期/取消立即撤销 pending；停止唤醒后清除 |
| TaskMgr | 1 个主 worker、既有队列/Condition/context | 容量包括 running；真实退出后释放位置；取消直接唤醒人工等待 |
| DbMgr → SQLiteStore | 1 个主 SQLite 连接/RLock | 原有具体事务与配对；主 worker 退出后关闭 |
| ModelRuntime / Provider | 既有固定绑定和所选驱动连接 | 唯一 Run，无共享对话；所有借用结束后关闭 |
| SessionMgr / handlers / CLI | 静态用例、读取/等待步骤和局部输入输出 | 无新增线程或连接；无长期批准状态 |

新增 Interaction DTO 只保存关联和显示数据；内部 Waiter 保存等待点状态，不进入持久 schema。schema helper 隔离同一套配置/运行校验。直接依赖是标准库、新包内部 API、现有可选 OpenAI SDK 与核心 jsonschema；未导入旧 src 包。

## 重现与剩余需求

在重构工作树根目录运行：

```powershell
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_tools.py tests_refactor/test_interaction.py tests_refactor/test_cli_interaction.py
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-cli
```

可运行操作说明在 `examples/refactor-cli/README.md`，真实模型工具配置在 `examples/refactor-openai/README.md`。实现范围止于 P3；P4/P5 统一记忆/复盘、P6 浏览器、P7 独立发行与旧体系删除尚待实施。永久授权、多主 worker 与跨崩溃工具重放保证不在本阶段范围内。
