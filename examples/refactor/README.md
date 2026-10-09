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
