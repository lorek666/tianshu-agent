# PR #7 独立复审报告（M2 · PR A：Task/Run 与租约）

- 复审对象：PR #7 `feat(m2): Task/Run fencing + 目录身份与跨进程租约`
- 复审基线：`7f77597`（合并提交，88 tests passed）
- 复审方式：不读作者自查结论，按计划 `docs/superpowers/plans/2026-11-08-m2-permissions-tasks.md`
  §2.1/§2.2/§3 的 Task 1–2 验收项逐条对照，并写探针脚本与真子进程复现
- 复审结论：**PR #7 不能作为 PR A 的验收**。找到 6 类真实缺陷（3 类安全相关），
  本分支修复其中 5 类；另有 2 类能力缺失与 4 项未验证边界，见 §4。
- 修复分支：`fix/m2-pr-a-review`；修复后 `116 passed`，`ruff check .` 全绿

## 0. 为什么这次要独立复审

PR #7 是自写、自审、自合并。以下缺陷都不是"深水区"问题，是第二个读者十分钟内就能
发现的形状：**同一件事在三个地方各写一遍、判断只比一个字段、状态改完不写事件**。
自审的盲区不是能力，是位置——作者知道自己的意图，所以看不出实现没兑现意图。

## 1. 探针（复现证据）

在 `7f77597` 上直接调用库函数，不修改仓库：

```bash
uv run python /tmp/pra/probe.py      # P1–P6
uv run python /tmp/pra/probe3.py     # P7/P9：真子进程
uv run python /tmp/pra/probe4.py     # P10–P12：收尾与 fence 后的租约
```

关键输出（原文摘录）：

| 探针 | 结果（修复前） |
|---|---|
| P1 | `OperationalError: cannot start a transaction within a transaction` |
| P2 | 父子目录被当成不同资源，两个 run 同时持有同一棵子树 |
| P3 | `finish_run(outcome="noop")` → `state=noop`，`TERMINAL_STATES` 不含它 |
| P4 | `tasks` 2 行、`runs` 2 行，`events` 0 行 |
| P5 | 裸连接（未设 `row_factory`）构造 store → `TypeError: tuple indices must be integers` |
| P6 | `DEFAULT_TTL_MS=60000`（计划建议 30000）；`renew` 失败静默返回 0 |
| P9 | 父进程（非持有者）用同一 `run_id` 通过 `assert_held`；`release` 删掉了别人的租约 |
| P10 | Run 正常收尾后租约不释放，同 Task 的新 Run 立刻被自己挡住 |
| P11 | 改目标后新 Run 被已 fence 的旧 Run 残留租约挡住 |

P10/P11 的严重性容易被低估：它们让"用户改主意 → 新 Run 接手"这条主路径在 TTL 内
**必然失败**，而 PR C 整个改目标流程就建立在这条路径上。

## 2. 缺陷与处置

| # | 严重度 | 缺陷 | 处置 |
|---|---|---|---|
| F1 | **高** | 父子目录互斥失效：`(st_dev, st_ino)` 只能回答"是不是同一个目录"，`/ws` 与 `/ws/sub` 是两个 inode 却是同一棵树 → 两个 Task 可同时写同一棵子树；计划 Review Focus #1 明确要求挡住 | 已修：`ResourceIdentity.overlaps()` + 租约行存规范路径，ancestor/descendant 一律冲突；`test_parent_child_directories_conflict` 等 3 个用例 |
| F2 | **高（阻塞 PR B）** | 事务不可组合：`EventLog.append_many`、`TaskStore._tx`、`LeaseStore.*` 各自 `BEGIN IMMEDIATE`，组合即嵌套报错。PR B 必须"审批消费 + grant + 租约 + intent + 事件"一次提交，这个形状根本拼不出来 | 已修：`EventLog.transaction()`（已在事务中则复用，不重复 BEGIN）+ 各 store 走同一入口；`tests/test_transactions.py` 5 个用例 |
| F3 | **高** | 写守卫只比 `run_id`：任何进程拿着同一 `run_id` 就能通过 `assert_held`，`release` 能删掉别人的租约（P9）。pid 会被复用，"同 pid"也不等于"同进程" | 已修：`ProcessIdentity{instance_id, pid, started_at}`，租约行记 `owner_instance_id`；`assert_held`/`renew`/`release` 都要求实例匹配；`test_foreign_instance_cannot_pass_guard` |
| F4 | **高** | fence/收尾不释放租约：Run 结束或被 fence 后租约行留到 TTL 到期，同一 Task 的新 Run 被自己上一个 Run 挡住（P10/P11） | 已修：`LeaseStore.release_run()`，`finish_run`/`_fence_run` 在同一事务内释放；`test_revise_goal_fences_running_run_and_frees_workspace` |
| F5 | **高（契约）** | Task/Run 根本不是事件的投影：状态直接写表，一条事件都没有（P4/P8）。计划 §Architecture 写的是"事件仍是事实源，Task/Run/Approval/Grant/Lease 为同库事务更新的可重建投影"，回放因此无法解释某 Run 为什么被 fence | 已修：`task.created` / `task.revised` / `run.start` / `run.finish` / `run.fenced` 五类事件与投影同事务提交；`rebuild_from_events()` + 重建测试 |
| F6 | 中 | `finish_run` 接受任意 `outcome`/`effect_state` 并把 `state` 写成任意值（P3），Run 永远不是终态，同一 Task 又能开新 Run | 已修：白名单校验；`test_unknown_outcome_or_effect_state_is_rejected` |
| F7 | 中 | store 依赖外部连接已设 `row_factory`（P5），换一个调用方就 `TypeError` | 已修：store 自行设置 |
| F8 | 中 | 测试证据不成立：所谓"两个进程"是同一进程两条连接，过期/接管靠 `sleep(0.05)` 猜时序；计划明确要求 subprocess + barrier，不用 sleep | 已修：`test_two_processes_one_resource`、`test_live_process_blocks_another_process` 用真子进程 + stdout 屏障；TTL 用注入时钟推进 |
| F9 | 中（新增能力） | 缺 `execution_guard`（计划 Task 2 明确要求）。没有它，`assert_held` 是"读一下"，读完到真正写之间可被接管，文档里"保证过期后不会再写"是过度承诺 | 已加：`execution_guard()`（POSIX flock / Windows `msvcrt.locking`）+ 跨进程互斥、超时、**硬退后 OS 释放**三个用例。**未强制**"所有 epoch 变更必须在 guard 内"，见 §4 |
| F10 | 低 | 默认 TTL 60000ms 与计划 30000ms 不一致；`renew` 失败静默返回 0，心跳失败不会被发现 | 已修：`DEFAULT_TTL_MS=30_000`、`HEARTBEAT_MS=5_000`；`renew` 续不满即抛 `LeaseLost` |
| F11 | 低 | `create_task` 的目录只有路径字符串，没有 D6 决定的 `mode`/`snapshot_policy`；空目录任务与"读 A 写 B"无法表达 | 已修（数据模型）：`WorkspaceBinding{root_path, mode, snapshot_policy}`，旧字符串自动升级；策略判定本身属 PR B |
| F12 | 低 | `revise_goal` 不检查 Task 状态、空 goal 可写入 | 已修 |

