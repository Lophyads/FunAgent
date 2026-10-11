# 源码导航

`src` 是当前 Python 包名，发行包与命令名是 `funagent`。从命令入口开始阅读：`__main__.py` → `app/main.py` → `app/application.py`。

```text
src/
├── __main__.py              # python -m src
├── app/                     # 应用装配与可执行入口
│   ├── application.py       # 配置初始化、资源装配、启动与关闭
│   └── main.py              # 命令参数、运行模式与退出码
├── frame/                   # 跨业务域共享的配置、契约与基础服务
│   ├── conf_mgr.py          # ConfMgr：配置验证与只读快照
│   ├── contracts.py         # 任务、会话、模型请求等公共数据契约
│   ├── protocols.py         # 事件等跨域接口
│   ├── lifecycle.py         # 资源所有权、worker 排空与关闭顺序
│   ├── session_mgr.py       # SessionMgr：会话用例
│   ├── interaction_mgr.py   # InteractionMgr：人工澄清与单次批准
│   ├── event_mgr.py         # EventMgr / TaskEvents：任务事件与 SSE 状态
│   ├── review_scheduler.py  # ReviewScheduler：后台复盘调度
│   ├── log_session.py       # LogSession：日志 handler 生命周期
│   ├── web_projection.py    # WebProjection：有界展示状态
│   ├── tool_validator.py    # ToolValidator：工具参数 schema 校验
│   └── global_server.py     # 旧应用入口的兼容导出
├── api/                     # CLI、HTTP、诊断与管理接口
│   ├── diagnostics_mgr.py   # DiagnosticsMgr：诊断服务绑定
│   ├── diagnostic_service.py # DiagnosticService：运行诊断视图
│   ├── cli_controller.py    # CLIController：交互式命令处理
│   ├── http_server.py       # HttpServer / HTTPController：HTTP 与 SSE
│   ├── log_reader.py        # LogReader：有界日志读取
│   ├── memory_routes.py     # 记忆管理 HTTP 路由
│   ├── data_routes.py       # 只读数据 HTTP 路由
│   ├── memory_export.py     # 记忆 JSONL 导出
│   ├── request_validation.py # API 请求参数验证
│   └── web/                 # 浏览器静态资源
├── task/                    # 主任务准入、执行与模型策略
│   ├── task_mgr.py          # TaskMgr：任务服务绑定
│   ├── task_queue.py        # TaskQueue：队列、取消与单 worker
│   ├── task_process.py      # TaskProcess：一轮任务的准备、执行与提交
│   └── main_strategy.py     # MainStrategy：主模型与工具选择策略
├── model/                   # 共用 ReAct 内核与模型驱动
│   ├── model_runtime.py     # ModelRuntime：已装配 Runner 的访问入口
│   ├── runtime.py           # Runtime：实际 ReAct 执行实现
│   ├── chat_accumulator.py  # ChatAccumulator：流式响应累积
│   ├── factory.py           # 按配置延迟加载所选驱动
│   ├── demo.py              # 离线模型
│   └── openai_chat.py       # OpenAI 兼容驱动
├── memory/                  # 授权、召回、复盘与记忆管理
│   ├── memory_mgr.py        # MemoryMgr：记忆访问绑定
│   ├── memory_operations_mgr.py # MemoryOperationsMgr：管理用例绑定
│   ├── memory_manager.py    # MemoryManager：来源授权与存储分发
│   ├── memory_operations.py # MemoryOperations：CLI/Web 管理与确认
│   ├── memory_prepare.py    # MemoryPrepare：主任务召回编排
│   ├── memory_planner.py    # MemoryPlanner：无工具召回规划
│   ├── memory_retrieval_strategy.py # MemoryRetrievalStrategy：规划模型策略
│   ├── review_process.py    # ReviewProcess：后台复盘用例与模型策略
│   └── factory.py           # 按配置构造存储、查询和 embedding 驱动
├── persistence/             # 会话、任务、日志与复盘的原子存储
│   ├── db_mgr.py            # DbMgr：主存储绑定
│   ├── protocols.py         # 按使用方划分的 Store 接口
│   ├── factory.py           # 主存储选择
│   └── sqlite.py            # SQLite 实现
└── tool/                    # 工具声明、选择、授权与执行
    ├── tool_mgr.py          # ToolMgr：工具分发绑定
    ├── tool_registrar.py    # ToolRegistrar / ToolEntry：声明与注册
    ├── tool_registry.py     # ToolRegistry：工具选择与执行实现
    ├── read_workspace_handler.py # ReadWorkspaceHandler：工作区只读工具
    ├── factory.py           # 工具与后端装配
    ├── builtins/            # 内置工具声明与 handler
    └── backends/            # MCP、terminal、web、computer、treg 等后端
```

