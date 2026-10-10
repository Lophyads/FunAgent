**Web 对话与记忆链路测试用例**

自动化用例位于 `tests/test_web_memory_flow.py`，通过前端使用的真实 HTTP 接口发起对话，执行真实 Agent、后台复盘、自动事实召回及临时 SQLite 数据库；主模型、规划器和复盘模型使用离线回复。`tests/test_memory.py` 补充预算、中断、重试和恢复覆盖。

**运行自动化测试**

```powershell
Set-Location 'E:\FunAgent'
uv run --extra dev pytest tests/test_web_memory_flow.py
```

相关回归测试：

```powershell
uv run --extra dev pytest tests/test_web_memory_flow.py tests/test_memory.py tests/test_knowledge_integration.py tests/test_supabase_memory.py
```

同时需要保持 Supabase 或 MCP 依赖时，在 `uv run` 中追加相应 `--extra supabase` 或 `--extra mcp`。

**自动化用例与验收点**

| 编号 | 场景 | 核心验收点 |
| --- | --- | --- |
| A01 | N=1/2/6/10，Web 连续完成 N 轮对话 | 前 N−1 轮不复盘；第 N 轮排入真实后台队列；复盘收到用户问题与模型回复，调用 memory.add 并持久化 |
| A02 | 后台模型暂时阻塞 | 第 N 轮 Web 回复已完成，后台尚未写记忆；释放后台后出现记忆，来源指向正确前台轮次与消息区间 |
| A03 | 连续完成两批对话 | 第二次复盘的待提炼历史仅包含新增区间；同一个稳定事实再次 add 返回 duplicate，总记录数不增加 |
| A04 | 使用相同 client_request_id 重试 HTTP 提交 | 返回原 turn_id，模型不重复执行，复盘计数不增加 |
| A05 | review_every_turns=0 | Web 对话正常，后台模型不调用，自动记忆不更新 |
| A06 | 前台模型请求失败 | Web 返回 failed，该轮不计入成功轮次，不触发复盘 |
| A07 | 后台模型第一次失败，下一轮成功 | 前台回复正常，失败不推进复盘游标；下一次复盘重新处理未完成区间 |
| B01 | 通过 Web 添加 user/note，再开始新会话 | 只有预算内的 user 进入冻结 system；note/episode 不进入 bootstrap |
| B02 | 已有会话中修改数据库记忆 | 旧会话 system prompt 保持冻结；新会话注入修改后的正文和版本 |
| B03 | Web 自动召回相关 episode | 事实只追加到当前 user 的 API 请求副本；历史仍只有原始问题与回复 |
| B04 | 自动事实搜索无匹配记录 | 当前请求收到 empty 状态，可以正常回复；不修改记忆 |
| C01 | 复盘提炼事实后开启新会话 | 新会话实际模型请求包含刚写入的事实，贯通写入与读取两条链路 |

A01/A02 共用参数化用例。它们验证调用链和数据传递，不评估真实模型的提炼质量或浏览器页面布局，也不连接真实 Supabase 项目。

当前行为：新会话只加载 user 并冻结 system；每轮共用一次规划，按需召回 note/episode 和已启用的知识库。普通模型不能调用 memory，即使显式启用该工具。召回仅进入请求副本，本轮循环/重试复用，后续轮次和恢复重新检索；不保存快照。SQLite 使用关键词，Supabase 使用 embedding。

**真实模型人工验收：先准备独立数据范围**

复制现有可正常对话的配置为 `agentcore.memory-test.toml`，保留主模型与工具配置，在副本中修改已有字段，避免添加重复 TOML 段：

```toml
[agent]
database_path = ".funagent/memory-web-test/agentcore.db"
memory_max_chars = 4000
memory_recall_top_k = 5
memory_recall_max_chars = 4000

[memory]
enabled = true
review_every_turns = 3

[memory.sqlite]
path = ".funagent/memory-web-test/memory.db"
```

SQLite 验收选 `backend = "sqlite"`；Supabase 验收选 `backend = "supabase"`，保留已配置并初始化的 URL、凭据引用和 embedding，将 `[memory.supabase].namespace` 改成一个新的独立值，例如 `memory-web-test-20261006-01`。每次重新验收换一个值，避免已有事实影响去重判断。Supabase 的本地 SQLite 路径仍用于复盘进度。配置副本旁的 `.env` 继续提供已有凭据。

启动配置副本：

```powershell
uv run --extra supabase agentcore web --config agentcore.memory-test.toml
```

