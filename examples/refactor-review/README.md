# P5 离线后台复盘示例

在重构工作树根目录启动：

```powershell
$env:PYTHONPATH = "src"
& E:\FunAgent\.venv\Scripts\python.exe -m funagent --conf-dir examples/refactor-review
```

依次操作（替换会话/任务 ID）：

```text
new P5 demo
submit <session_id> remember: 用户喜欢中文
wait <task_id>
review <session_id>
memory list facts profile
submit <session_id> memory: 中文
wait <task_id>
history <session_id>
quit
```

主回复提交后独立 worker 写入 profile 事实，水位与 through_message_id 一致。`wait` 等待主任务；`review` 查询后台状态，若 running/queued 可再次查询。下一轮画像与召回直接读取已保存事实。history 只有主聊天，不包含 review/recall 文本。DemoProvider 的 remember: 仅为离线演示协议，实际模型通过同样的固定记忆工具集执行。

memory.conf 的 policies.review 通过 enabled = true 启用；未声明 enabled 时默认关闭。profile 与 fallback 由启动绑定，sources 只能包含可写 episodic 来源，capacity 限制待处理会话数（running 独立一项），timeout 包括连接锁等待。待处理同会话上界合并，运行 job 的范围固定。

复盘只允许 memory_search/write/update/delete；每笔变更都要成功确认，才以 CAS 推进 app.sqlite3 水位。普通 final、无变更（NO_WRITES）、失败/unknown/取消/CAS 冲突保留原水位；成功写入仍保留，下一次主任务完成或内部 Schedule 从原水位重读。没有无限自动 retry。事实写入使用来源、规范正文、类别和真实 message IDs 的稳定键，不承诺语义等价去重。

memory.sqlite3 的删除 tombstone 保留可信版本与证据回执，复盘重试能读取并确认相同删除；普通检索不显示回执。Update 重试读取最新版本后再 CAS；旧版本冲突保持可见。两个数据库分别提交，没有跨库事务。Stop 等待 main/review workers 实际退出后再关闭连接。