上图列出主要阅读入口，未列出每个辅助文件。

## 文件职责约定

以组件为主体的文件名与主要类名对应，使用 snake_case：`ConfMgr → conf_mgr.py`、`TaskQueue → task_queue.py`、`MemoryPrepare → memory_prepare.py`。共享协议、数据契约和纯函数集合按职责命名；驱动集合按协议或后端名称命名。两个独立的服务管理器各自放在对应文件中。

| 文件 | 放置内容 |
| --- | --- |
| `__init__.py` | 包说明、公开对象导出和 `__all__`；不定义管理器、业务逻辑或创建资源 |
| `task_mgr.py` 等业务绑定入口、`model_runtime.py` | `Initialize` / `GetInstance` / `Close` 服务绑定；依赖协议，不加载具体驱动 |
| `contracts.py` | 数据结构、值对象与业务错误 |
| `protocols.py` | 使用方依赖的接口与协议 |
| `factory.py` | 根据配置选择、构造实现；可选依赖在选择时加载 |
| 组件文件 | 同名组件的实现，如 `task_queue.py`、`memory_prepare.py`、`memory_operations.py` |
| 驱动文件 | 按协议或后端组织的实现，如 `sqlite.py`、`openai_chat.py`、`backends/mcp.py` |

`MemoryMgr` 提供已绑定记忆服务的访问入口，`MemoryManager` 负责来源授权和存储分发；分别位于 `memory_mgr.py`、`memory_manager.py`。`ModelRuntime` 的绑定入口位于 `model_runtime.py`，实际 `Runtime` 执行实现位于 `runtime.py`。

内部代码从具体模块导入，便于直接定位实现：

```python
from src.task.task_mgr import TaskMgr
from src.task.task_queue import TaskQueue
from src.memory.memory_prepare import MemoryPrepare
from src.model.protocols import Runner
from src.memory.contracts import MemoryQuery
from src.app.application import Start, Stop
```

原有 `from src.task import TaskMgr` 等包级导入继续可用，导出的是同一个类，服务状态不会分叉。具体模块导入使用上面的组件文件名。`src.frame.global_server.Initialize/Start/Stop/main` 继续转到新入口。命令行入口为 `src.app.main:main`；`api/cli_controller.py` 负责应用启动后的交互命令，两者职责不同。

新增功能时先选择业务域，再按接口、数据契约、绑定或具体用例放入对应文件。跨域基础设施留在 `frame`，依赖所有业务域的装配留在 `app`。

## 项目根目录

| 路径 | 用途 |
| --- | --- |
| `src/` | FunAgent 主应用源码及 Web 资源 |
| `tests/` | 自动化行为与回归测试 |
| `scripts/` | 发布、浏览器和迁移验收脚本 |
| `architecture/` | 架构设计与历史决策记录 |
| `docs/` | 用户接口说明与迁移证据 |
| `sql/` | 可选记忆存储的部署 SQL |
| `mcp_server/MODULAR-RAG/` | 独立 RAG 服务及其源码、脚本和配置 |
| 根目录 `*.conf` | 当前应用部署配置 |
| `.env`、数据库、`logs/`、`tmp/` | 本机凭据、运行数据与临时产物 |

主应用和 RAG 各有自己的 `src` 包，应在各自环境中运行。