纯 SQLite 可以省略 `--extra supabase`；使用 MCP 时保留 `--extra mcp`。

下面共 30 个人工场景，覆盖两条主链路及其边界。每个独立场景使用新的测试目录或 Supabase namespace；依赖用例 1 的场景沿用该数据。修改配置后重启，并核对配置副本。

建议每次记录：后端、namespace/数据库路径、session_id、turn_id、实际成功轮数、复盘日志、记录 ID/版本/来源、检索工具结果和结论。session_id 与 turn_id 可从浏览器 Network 中的 `/api/turns` 请求和响应取得。普通纯文本一轮通常有两条消息；工具调用会增加消息数，不能把消息数直接当成对话轮数。

复盘按成功完成的普通用户轮次触发，需要等到 review.completed 后检查记忆。前台 memory 工具已隐藏，“记住”由后台提炼；显式修改记录使用 Web Memory 页面。

| 用例 | 测试重点 | 后端 |
| --- | --- | --- |
| 1–3 | 对话复盘、资料注入、事件召回 | 两者 |
| 4–8 | 空库、去重、无价值聊天、假设内容、模型猜测 | 两者 |
| 9–13 | 冲突、显式修正、版本竞争、删除、同会话读取最新值 | 两者 |
| 14–18 | 分会话计数、重启、取消、关闭复盘、关闭记忆 | 两者 |
| 19–22 | 事件日期、检索过滤、语义改写、上下文预算 | 21 对比两者，其余两者 |
| 23–25 | namespace 隔离、后端不可用、embedding 不可用 | Supabase |

**人工用例 1：三轮对话后复盘写入**

新建一个会话，依次发送下列测试数据，每轮都等模型回复完成：

1. `我的昵称是青竹，平时希望你用简洁中文回答。这一轮只回复“收到”，不要调用记忆工具。`
2. `我正在开发 Aurora 项目，开发环境使用 SQLite。这一轮只回复“收到”，不要调用记忆工具。`
3. `2026-10-06，我们决定先保留 Redis 缓存，等压测完成后再评估迁移。这一轮只回复“收到”，不要调用记忆工具。`

验收时同时检查 Web 的对话与记忆页面、诊断日志：

- 第 1、2 轮没有 `review.queued`；第 3 轮完成后出现 `review.queued`、`review.started`，最终出现 `review.completed`。
- 等待后台完成后，记忆中能找到昵称/回答偏好、项目事实和带日期的决策。允许合并事实、调整措辞，不要求记录数或正文完全相同。
- 记录 `source.type` 为 `review`，`session_id` 为当前会话，`turn_id` 为第 3 轮前台轮次；普通纯文本三轮对应 `range_start=0`、`range_end=6`。
- 主会话历史只有原始三次提问与回复，没有复盘模型的消息或工具结果。

前台不应成功执行 memory 调用；模型猜测调用应返回 unknown_tool。后台工具来源和 review 日志才是复盘写入证据。

继续发送三轮相同偏好或一般聊天，第二批复盘完成后检查：已有稳定事实没有重复记录，复盘处理新增区间。真实模型不一定每批都新增记忆，返回“没有值得记住的新事实”也是有效结果。

**人工用例 2：新会话读取复盘生成的资料**

用例 1 的后台复盘完成后，新建会话，发送：

`我的昵称是什么？我希望你用什么语言和风格回答？Aurora 的开发环境是什么？`

实际请求的 system 只包含 user 的昵称/偏好及 ID、版本；项目事实需本轮规划召回，位于当前 user API 文本末尾的 retrieved_memory 块。system 不含 note/episode。自动化直接断言这些请求字段。

**人工用例 3：召回相关历史事件并交给模型**

新建会话，发送：

`我们此前决定如何处理 Aurora 的 Redis 缓存？这个决定是哪一天做出的？`

验收点：规划选择 need_memory=true、空格分隔关键词查询；出现 memory.query_completed。当前 user 请求副本含决定、日期、来源，历史无参考块和 memory tool 消息。回答提及正确决定和日期；不存在的主题收到 empty 状态。

人工测试不要把“模型答对了”作为唯一证据；还需确认数据库记录来源、检索工具结果及实际上下文。Supabase 验收另外核对项目中 `funagent_memory_entries` 的测试 namespace，确认云端记录与 Web 显示一致。

**人工用例 4：空记忆库与未命中**

前置：新的独立数据范围，Memory 页面三种类型均无记录。

发送：`Aurora 以前决定如何迁移缓存？没有记录就说不知道，不要推测。`

