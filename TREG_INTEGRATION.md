# TinyFish 免费联网工具接入

当前项目只接入两个端点，保留已有模型工具接口：

| FunAgent 工具 | treg 端点 | 请求映射 |
| --- | --- | --- |
| `web_search(query, limit=5)` | `tinyfish.web.search` | GET；`query`、`domain_type=web`、`page=0` |
| `web_extract(urls, char_limit=15000)` | `tinyfish.web.fetch` | POST；`urls`、`format=markdown`、`ttl=0`、剩余超时 |

## 配置与凭据

安装已有 MCP 可选依赖即可；根目录 `start.sh` 已包含该依赖：

```powershell
uv sync --locked --extra mcp
```

在项目根目录 `.env` 中设置 `TREG_TOKEN=<你的 treg 团队令牌>`，或在启动进程的环境中设置同名变量，然后重启 FunAgent。令牌来自 [treg](https://treg.to/)，配置文件只保存环境变量名，不保存令牌值。免费端点仍需要 treg 身份验证。

当前根目录 `tool.conf` 已启用主模型的 `web` 工具集与 `network.read` 权限，并绑定到以下后端：

```toml
[tools.web_search]
backend_id = "network"
[tools.web_extract]
backend_id = "network"
[backends.network]
kind = "treg_web"
endpoint = "https://treg.to/mcp/"
credential_env = "TREG_TOKEN"
timeout = 30.0
max_bytes = 1048576
allow_private = false
```

其他配置目录还需将 `web` 加入 `[purposes.main].toolsets`，将 `network.read` 加入 `[session_access].default` 或对应会话权限。`recall`、`review` 不开放联网工具。

没有令牌时，这两个工具不可用，其他工具仍可初始化。已配置令牌时，所选后端在启动时建立一个共享 Streamable HTTP MCP 会话；鉴权或连接失败会报告现有 MCP 错误。修改令牌需要重启。连接页展示后端 `network` 的可用状态。

## 调用边界与免费策略

可调用端点固定在代码中，模型不能传入 `endpoint_id`、HTTP 方法、请求头或额外参数。`catalog_get`、`call` 仅供适配器内部使用，不注册为模型工具，不启用动态发现。没有付费路由、自动重试或替代供应商。

每次执行先通过 `catalog_get` 检查准确端点、HTTP 方法、`cost.type=free`、美元报价为 0，并拒绝任何已声明的溢出路线或报价。报价改变、缺失或出现溢出时返回 `TREG_COST_POLICY`，不执行 `call`。回执声明非零费用或溢出路线时也报告该错误；回执未提供费用时不会编造零费用。

这是调用前的报价检查，不能作为 treg 服务端的原子计费上限。目录查询与执行之间仍存在时间间隔，执行后的检查不能撤销已经发生的计费。当前两个端点的官方目录均标记免费且无溢出报价；如果需要账户级禁止溢出，应由账户管理员在 treg 中配置。

## 结果、网络和生命周期

- Search 取第一页并按 `limit` 截断，保留 `results`、`truncated`、`artifact`。`limit` 是返回数量上限；不自动翻页凑足结果。存在更多结果时标记截断。
- Fetch 最多接受 5 个 URL，重复 URL 去重，按请求顺序返回 `pages`。每页正文按 `char_limit` 截断；缺页、空正文、部分失败不会伪装成完整成功。
- `artifact.kind=provider_response`；哈希与字节数来自规范化后的供应商 JSON，而不是 FunAgent 直接下载的原始 HTML/PDF。页面最终 URL 保存在 `pages[].url`。
- 本地检查 URL 协议、凭据、端口、localhost、本地域名、非公开 IP 字面量及可选主机白名单。目标域名的 DNS、抓取和重定向由 TinyFish 执行；不会用本机代理 Fake-IP 解析结果判断远端目的地，也不会在本地下载目标页面。域名解析与重定向的目的地址保护依赖远端服务。
- 共享会话沿用现有 MCP 的鉴权、取消通知、期限和关闭机制；单次工具预算 30 秒，MCP 消息与最终输出最多 1 MiB。关闭先等待借用返回，再关闭会话；未能及时排空时可重试关闭。

## 验收

```powershell
uv run --no-sync pytest tests
uv run --no-sync funagent --conf-dir . --check-config
```

离线测试验证固定端点、免费报价变化、费用回执、URL 限制、结果转换、权限选择、超时、取消和关闭。协议测试使用本地 MCP 服务和测试令牌，不调用 treg 账户。配置好真实 `TREG_TOKEN` 后，分别请求“搜索 Python 官方文档”和“提取 https://example.com 的正文”，确认连接可用、结果有真实 URL，并在 treg 核对实际调用与计费。

2026-10-12 本地验收结果：53 项测试通过，实际 CLI 的 `--check-config` 返回 `Configuration valid.`。当前进程环境及根目录 `.env` 均未配置 `TREG_TOKEN`，因此尚未进行真实 treg 鉴权与联网调用。

官方依据：[treg 仓库](https://github.com/superdesigndev/treg)、[MCP 实现](https://github.com/superdesigndev/treg/blob/main/src/treg/mcp.py)、[Search 目录](https://treg.to/catalog/endpoints/tinyfish.web.search)、[Fetch 目录](https://treg.to/catalog/endpoints/tinyfish.web.fetch)。
