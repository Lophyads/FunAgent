# P7：新体系发行与旧架构删除

日期：2026-10-10。分支：`codex/framework-refactor`。P6 输入基线：`8d8538d`。P7 开发和交付验收完成。

本阶段只改重构工作树 `C:/Users/ql136/.codex/worktrees/framework-refactor/FunAgent`。原 `E:/FunAgent` 的未提交源码未改；仅复用其 Python 3.12.13 解释器运行测试。原有六份未跟踪架构文档保持原内容、未纳入阶段提交。没有删除用户旧数据文件或迁移旧 schema。

## 目标与实际 Red

P7 新增 `test_release.py`。首轮可收集的行为测试实际输出 **5 failed, 8 passed**：

1. 发行配置仍声明旧 `agentcore` 命令。
2. 旧 src 包、配置/schema 和 tests 仍在。
3. 启动绑定失败时，未绑定 Store 的 Close 抛异常会中断后续清理，并泄露内部异常。
4. Stop 等主 worker 后才取消复盘；被占用的主 IO 使复盘取消迟迟不发生。
5. 主 worker 排空失败但还活着时，入口继续关闭它正在借用的 Provider/数据库/记忆。

这五项是业务 Red；Windows 执行工具的 sandbox helper 初始化失败属于环境故障，不计为 Red。通过工具权限审核在指定工作树执行后继续完成验收。

## Green 与 Refactor

- 两个 worker 加入 `RequestStop`：停止准入、发出 cancel；主任务立即唤醒关联人工等待，复盘取消未开始和正在运行的 job。入口先对两个队列发信号，再关闭 HTTP、等待 worker、关闭连接。
- worker 关闭故障后，通过公共就绪状态确认是否真的退出；仍有 worker 借用资源时保留所有权并报错，允许重试 Stop。已退出 worker 的持久化收尾故障仍尝试其他资源清理。
- 启动未绑定 Store 的关闭异常不再阻断其他已拥有资源的回滚；公开错误保持稳定、脱敏。HTTP 关闭失败也保留应用资源供重试。
- 删除旧六个包 `agentcore/gateway/log/tools/memory/knowledge`、旧 tests、旧 CLI/export、旧配置和旧 SQL 文件及两个旧示例。无兼容桥、Noop 或旧 schema 导入。
- 发行项目改为 `funagent 0.2.0`，wheel 仅含新包；核心直接依赖只有 jsonschema。OpenAI SDK 改为选用的 extra，开发 extra 为 pytest；移除旧可选驱动和 async/multiprocess pytest 配置，更新 uv.lock。
- sdist 显式包含新源码、测试、配置示例、验收脚本和本报告，排除独立 RAG 项目与旧体系。更新 README 与真实模型示例，验收脚本支持 `--installed`，该模式不插入 checkout/src。
- 全套测试 autouse 清理后检查八个共享入口均未就绪、无 `funagent-` 线程。追加复盘 worker / HTTP 排空失败边界，P7 单独 **15 passed**。

## 验证结果

| 验证 | 实际结果 |
| --- | --- |
| P0–P7 正序套件 | **357 passed**，无警告 |
| 同一套件按收集结果完全逆序 | **357 passed**，无 singleton/worker 残留 |
| 新 wheel + sdist 构建 | 成功，wheel 从 sdist 构建 |
| 临时独立环境安装 wheel | 仅安装核心依赖；无旧包和 OpenAI SDK，console/module 导入正确 |
| 解压 sdist 后独立 editable 安装 | 成功；从与源码无关目录运行，实际导入解压源码 |
| wheel / editable 的 cli/web/both | 正常回复、工具配对、真实工作区读取、澄清/批准、历史、记忆/复盘、资源关闭通过 |
| 安装入口与公开启动错误 | console 可校验/运行；缺配置明确失败；所选可选驱动缺失失败，未选驱动不阻碍启动 |
| 启动故障注入 | 配置、第二个 Provider、工具注册、主 Store、记忆绑定、主/复盘 worker、HTTP bind/thread；资源回滚并可重启 |
| 停机故障注入 | 主/复盘 IO 未返回时仍拥有资源；主/复盘 worker 与 HTTP 关闭失败保留连接供重试 |
| 真实 Chrome 操作（安装的 wheel） | **10 组操作通过**；Chrome 154.0.8037.98；JS errors = []；桌面/移动截图 |
| 源码与发行扫描 | AST 无旧包导入；无 Lease/渠道/投递机制或发行引用；git diff --check 通过 |