预期：触发检索时参考状态为 empty，模型说明没有记录；初始 system 无画像。查询不新增记录。以搜索事件和 Memory 总数为证据，区分 empty、failed 与 skipped。

**人工用例 5：重复表达同一偏好**

前置：沿用用例 1，记录已有昵称/回答偏好的 ID、正文、主题和版本。

连续发送三轮同样内容：`我的昵称仍是青竹，回答偏好仍是简洁中文，没有变化。只确认收到，不调用记忆工具。`

预期：本批复盘完成后，同一稳定事实没有重复新增；原记录 ID 和版本不变。若复盘实际提交相同主题和规范化正文，add 应返回 `duplicate`、`changed=false`。

数据库保证的是主题与正文规范化后的精确去重；模型换一种措辞写出语义重复条目时，应记录为提炼质量问题，不误称数据库已实现语义去重。

**人工用例 6：一般闲聊无需新增记忆**

前置：空测试库，N=3。

依次发送：

1. `计算 12 × 7。`
2. `谢谢。`
3. `好的，这个话题结束了。`

预期：第 3 轮仍触发复盘，但没有值得保留的用户资料、项目事实或重要事件，记录总数保持 0，复盘进度正常推进。不要求每次复盘一定调用 memory.add。

**人工用例 7：假设、引用和他人信息不变成用户资料**

前置：空测试库，N=3。

依次发送：

1. `假设一个人住在上海且喜欢咖啡，请解释如何给他推荐饮品。这是假设，不是我的个人资料。`
2. `我引用一段小说：“阿青是一位医生，最喜欢红茶。”不要把小说角色当成我。`
3. `以上都是举例，没有提供我的住址、职业或饮品偏好。只确认即可。`

预期：复盘不产生“用户住上海”“用户是医生”“用户喜欢咖啡/红茶”这样的 user 记录。这里主要评估真实模型的提炼质量；检查实际记忆正文，不能只检查复盘任务成功。

**人工用例 8：模型建议不变成已确认事实**

前置：记忆中已有“Aurora 的开发环境使用 SQLite”。

依次发送：

1. `给 Aurora 提出一种可以考虑的数据库升级方案。只是建议，不代表我们已经采用。`
2. `刚才你的方案尚未获批，当前开发环境仍然是 SQLite。只确认，不写记忆。`
3. `目前没有做数据库迁移决定。只确认，不写记忆。`

预期：后台不把模型推荐的数据库写成“Aurora 已经使用该数据库”，已有 SQLite 事实不被修改。保留模型第一轮建议、复盘后的数据库正文作为对照。

**人工用例 9：后台遇到事实冲突不覆盖当前资料**

前置：通过 Memory 页面添加一条 note，主题 `Aurora 数据库`，正文 `Aurora 的开发环境使用 SQLite。`，记录 ID 和 v1。

依次发送：

1. `2026-10-06，Aurora 的开发环境改成了 Supabase。只确认，不调用记忆工具。`
2. `这是新的变化情况。本轮先不要编辑数据库记忆，只确认。`
3. `先结束这批对话，只确认，不调用记忆工具。`

预期：后台复盘只允许 list/search/add；原 note 的 ID、正文、版本均不变，也不新增一条宣称取代当前资料的冲突 note。允许以 episode 记录带日期的变化。该行为符合当前设计，正式修正资料需按用例 10 显式编辑。

**人工用例 10：显式修正记录并读取新版本**

前置：沿用用例 9 的 note。

在 Memory 页面将 note 改为 `Aurora 的开发环境使用 Supabase。` 并保存。同一会话再次问 `Aurora 的数据库配置是什么？`，再新建会话问一次。

预期：ID 不变、v1→v2；两次新查询的临时参考包含新值/v2，system 都不含 note，原会话 system 保持冻结。旧模型回复可能仍提到 SQLite，应以本轮参考和版本判断最新记录。

聊天无法调用 memory.replace，修正以 Web 保存成功和版本变更为准。

**人工用例 11：两个编辑窗口争用同一版本**

前置：同一测试服务，两个浏览器页签均选择同一条 note 的当前版本，各自输入不同草稿。

先在页签 A 保存，再回到页签 B 尝试保存旧草稿。

预期：B 不会覆盖 A。页面刷新可能提前检测到版本变化并禁用保存；若旧请求已发出，服务应返回 HTTP 409。B 的草稿保留，展示版本冲突提示。

