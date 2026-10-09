# P0 / P1 实施记录

实施日期：2026-10-10。工作树：`framework-refactor/FunAgent`，分支：`codex/framework-refactor`。仅实现 P0/P1；原 `E:\FunAgent` 的源码和暂存改动未修改。

## P0

- 目标：独立 `src/funagent`、五类配置抽象与 TOML 实现、原子只读快照、独立测试及配置校验入口。
- Red 1：四个缓存/生命周期测试可收集，`ConfMgr` 薄声明因 `NotImplementedError` 导致 **4 failed**。
- Green 1：统一 loader + 冻结映射 + 冻结 ModelProfile + Initialize/GetInstance/Close，**4 passed**。
- Red 2：跨域引用/非法上限/结构校验新增测试，观察 **24 failed, 11 passed**（缺失校验或原始 KeyError）。
- Green / Refactor：共用 table/name/number/reference helper；公开异常仅含稳定 code、固定字段名与文件路径，不包含解析原文或密钥。**35 passed**。
- 默认 pytest 收集 **35 tests**，仅来自 `tests_refactor`，无旧 conftest 导入。
- 生产基线：**5 Python 模块，255 非空行**；一个 ConfMgr 快照引用及生命周期锁；无线程、数据库或 SDK 连接。直接依赖仅标准库与包内 contracts。
- 入口与五份配置见 `examples/refactor/README.md`。P0 不初始化尚未实现的资源。

## P1

- 目标：Provider/静态 Adapter、四项 ModelStrategy、唯一 ModelRuntime.Run 与最薄工具注册表。仅 ephemeral、完整回复、离线 fake；无会话数据库或记忆/复盘业务壳。
- Red 1：回复/工具批次/上限场景，三种策略与两种协议可收集，薄 Run 声明产生 **8 failed**。最小循环与同源注册表实现后，P0/P1 **43 passed**。
- Red 2：retry/fallback/fatal 策略场景产生 **8 failed, 8 passed**。提取有限发送 helper 后累计 **51 passed**；每次 attempt 都重新 Encode，成功 fallback 在后续 iteration 保持选中。
- Red 3：无效批次/截断/重复 ID、公开异常、控制传播新增场景，P1 套件 **18 failed, 33 passed**。完整批次校验先于任一 handler；修复 Decode 后控制检查与未执行调用结果后累计 **86 passed**。
- Review Red：将相同失败场景换成第二种 wire 协议，并补资源所有权/真实调用等待验证；发现失败 profile 统计、非标准错误 code 脱敏、未绑定 fallback 三个缺陷，累计 **3 failed, 104 passed**。
- Green / Refactor：最后实际尝试的 profile 进入结果、模型错误使用稳定 code 集、声明的 fallback 必须在启动绑定。freeze helper 统一在 contracts；单例不保存对话。累计 **107 passed**，逆序执行仍 **107 passed**；测试失败清理也使用生产 Close 入口。
- 配置入口 `python -m funagent --conf-dir examples/refactor --check-config` 冒烟成功；`git diff --check` 无问题。新生产包没有旧包/Lease/渠道引用。
- 生产基线：**9 Python 模块，653 非空行**（P0：5 模块/255 非空行）。直接依赖仅标准库及新包内部模块；无 SDK、网络、数据库、IO pool 或 worker。
- `tests_refactor/test_model_runtime.py` 与 `test_provider_contract.py` 覆盖两种 wire 编码、同轮有限 retry、fallback 重新编码、fatal 策略、完整工具配对、batch fail closed、调用 ID 去重、工具限权、总期限与子 timeout、并发/顺序隔离、usage，以及连接关闭。

### 状态与资源所有权

| 所有者 | 共享状态 / 资源 | 关闭规则 |
| --- | --- | --- |
| ConfMgr | 五份只读快照、生命周期 Lock | Close 清除缓存；初始化失败不发布 |
| ModelRuntime | 三份固定映射：profiles / provider 实例 / 静态 Adapter | 调用者先等待 Run/IO 实际结束；Close 对每个连接只调用一次，故障也尝试其余连接 |
| ToolMgr | 一份固定 definition/handler 注册表 | 无真实后端；Close 清除注册表 |
| 每个 Run | messages、seen/pending calls、结果、usage、iteration/attempt、所选 profile | 仅局部变量，不写共享对话 |
| Provider 实现 | 客户端和必要连接锁 | Send 单次 IO、线程安全、有界 timeout、禁 SDK 隐式 retry；P1 只有测试 scripted fake |

公共抽象隔离 IO（Provider）、wire 格式（Adapter）、四项策略数据（ModelStrategy）和工具实现（ToolHandler）。未增加资源容器、ProviderRunner、路由状态对象、ToolView 或复制的业务循环。

### 剩余需求

P2 及以后尚未实施：持久化主任务/任务 CLI、真实 Provider、真实工具/schema 权限/人工交互、记忆/复盘与浏览器。真实 SDK 的协议/连接/timeout 合同要由后续受控 transport 验证；P1 的 fake 不能替代该验收。wheel 安装与旧包删除属于 P7。

P0 阶段提交：`1a01e28`。P1 以本报告、实现和测试共同提交；原有六份架构文档保持原内容。

## 重现验证

本机使用已有的 `E:\FunAgent\.venv\Scripts\python.exe` 作为解释器，在本重构工作树根目录运行；测试通过 pytest 的 `pythonpath = ["src"]` 导入本工作树的新包，未导入原 E 源码。

```powershell
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_conf.py
& E:\FunAgent\.venv\Scripts\python.exe -m pytest tests_refactor/test_model_runtime.py tests_refactor/test_provider_contract.py
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor --check-config
```
