# 数据准入 T3 · 显式迁移 CLI 本地候选

T3 让维护者通过 `tianshu data migrate --data-root ROOT --backup BACKUP [--json]` 调用已审迁移库。本机两文件定向通过；独立实现复审、同步实际 main 后的最终组合全量与新原生 CI 尚待验收。本文不是 T3 批准或 G3 完成记录。

基线为 `585e3474b7f9f03bb118308470fac1f500c3a02e` 加 T2 精确冻结，工作分支为 `codex/g3-data-admission`。T2 输入清单 SHA256 为 `dddec59d87775811550493c535efee2fdc30faff199c41bb6bdbf9c80598c68a`；父会话已核 T2 的独立 `PASS_TASK2_CONTENT_ONLY`，未决 0。该结论只覆盖 T2 内容，不代替 T3 或最终组合的验收。T2 历史候选事实记录与证据保持原件，不回写为新结论。

权威输入为 [数据准入契约](data-admission-contract.md)，正文 SHA256 为 `c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`，以及既定实施计划 Task3（计划 SHA256 为 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`）。本片只修改 `data_cli.py`、新增迁移 CLI 测试、原 `test_data_cli.py` 末尾入口门禁，以及授权事实文档。T1/T2 源码测试、backup、maintenance、cli、恢复、Host/UI 和其他 debug 范围不改。

## 命令行为

入口要求显式标准根与现有备份，不创建隐式备份，不启停服务，不执行 restore apply。迁移算法、版本判定、布局与摘要均委托 T2 的 `migrate()`，未复制。

- 成功退出 `0`；失败只捕获共享的 `AdmissionRefused`，退出 `2` 并保留其真实 `code`。六类为 `incompatible`、`migration_incomplete`、`progress_corrupt`、`backup_mismatch`、`maintenance_busy`、`migration_failed`。
- JSON 失败含 `operation="migrate"`、`ok=false`、`code`、`error`；成功含 `ok=true`、`code=null`、原迁移报告字段和 `message`。文本使用库的 `to_text()`；文本/JSON 统一通过既有 `redact()`。
- 四个既有 `migration-before/after-step-1/2` 仍由真实子进程 `os._exit(77)` 终止，不被 CLI 包装。
- `RuntimeError`、`ValueError` 等内部编程异常继续传播到原主 CLI 边界，不统一伪装为六类域失败。

使用步骤和数据边界见 [维护指南](backup-restore.md) §0/§5。

## 测试实际证明的内容

新文件 29 项迁移测试，加原入口文件 27 项，共 **56 项**。最终命令、运行前后源/测试哈希、原始输出及范围核对见 [证据目录](evidence/data-admission-t3-2026-11-13/README.md)。

1. 缺 root/backup 参数真实退出 `2`，不会初始化目录；缺失根、空根与只有 custom DB 的目录真实拒绝且内容不变。restore 与 restore-plan `--apply` 仍不暴露。
2. 六 code 各用真实单故障路径：未来机制版本、已迁 v2 缺进度、坏 JSON、未知业务投影变化、子进程持共享 EventLog、真实进度原子发布 OS 失败。每条移除故障后有真实迁移正控；未手写同一异常冒充六条产品路径。
3. 共享锁子进程实际打开 EventLog 后经管道报告屏障；迁移拒绝，原连接仍能追加真实事件。迁移子进程在真实独占锁内报告另一屏障，第二个 EventLog 子进程被拒且 payload 目录尚未创建；释放后命令完成。未用同进程双连接或 sleep 猜时序，子进程与读屏障线程有超时和收尾。
4. 四原名 failpoint 分别真实退出 `77`；逐点核 DB 版本、严格 format2 的 applied/current、普通 EventLog 中间态拒绝；续做退出 `0`，再次调用仍退出 `0` 且逻辑内容、材料和进度字节不变。完整事件行及版本行的前缀、顺序与内容保持，最终两步审计各一条，append-only UPDATE/DELETE 触发器仍拒绝。
5. 真进度 rename 失败与真 SQL/DDL 失败均为 `migration_failed`，保持 DB v0、历史内容不变且不出现 completed；解除故障后迁移完成。真实 hardlink/symlink 先证构造，再证在创建进度前拒绝。只有平台原生无法构造链接时才明确 skip，本机该组未跳过。
6. 成功/失败、文本/JSON 四种输出中的假 `sk-` 路径均被脱敏；两种编程异常直接从 `data_cli.run()` 传播且不输出域报告。

## RED 与本机门禁

首轮真实 RED 为 **25 failed / 26 passed，7.13s**，失败全部发生在 `migrate` 尚未暴露处。它只证明入口缺失，不能当成六类深层协议或互斥的负控。原源/测试及日志均保全。之后根据真实 schema，将作者测试的一处 payload 下标改为显式 `SELECT payload`；该错误未在首 RED 到达，不是产品缺陷。

最小接线后首轮 **51 passed，8.95s**；补充三种非标准根和两种编程异常后 **56 passed，8.70s**。最终冻结定向为 **56 passed，9.02s**；`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 均真实退出 `0`，三份源/测试运行前后哈希相同。命令与结果独立保存在 `final-local-results.json`，不可用前述过程结果替代最终冻结绑定。

测试使用既有 `run_clean.py` 在子进程局部去除 `TIANSHU_*` 及以 `BASE_URL`/`API_KEY` 结尾的配置，不打印值、不改全局环境。只在 macOS arm64/Python 3.12 本机执行；没有 Windows 设备、ARM64 Windows、新原生 CI、UI 或全量结论。T2 25 项输入中 24 项非知识库原件保持哈希，知识库仅在 §2/§8 新增 T3 状态；T3 起点 463 项现存文件中 459 个非授权对象无漂移。

## 待独立验收

第二读者应绑定最终候选源/测试哈希，独立复跑真实六 code、双进程屏障、四硬退出续做和失败不 completed，核对结构化错误、脱敏与异常边界；作者上述结果不构成批准。同步实际 main 后由父会话集中跑最终组合全量与四 required CI，再绑定正式提交；未 commit/push、不借旧绿色代替新结论。

实际恢复隔离、restore apply、容量保护与 GC 未在本片交付。0→2 仍只是两步迁移机制；维护锁仍是合作式准入，不对抗 raw SQLite/旧版程序或同 uid 恶意写入。