在 B 中确认最新内容后，可选择“采用当前版本”再保存，版本继续递增；也可以“草稿另存为新条目”。记录 A 的已保存正文、B 的草稿和冲突提示。切回页签会自动刷新，因此无需强求一定产生 409 请求。

**人工用例 12：删除后新会话不能召回该记录**

前置：review_every_turns=0，通过 Memory 页面添加独立 note `Atlas 项目备用端口是 8123。`，记录 ID。新建会话查询一次，确认动态召回。

删除该条目，在同一会话及新会话询问备用端口。

预期：下轮参考不含已删除 ID。旧模型回复/用户输入仍可包含先前信息，删除记忆不擦除聊天历史，因此应检查召回结果而非只检查答案。

**人工用例 13：用户画像在旧会话中冻结**

前置：添加 user `用户偏好中文。`，开启会话 A；随后通过 Web 改为 `用户偏好英文。`。

继续 A 并新开会话 B，各自请求按偏好回答。

预期：A 的 system 保留中文，B 使用英文；user 不参与 note/episode 动态搜索。后台新增 user 也只影响后续新会话。

对实际冻结 prompt 作对比；仅看回答不能证明画像读取策略。

**人工用例 14：复盘轮数按会话分别累计**

前置：空库，N=3。创建会话 A 和 B。

在 A 完成两轮，在 B 完成一轮。每轮输入一个已确认的偏好或项目事实，并要求只确认、不调用记忆工具。随后在 A 完成第三轮，再在 B 补足第二、第三轮。

预期：A 两轮加 B 一轮不触发任何复盘；A 的第 3 轮触发 A 的复盘，B 的第 3 轮触发 B 的复盘。复盘历史只来自对应会话，记录来源 session_id 正确。

同一后端/namespace 的记忆是共享的，所以 B 可以读取 A 已提炼的记忆；会话计数隔离不代表记忆按 session_id 隔离。

**人工用例 15：重启后保留轮数和已复盘进度**

前置：空库，N=3；记录会话 session_id。

1. 完成两轮有效对话，确认尚未复盘。
2. 正常停止并重新启动同一配置，保留同一数据库、namespace 和会话。
3. 确认没有自动补跑复盘；在原 session_id 完成第三轮。
4. 等复盘完成后再次重启，在原会话完成第 4、5、6 轮。

预期：第 3、6 个成功轮次各触发一次复盘；第二批只提炼未复盘区间，第一批事实不重复写入。若浏览器生成了新 session_id，这是新会话计数，不能作为重启丢失进度的证据。

退出时尚未完成的任务不会在启动时自动执行；检查恢复行为必须继续达到下一次触发轮次。

**人工用例 16：取消前台轮次不计入 N**

前置：N=3，原会话已有两个成功完成的轮次。

发送一个需要较长回复的问题，生成期间点击停止。确认该 turn 的最终状态确为 `interrupted`，然后再发送一轮能正常完成的问题。

预期：被取消的轮次不触发复盘；下一轮成功完成后，成功轮数达到 3，才触发复盘。检查 turn 状态和 review 日志，不按点击次数计数。若点击前已经 completed，应重新执行本用例。

取消轮次的部分输入可能仍在会话历史中；本用例验证触发计数，不要求后台永久忽略这些历史内容。

**人工用例 17：只关闭后台复盘，保留记忆功能**

前置：已有一条 user 记忆。在配置副本中设 `review_every_turns=0`，重启。

新建会话询问已存画像及项目事实，再完成至少三轮新事实，最后通过 Memory 页面查看列表。

预期：画像仍可注入、事实仍可召回、Web 可 CRUD；不触发复盘、不自动新增记录。前台仍不能调用 memory.add。

**人工用例 18：关闭整个记忆功能**

前置：已有测试记忆。在配置副本中设 `[memory] enabled=false`，重启并新建会话。

发送：`请介绍一下你能够使用的工具。`，并打开 Memory 页面或查询其接口。

预期：当前新会话不注入数据库记忆，也不提供 memory 工具；普通 Web 对话仍正常。记忆接口返回禁用状态，而非返回正常空库。恢复 enabled=true 并重启后，之前的记录仍然存在。检查新会话，不用旧会话冻结的资料判断是否关闭成功。

**人工用例 19：相似事件按日期保留为独立历史**

在 Memory 页面添加两条 episode，主题均为 `Aurora 压测`，正文均为 `Aurora 压测完成，缓存命中率为 95%。`，日期分别为 `2026-10-05` 和 `2026-10-06`。

新建会话发送：`Aurora 两次压测的日期分别是什么？请区分两次事件。`

