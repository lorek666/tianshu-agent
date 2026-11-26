# 空任务轮询只读预检

## 背景与根因

Windows hosted CI 曾在首次 `schedule.create` 报 `sqlite3.OperationalError: database is locked`。独立诊断无法证明唯一锁持有者，但确认空队列轮询和启动恢复会在没有可处理候选时仍进入 `BEGIN IMMEDIATE`：

- `JobStore.claim_next()` 每次轮询都先拿写锁，再发现没有 `pending` 或 `claimed` 作业。
- `wake_stale_parents()` 启动时即使没有 `waiting` 作业也开写事务。
- `reconcile_finished_subtasks()` 启动时即使没有未完成子任务也开写事务。

这些无谓写事务会与 daemon 启动、控制请求和其他恢复写入争抢 SQLite 写锁，Windows I/O 放大后更容易暴露。

## 决策

在三处事务前增加只读候选预检；空集合直接返回。存在候选时仍进入原有写事务并重新判定：

- `claim_next()` 的判定与认领仍在同一个 `BEGIN IMMEDIATE` 事务内，claim 线性化与 epoch  fencing 语义不变。
- 两项启动恢复仍在写事务内重新读取并处理候选，不把预检当作正确性依据。
- 不延长 busy timeout、不跳过测试、不放宽 SLO 或 CI 门禁。

## 验证

定向红绿使用真实子进程持有 `BEGIN IMMEDIATE`，通过 stdin/stdout 屏障精确控制锁窗口；父进程将 busy timeout 注入为 50ms：

- 修复前，`claim_next`、`wake_stale_parents`、`reconcile_finished_subtasks` 三项均真实报 `database is locked`。
- 修复后，三项均在外部写锁存在时返回空结果，不再申请写锁。
- 受影响回归：`tests/test_job_claims.py`、`tests/test_g2_delegation_exec.py`、`tests/test_g2_delegation_fence.py`、`tests/test_schedule_webhook.py` 通过。
- `uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 通过。

本机全量 `uv run --frozen pytest`、独立第二读者复审与 hosted required CI 尚待完成；未完成前不得宣称该候选已收口。
