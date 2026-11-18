# 2026-11-18 · G3-B05 / W11（Part B）：`tasks.py` / `loop.py` / `dispatcher.py` 巨石模块扁平拆分（零契约变更）

## 背景与目标

继 W11 Part A 完成 `cli.py`（`→ _cli_{ops,memory,eval}.py`）与 `memory.py`（`→ _memory_{types,apply}.py`）拆分后，本批次对内核剩余三个最大热文件 `tasks.py`、`loop.py`、`dispatcher.py` 执行纯结构重构（契约 C1–C5、SQLite 投影 Schema、事件 payload、异常类与对外导出符号 100% 保持不变），完成 W11 巨石模块拆分的全部代码收口。

## 关键实现与守护设计

1. **单层扁平布局与全仓静态守卫兼容（`src/tianshu/_tasks_*.py` / `_loop_*.py` / `_dispatcher_*.py`）**
   - 保持 `src/tianshu/*.py` 单层扁平模块布局（不建嵌套子包），确保 `tests/test_windows_compat.py`（非递归 `.glob("*.py")` 二进制文件 I/O AST 守卫）与 `tests/test_rule_ids.py`（C4 规则编号产生点 AST 扫描）全量覆盖所有拆分子模块；所有新增与修改模块保持纯二进制 I/O 与零 `# type: ignore`（顺手消除原 `tasks.py` 10 处 `# type: ignore[return-value]`，改为显式投影存在性断言）。

2. **`tasks.py` 拆分为门面 + 3 个内部子模块**
   - `tasks.py`：保留 `TaskStore` 核心入口（Task/Run 创建、目标与绑定修订守卫串行化 fencing、`begin_execution` 线性化许可、状态迁移、暂停/取消控制），完整保留 `__all__` 并重导出 `RunFenced`、`RunCursor` 及全部状态/事件常量。
   - `_tasks_types.py`：抽离封闭状态/事件常量、SQLite 投影建表 `SCHEMA`、子任务限额校验与 UTF-8 字节截断纯函数、`Task`/`Run`/`Subtask`/`RunCursor`/`ExecutionPermit` 值对象与行映射。
   - `_tasks_subtasks.py`：抽离 `_SubtaskStoreMixin`（G2 D1/D2/W10 子任务委派创建、取消传播、父目标变更 fence、父作业唤醒与启动恢复，保留 `_settle_subtask_budget` 实例方法供负控注入）。
   - `_tasks_cursor_rebuild.py`：抽离 `_TaskCursorRebuildMixin`（持久游标读写、模型回合幂等提交、`_finish_locked` 与 `_run_end_payload` 终态结算及 `rebuild_from_events` 投影重建）。

3. **`loop.py` 拆分为门面 + 3 个内部子模块**
   - `loop.py`：保留 `SYSTEM_PROMPT`、`initial_messages`、`DiskAdmission` 导入、`Limits`、`RunResult`、`AgentLoop` 主驱动骨架、`run()` 与 `Stack`/`build_stack`，完整兼容评测报告（`tianshu.loop.SYSTEM_PROMPT`）与低磁盘入口（`loop.DiskAdmission`）的运行期 monkeypatch，并重导出 `_Progress`、`_fresh_progress` 等测试依赖符号。
   - `_loop_summary.py`：抽离摘要材料切块/字节计数纯函数与 `_LoopSummaryMixin`（共享预算账本预占与结算、§7.1 语义摘要生成/复用、`_complete_with_retry` 有界重试与流式增量回调）。
   - `_loop_memory.py`：抽离 `MEMORY_READ_TOOLS`、`MEMORY_WRITE_TOOLS`、`open_memory` 与 `_LoopMemoryMixin`（记忆装备与只读检索、`read_content` 多编码分页读取、写工具目标规范化）。
   - `_loop_bookkeeping.py`：抽离 `_Progress`、提案三元组判定、`message_source_seqs` 与 `_LoopBookkeepingMixin`（消息 `seq` 归因、spill 落盘、事件重建、审批重绑、未决派发与目录阻断对账、`_finish` 收尾及恢复认领）。

4. **`dispatcher.py` 拆分为门面 + 4 个内部子模块，同步对齐 AST 守卫与变异负控脚本**
   - `dispatcher.py`：保留 `Dispatcher` 门面（构造与迁移检查、只读查询、`prepare`、`dispatch`、`revoke`）、`build_request` 及模块级 `execution_guard`/`failpoint`/`net_fetch`/`require_isolation_requirements`，重导出 `_Authority`、`ExecutionGuard`、`PolicyDecision`、`is_ancestor_or_self` 及全部公开符号。
   - `_dispatcher_types.py`：抽离封闭常量集（保留 `D_DISPATCH_CALL_ID_REUSED` 与 `APPROVAL_REJECTED` 顶层无注解 `ast.Assign` 以匹配 `test_rule_ids` 常量表提取）、`SCHEMA`、异常与值对象、`resolve_scheme_resource`、`precondition_rule_id`、`_audit_args` 与 `_row_to_dispatch`。
   - `_dispatcher_authority.py`：抽离 `_DispatcherAuthorityMixin`（工具身份与 Run 执行资格核验、`exec` 隔离预检、记忆执行守卫、作用域/外发/MCP/子任务配额授权、目标解析与一次性票据消费）。
   - `_dispatcher_exec.py`：抽离 `_DispatcherExecMixin`（网络抓取、MCP 外发并保持 `_dispatch_mcp` 栈帧函数名不变、子任务委派执行、租约接管对账与 `ControlRun` 生命周期）。
   - `_dispatcher_ledger.py`：抽离 `_DispatcherLedgerMixin`（派发表写入、MCP 外发边界推进、终态参数脱敏回填、保持原位置参数签名的 `_reject`/`_reject_runtime`/`_fail_unknown`、以及单事务 `_commit_mcp_unknown`/`_commit_result`）；子模块内通过 `sys.modules["tianshu.dispatcher"]` 动态绑定守卫/探针/网络/故障注入入口以承接测试 monkeypatch。
   - 同步扩展 `tests/test_rule_ids.py::test_reserved_ids_really_have_no_reachable_path` 扫描 `dispatcher.py` 与全部 `_dispatcher_*.py`，并将 `scripts/negative_controls_mcp_crash.py` 与 `scripts/negative_controls_object_block.py` 中对应锚点的 `relpath` 对齐至拆分后子模块。

## 诚实边界

- 本批次为纯模块结构重构，不引入任何新功能或契约变更；合并入 `main` 仍须等待独立第二读者 Tier 1 复审通过及 GitHub 四项 required CI 全绿。

## 验证命令

```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen python scripts/negative_controls_object_block.py NC-O11 NC-O13 NC-O20 NC-O22 NC-O24 NC-O25 NC-O26
uv run --frozen python scripts/negative_controls_mcp_crash.py NB25 NB38
uv run --frozen pytest
```