## 3. 修复后验证

```bash
cd /Volumes/Date/天枢Agent
uv sync --python 3.12
uv run pytest        # 116 passed（修复前 88）
uv run ruff check .  # All checks passed!
```

新增/重写用例：

- `tests/test_transactions.py`（新，6 项）：投影+事件一起回滚、跨 store 组合提交、三层嵌套复用外层事务、
  内层失败回滚外层、另一连接无法插进已开始的事务
- `tests/test_tasks.py`（重写，21 项）：0 目录合法、binding 持久化、未知 outcome 拒绝、
  `assert_current`/`assert_write_allowed`、5 类事件序列、**清空投影后按事件重建逐字段相等**
- `tests/test_leases.py`（重写，26 项）：父子目录双向冲突、符号链接别名冲突、
  注入时钟推进 TTL、外来实例不能过守卫/续租/释放、真子进程互斥、
  执行守卫跨进程互斥 + `os._exit` 后释放、旧库缺列自动迁移

## 4. 未修复 / 未验证（不得当作已解决）

PR #7 计划覆盖的 Task 1–2 交付物仍缺以下部分，均属 PR B 前置，本分支没有补：

1. `jobs.py` / `JobClaim{owner_instance_id, claim_epoch}`、`claim_next`/`heartbeat`/`finish`。
   没有 claim_epoch，"活着但已过期"的旧 worker 仍可能改新 owner 的状态。
2. `RunCursor` / `save_cursor` / `commit_model_turn(claim)`、`start_run(task_id, budget)`。
3. `execution_guard` 的**强制使用**：目前只是提供了锁，没有任何调用点保证
   "租约/epoch 切换与 `execution.started` 在同一把锁内"。在 PR B 接线之前，
   F9 的 TOCTOU 窗口仍然存在，`assert_held` 的措辞仍只是"执行侧双检之一"。
4. `authority` 数据根与 `--db` 逃逸防护（计划 `test_changed_db_cannot_escape_authority`）。
5. `schemas/m2/task.json`、`run.json`。
6. 计划 §2.2 的 takeover 协议：TTL 到期不等于旧 worker 已停；接管必须先拿 guard、核实 owner、
   核对未决副作用。当前实现只做到"过期/进程消失即可接管"。
7. PID 复用检测：`instance_id` 解决"不是同一实例"，但 `_pid_alive()` 对**被复用的 pid**
   仍会答"活着"，后果是死租约要等 TTL 才可接管（可用性，不是安全性）。Windows 上
   `_pid_alive` 恒为 True，只能靠 TTL。
8. 未验证平台与边界：Windows 原生进程（`msvcrt.locking` 分支未跑过）、真实断电/fsync、
   租约与真实文件写入的交叉（dispatch 尚未存在）、跨用户/跨会话的 `%TEMP%` 差异。

## 5. 对流程的意见

- M2 剩余 PR 建议保持"作者之外的人复审后再合并"。本轮 6 个缺陷全部属于
  "写的人看不出、读的人十分钟看得出"的类型。
- 计划里的验收项是**契约**，不是建议。PR #7 漏掉的不是边角，是计划正文用加粗写明的那几条
  （父子目录、跨进程证据、投影可重建、execution_guard）。合并前应逐条对照打勾，
  而不是看测试数量。
- "测试通过"不等于"验收项通过"：88 个用例里没有一个真子进程，也没有一个 sleep 之外的
  时间控制手段，所以"跨进程"这四个字在 PR #7 里没有证据。