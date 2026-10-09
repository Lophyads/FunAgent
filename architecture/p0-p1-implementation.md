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

待实施；之后记录实际 Red/Green 与所有权检查。