真实浏览器检查覆盖会话创建、完整回复、工具、刷新恢复同一任务、切换会话、答复澄清、批准/拒绝实际读取、取消人工等待、失败显示、记忆召回/复盘隔离和移动布局。结果与截图位于：

`C:/Users/ql136/.codex/visualizations/2026/10/10/01a1244a-a5f2-7f02-b059-0ce7e2d9a09f/p7/`：`browser-results.json`、`browser.png`、`browser-mobile.png`。

release smoke 使用临时全新 venv，清除 PYTHONPATH/PYTHONHOME/VIRTUAL_ENV，并以 `-I` 运行安装包行为检查；断言实际导入路径在 wheel 的 site-packages 或解压 sdist 的 src。完成后关闭应用/浏览器并删除临时环境与数据库。构建产物在本工作树 `dist/`。外部模型协议已通过 SDK + 受控 HTTP transport 合同测试；本次未连接真实供应商或云端知识库。

## 代码量实测

口径为 src 下生产 Python `.py` 模块及非空行，含 docstring/DTO/SQL；不含测试、脚本、HTML/CSS/JS 或独立 RAG 项目。

| 基线 | 全部生产模块 / 非空行 | 其中新 funagent |
| --- | --- | --- |
| 重构前 `d0da2a9` | 77 / 13406 | 0 / 0 |
| P0 `1a01e28` | 82 / 13661 | 5 / 255 |
| P6 `8d8538d` | 110 / 16582 | 33 / 3176 |
| P7 | **33 / 3207** | **33 / 3207** |

相对重构前生产 Python，非空行减少 **76.1%**；相对包含新旧两套代码的 P6，减少 **80.7%**。P7 为生命周期异常路径新增 31 非空行；减少主要来自移除旧重复体系，没有通过删除事务/配对/控制路径压缩新内核。

## 状态与资源所有权

| 所有者 | 资源 / 状态 | 清理顺序 |
| --- | --- | --- |
| global_server | 唯一 HTTP 监听与启动装配 | 停止任务/复盘准入和取消后，等 HTTP handler 结束；失败保留资源 |
| ConfMgr | 五份冻结配置 | 其他资源关闭后清缓存；失败加载不发布 |
| TaskMgr | 有限队列、原始任务 cancel Event、一个主 worker | 发 cancel、唤醒人工等待、处理排队任务、等实际 Run/IO 返回 |
| ReviewScheduler | 有限合并队列、独立 cancel Event、一个可选复盘 worker | 与主 worker 同时请求停止，固定运行上界，等实际 Run/IO 返回 |
| InteractionMgr | pending 表/Event/过期时间 | Task 取消直接唤醒原等待；worker 退出后关闭 |
| ModelRuntime | 固定 profile/provider/adapter 映射，按 provider ID 的连接 | 两个 worker 和 HTTP 退出后，每个连接关闭一次 |
| ToolMgr | 固定 definition/handler/schema 与单次批准集合 | worker 退出后清注册表；实际工作区工具无额外共享连接 |
| DbMgr → SQLiteStore | app.sqlite3、会话/任务/消息/水位事务 | 使用者退出后关闭；不向业务传 SQL/连接 |
| MemoryMgr → Store | memory.sqlite3、来源访问策略、只读 query callback | 使用者退出后逐个关闭；Store 自有连接/锁 |
| 每次 Run | messages、调用去重、结果、attempt/iteration | 局部状态；主业务、召回、复盘使用同一循环 |

## 重现命令

```powershell
uv sync --locked --extra dev --extra openai
uv run --extra dev --extra openai pytest tests_refactor
uv build --out-dir dist
python scripts/release_smoke.py --wheel dist/funagent-0.2.0-py3-none-any.whl --sdist dist/funagent-0.2.0.tar.gz
```

逆序可以在项目根目录运行以下 Python（不额外安装排序插件）：

```python
import pytest
class ReverseOrder:
    def pytest_collection_modifyitems(self, items):
        items.reverse()
raise SystemExit(pytest.main(["tests_refactor", "-q"], plugins=[ReverseOrder()]))
```

浏览器参数加到 release_smoke：`--node <Node 路径> --playwright <Playwright 目录> --browser <Chrome/Chromium 路径> --artifact-dir <结果目录>`。本机已有这些运行时，未安装新浏览器。可加 `--offline` 使用 uv 缓存。

## 交付边界

P7 门禁完成。应用仅支持单机单进程、CLI 与浏览器；无渠道投递、跨进程 Lease、旧接口/配置/schema 兼容、token streaming 或旧数据清理。实际模型端点/凭据配置由使用者选择；首版无需新扩展点或额外后台执行池。
