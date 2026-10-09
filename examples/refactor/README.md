# 新配置示例

五份 `.conf` 使用 TOML，独立于旧 `agentcore.toml`。不读取、合并或迁移旧配置。

```powershell
$env:PYTHONPATH = "src"
python -m funagent --conf-dir examples/refactor --check-config
python -m pytest tests_refactor
```

新 console entry point 为 `funagent = funagent.global_server:main`。当前入口仅校验配置；任务 CLI 在 P2 接入。旧入口与依赖的最终删除属于 P7。

`model.conf` 声明 provider ID、adapter ID、profile、正 timeout、非负 max_retries 和已存在的 fallback_profiles。adapter ID 在启动绑定到静态 Adapter；provider 的 kind 与 credential_env 只缓存为数据，本阶段不会创建客户端或解析凭据。

`tool.conf` 声明工具 schema、handler 名称、可选 backend ID 和用途允许集。`memory.conf` 声明 semantic/episodic 来源与策略引用；本阶段不会打开 Store。`log.conf` 和 `serv.conf` 的 `[options]` 分别声明日志选项和接入/存储/容量选项。

示例中的 scripted provider 和 echo handler 由 P1 测试在启动时绑定。此目录不需要网络或密钥，配置校验也不初始化模型、数据库、worker 或 HTTP server。GetInstance 不执行 IO；Stop 后可以通过 Initialize 装配另一目录。

## P1 启动绑定

`ModelRuntime.Initialize(profiles, providers, adapters)` 接受冻结 ModelProfile 数据、每个 provider ID 的连接实例和每个 adapter ID 的静态实现类。`ToolMgr.Initialize(definitions, handlers)` 接受同名 ToolDefinition/handler 映射。注册表在启动后固定；只有启动装配传实现对象。

三种用途的策略都继承 `ModelStrategy`，只实现 `SelectModel`、`MaxIterations`、`ToolSet` 和 `FallbackPolicy`。策略不保存 messages，也不重写 Run。`FallbackRule.profile_ids` 只能选择主 profile 在配置中声明且已绑定的 fallback；retry/fallback 的 code 集分别决定行为。一次成功有效模型响应计一个 iteration，传输 attempt 的 retry 不增加 iteration。达到上限不请求额外总结。

业务调用使用 `ModelRuntime.GetInstance().Run(RunInput(...), Strategy, RunContext(...))`。RunContext 只包含业务数据和 Event/deadline；本阶段 record_scope 仅支持 ephemeral。Provider 收到的 attempt context 使用 `min(主 deadline, 当前时间 + profile timeout)`，与父任务共享 cancel Event。父取消/总 deadline 返回 cancelled；仍有预算的子 timeout 只按策略重试/降级。

RunResult 的 completed/failed/limit/cancelled 及 final/reason/error_code/tool_results/usage/iterations/attempts 数据不代表持久化提交。无效调用批次在工具执行前整体拒绝；未知或未授权工具返回 invalid/denied。handler 返回的 call_id 由注册表对齐。取消时已完成工具保留真实结果，未执行工具有明确 cancelled 结果；执行中异常只报告未知效果。

调用者必须等 Run 和实际 IO 返回后再关闭资源，顺序为 ModelRuntime.Close、ToolMgr.Close、配置 Stop。成功初始化后 ModelRuntime 拥有传入 provider；同一连接被多个 ID 使用也只关闭一次。配置或绑定失败不发布半就绪单例；未接管的资源由启动调用者关闭。

离线脚本 Provider、两种最小 Adapter 和三种策略见 `tests_refactor/fakes.py`；相同循环与失败场景由两种协议替换验证。本阶段未实现真实 SDK，Provider 协议要求真实实现关闭 SDK 的自动重试。