预期：两条事件均被保留，ID 不同、日期正确。SQLite 对匹配度相同的有日期事件按事件时间排序；Supabase 首先按向量相似度排序，不要求两种后端的排序完全一致。可再编辑其中一条日期，确认另一条没有变化。

**人工用例 20：类型过滤、结果数量与只读查询**

前置：通过 Memory 页面添加一条主题含 Redis 的 note，以及两条内容含 Redis 的 episode，记录三条 ID 与版本。

配置 memory_recall_top_k=2 并重启，再问 Redis 历史事实。

预期：搜索只查 note/episode，每类最多 2 个候选，保留各后端排序、交替合并、按 ID 去重后最多注入 2 条；user 不参与动态搜索。查询前后正文、版本和总数均不变。精确参数与排序由自动化或测试请求记录确认。

在此用例中用 review_every_turns=0，避免查询轮次恰好触发后台提炼干扰总数对比。Supabase 未命中时另检查模型、阈值与语义相似度，不把空结果当作过滤失效。

**人工用例 21：同义问法对比关键词与语义召回**

前置：添加 episode `Aurora 曾决定给高频查询保留 Redis 缓存，以降低接口响应延迟。`。

分别发送：

1. `此前关于 Aurora 的 Redis 缓存决定是什么？`
2. `此前为了让 Aurora 接口响应更快，我们采用了什么措施？`

SQLite：第一种关键词查询应能找到记录；第二种改写可以未命中，因为该后端采用关键词检索。

Supabase：两种问法用于评估 embedding 质量。记录规划的实际关键词、命中与排名；未命中时区分接口报错与检索质量，按实际模型调整阈值。不承诺任意同义句命中。两种后端的事实均只交给本轮 user 请求副本。

**人工用例 22：画像与事实预算独立**

前置：设 memory_max_chars=200、memory_recall_max_chars=650 并重启。添加短 user `用户偏好中文。`，再添加超长 note，开头含 `项目长说明：`。

新会话发送：`按我的回答偏好介绍项目长说明。`

预期：能容纳的完整 user 条目进入 bootstrap，note 永不进入 system；相关事实块可截断正文并标注，保留来源，总字符数不超过 650。设 memory_recall_max_chars=0 则停止事实搜索，画像仍可用。

预算统计的是格式化后的记忆块字符数，包含 ID、版本和主题，不是 token 数，也不是整个 system prompt 的长度。

**人工用例 23：Supabase namespace 隔离**

前置：同一 Supabase 项目，准备两个配置副本 A/B，各有独立会话数据库；namespace 分别为 `memory-web-test-a` 和 `memory-web-test-b`。

在 A 的 Memory 页面添加 user `用户的验收标记是松林。`，然后启动 B，新建会话搜索该标记。再切回 A 查询同一标记。

预期：B 的记忆列表与搜索不读取 A 的条目，A 仍能读到。Supabase 表中记录的 namespace 正确，复盘进度也不会跨范围复用。

namespace 是查询范围，持有服务密钥的调用方仍有跨范围权限；此用例验证后端筛选，不证明多用户身份授权。

**人工用例 24：Supabase 凭据错误与后端故障**

先做可直接执行的启动失败检查：在配置副本中将 credential_ref 改为 `env:SUPABASE_MEMORY_TEST_INVALID_KEY`，并在配置副本旁的 `.env` 新增该测试变量，值设为 `invalid-for-test`。保留原真实凭据变量。

预期：启动时结构检查/认证失败，明确报告 Supabase 不可用；不自动切到 SQLite，也不把该状态当作空云端记忆库。恢复配置引用后可重新启动。

若能只阻断 Supabase 并保持主 LLM/MCP 正常，使用已有会话同时询问记忆与知识库事实，继续触发复盘。新会话的画像读取失败也应明确报告。

预期：事实状态 failed，知识库证据仍进入请求，前台可正常回复；复盘失败不推进游标，不切换 SQLite。恢复后下次 N 轮边界处理未复盘区间，成功写入的部分条目不保证回滚。若主 LLM 同时断网，故障环境不满足条件。

**人工用例 25：本地 embedding 暂时不可用**

前置：Supabase 表已初始化，主 LLM 与 embedding 服务能够独立运行；先新增一条记忆并确认向量读写正常。

停用 embedding 服务并保留主 LLM/Supabase 可用。刷新 Memory 列表，尝试新增记录、询问历史项目事实以触发自动召回，再完成 N 轮新事实。

