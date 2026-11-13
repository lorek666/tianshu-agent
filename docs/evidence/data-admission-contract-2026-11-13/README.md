# 数据准入契约 · 设计审阅原件

本目录保存 2026-11-13 独立第二读者 `/root/review_admission_contract` 的设计探针与结果，
源码基线为 `f509a53881c372d8d7f4b1e2718130edaff4ed06`。
结论仅为[待实现契约](../../data-admission-contract.md)通过设计审阅，
不代表准入、迁移 CLI、恢复或整个 G3 已验收。

先读[独立报告](../../REVIEW-DATA-ADMISSION-CONTRACT-2026-11-13.md)。
`admission-contract-review-evidence.json` 记录原文件名、SHA256、版本和范围。
报告与以下原件按字节归档；原绝对路径是当次现场，不能要求它在另一台机器存在：

- `admission-contract-probes.py.txt` 与 JSON：单向材料比较的反例、真子进程硬链接锁反例、FTS shadow 保存点与 SQL 值类型。
- `admission-legacy-probes.py.txt` 与 JSON：原 helper 拒绝正常 v0、旧格式 completed 的真实形状、删除整个版本旁表后重开的历史误判。
- `admission-focused-tests.txt`：五个既有兼容回归的实际输出。

`.py.txt` 是原始证据文本，不能当成新产品脚本或 CI 测试。重现历史行为时，在上述基线的隔离 checkout
中把它们复制到临时目录，扩展名改成 `.py`；只在临时副本里把 `REPO`/`sys.path` 改为该 checkout，
把输出根改为一个已存在的临时目录。使用项目 Python 3.12，并仅在测试子命令中隔离
`TIANSHU_*` 与模型 endpoint/key 环境，不打印变量值。主探针自启动子进程，须保留实际脚本文件路径；
不能改成单个 `exec` 字符串冒充真子进程。它们只应操作合成数据。

当前分支仅比该源码基线多一行测试编码修正及文档，不改变这些历史产品对象。
新实现后其中的旧行为应改变；届时使用新的回归与负控验收，不把本目录结果冒充新实现通过。
下一步按契约 §5 和独立实施计划编写失败测试，再实施实际连接准入及受绑定迁移。
