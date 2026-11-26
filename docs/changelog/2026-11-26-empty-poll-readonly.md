# 空任务轮询只读预检

## 背景与根因

Windows hosted CI 曾在首次 `schedule.create` 报 `sqlite3.OperationalError: database is locked`。独立诊断无法证明唯一锁持有者，但确认空队列轮询和启动恢复会在没有可处理候选时仍进入 `BEGIN IMMEDIATE`：

- `JobStore.claim_next()` 每次轮询都先拿写锁，再发现没有 `pending` 或 `claimed` 作业。
- `wake_stale_parents()` 启动时即使没有 `waiting` 作业也开写事务。
- `reconcile_finished_subtasks()` 启动时即使没有未完成子任务也开写事务。

这些无谓写事务会与 daemon 启动、控制请求和其他恢复写入争抢 SQLite 写锁，Windows I/O 放大后更容易暴露。

## 决策

在三处事务前增加只读候选预检；空集合直接返回。存在候选时仍进入原有写事务并重新判定：

- `claim_next()` 的判定与认领仍在同一个 `BEGIN IMMEDIATE` 事务内，claim 线性化与 epoch fencing 语义不变。
- 两项启动恢复仍在写事务内重新读取并处理候选，不把预检当作正确性依据。
- 不延长 busy timeout、不跳过测试、不放宽 SLO 或 CI 门禁。

## 验证

定向红绿使用真实子进程持有 `BEGIN IMMEDIATE`，通过 stdin/stdout 屏障精确控制锁窗口；父进程将 busy timeout 注入为 50ms：

- 修复前，`claim_next`、`wake_stale_parents`、`reconcile_finished_subtasks` 三项均真实报 `database is locked`。
- 修复后，三项均在外部写锁存在时返回空结果，不再申请写锁。
- 首次独立复审通过三条产品预检，发现测试夹具可能在 launcher 已退出后仍等锁进程关闭 stderr。
  夹具现在直接启动原生解释器，保留有界的握手及退出等待，失败诊断只从文件读取有限字节。
- 上一轮完整测试观察到共享 Host 测试读到尚未写完的握手标记。夹具现在先写完整身份再原子发布标记，
  保留 A 握手 / B 换锁的真实子进程判据。
- 受影响回归：`tests/test_job_claims.py`、`tests/test_g2_delegation_exec.py`、`tests/test_g2_delegation_fence.py`、`tests/test_schedule_webhook.py` 通过。
- `uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 通过。

冻结树 `868c6ac98cf71e56ccb2b8ee4827b4a5b7b4d175` 的裸 `uv run --frozen pytest` 退出 0；
日志 SHA-256 `fe9604acae8ff13a4ce74c6b6ade9730fb84cd0a833d94549202f8f0c9e31a21`。独立第二读者按该树复现三处旧写锁及两处旧夹具故障，
并验证非空候选仍在写事务内重判、回收保持有界。主控在独立目录重放通过，
独立证据清单 SHA-256 `6f53330ff4fec11342aeb01f1af5e09c0e67a9f51250687046df584e471e850d`。

最终提交同步实际 `main` 后按同一冻结核对非文档树。托管验收以该 PR 的实际最终 head/base 及其新完整 CI 为准；
该修复不证明历史 `schedule.create` 失败时是哪一个进程持锁。
