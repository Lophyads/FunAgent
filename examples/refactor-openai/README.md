# P2 真实模型配置

将 `model.conf` 的 `your-model-name` 替换为所用端点支持的模型名称；如使用兼容 Chat Completions 的端点，修改 `base_url`。`credential_env` 声明已有的凭据环境变量名，配置文件不保存密钥。

```powershell
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-openai --check-config
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-openai
```

命令用法与 `../refactor-cli/README.md` 相同。只有启动所选真实驱动时才导入 SDK、解析凭据；缺凭据会清理已建资源并报 `CONFIG_CREDENTIAL`。离线 demo 和纯配置校验不要求真实凭据。

Provider 仅执行一次同步请求，SDK `max_retries=0`；有界 retry/fallback 归唯一 ModelRuntime 内核。请求 timeout 使用当前 attempt 的剩余预算与驱动 30 秒上限。工具请求与结果按 Chat Completions 格式编码，token usage 只保留标准计数字段；供应商错误统一转成稳定公开 code。

实现依据：[官方 Python SDK 文档](https://developers.openai.com/api/reference/python)与 [Chat Completions 协议](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)。本阶段使用已安装的 SDK 2.54.0，通过受控 HTTP transport 验证编码、认证、超时、错误、单次 attempt 和 SQLite 主任务闭环；没有向真实端点发送请求。