预期：不需要 embedding 的列表读取仍能成功；新增/搜索调用返回 embedding 错误。新增失败不应留下只有正文、没有向量的新记录。后台若实际尝试 add/search 并失败，该批复盘不推进完成游标，前台普通回复仍可成功。

恢复 embedding 后重做添加与搜索，应正常工作；后续触发的复盘可处理未完成区间。若后台没有调用 add/search，本轮并未触发故障，应标记“未触发”，不要判为通过。若主 LLM 和 embedding 共用同一个被停掉的进程，也不符合本用例的隔离前置条件。

**检查冻结系统上下文的只读方法**

Web 回复本身不足以证明记忆被注入。可以在 E:\FunAgent 的 PowerShell 中读取上文独立测试会话库的最近三个冻结上下文；此命令不调用模型、不写数据库，也不依赖记忆后端类型：

```powershell
@'
import sqlite3
from contextlib import closing

with closing(sqlite3.connect("file:.funagent/memory-web-test/agentcore.db?mode=ro", uri=True)) as db:
    rows = db.execute(
        "SELECT id, system_prompt_bytes FROM sessions ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    for session_id, prompt in rows:
        print("SESSION:", session_id)
        print(prompt.decode("utf-8"))
'@ | uv run python -
```

替换为实际独立测试目录。读取的是冻结 system；动态资料不应出现在 `/api/sessions/<session_id>/messages` 中。两种记忆后端的会话库都在本地。查询事件可确认状态和数量；临时正文通过离线用例捕获的实际模型请求或测试端点记录确认。

以下只读检查用于确认数据库 content/api_content 未被临时参考修改：

```powershell
@'
import sqlite3
from contextlib import closing
with closing(sqlite3.connect("file:.funagent/memory-web-test/agentcore.db?mode=ro", uri=True)) as db:
    for role, content, api_content in db.execute("SELECT role,content,api_content FROM messages"):
        assert "<retrieved_memory>" not in str((content, api_content))
        assert "<retrieved_knowledge>" not in str((content, api_content))
    print("历史未保存临时参考块")
'@ | uv run python -
```

**人工用例 26：重复召回不膨胀历史**

review_every_turns=0，同一会话连续五轮询问同一项目事实。每轮可重新命中同一 ID，但参考只在当前 user 请求副本附加一次，历史 user 仍是原文；纯文本五轮共十条消息。最后发送“谢谢”，规划跳过后不再提供先前参考（正常模型回复主动引用的事实除外）。

**人工用例 27：共享规划、独立来源**

启用 Modular RAG，分别询问历史决定、知识库制度、需要两者的问题和闲聊。每轮只有一次规划，各 need 字段独立控制搜索，need_knowledge=false 时不查知识库。关闭 knowledge.enabled 后，规划 prompt 不包含知识库规则/字段，事实召回仍可用；关闭记忆时不包含记忆规则/字段。两者关闭时不调用规划器。模型意外输出禁用来源字段不能触发搜索；模式信息不进入 prompt 或模型输入。

**人工用例 28：规划失败、重试和恢复**

使用离线测试模拟无效规划 JSON：已启用来源分别按原问题回退一次，无扩写。模型重试或调用其他工具时参考保持一致、不重新规划；恢复时根据最近真实用户问题重新检索最新资料。取消、租约和总预算中断见 test_memory.py。

**人工用例 29：不能绕过前台工具隐藏**

配置副本显式 session.enabled_tools 加入 memory，询问 `请直接调用 memory.add 保存这句话。`。schema 无 memory；猜测调用返回 unknown_tool，数据库不变。Web CRUD 和后台复盘仍可用。

**人工用例 30：后台复盘历史隔离**

添加相关 note，查询后达到 N 轮。复盘的待提炼对话无 retrieved_memory/retrieved_knowledge 原文；末尾单独读取的数据库视图仍包含 user/note，用于去重。正常模型回复中的引用仍属于对话。以隔离测试与复盘请求确认。

**人工验收记录建议**

| 用例 | 配置/数据范围 | session_id / turn_id | 关键证据 | 结果 |
| --- | --- | --- | --- | --- |
| 例如 1 | SQLite 测试目录，N=3 | 填写实际 ID | review.completed；source.type=review；记录 ID 与 v1 | 通过/失败/未执行/未触发 |

对“精确请求传递、触发计数、版本冲突、隔离”给出功能性结论；对“提炼是否准确、同义句能否命中”记录模型质量结论。允许正文措辞变化，但不能忽略捏造事实、冲突覆盖、无来源回答或搜索失败。
