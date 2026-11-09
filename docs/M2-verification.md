# M2 验收记录：权限、任务与作业生命周期

> M2 目标（PRD §11 / `docs/superpowers/plans/2026-11-08-m2-permissions-tasks.md`）：
> **越权注入被拦；删除必须经审批；租约跨实例不重复执行。**
>
> 本文只记录**实际执行过**的东西。没跑过的（Windows 原生、真实断电、桌面 Host 接线）
> 一律列在 §8，不写成"通过"。测试数量不等于验收项数量。

- 验收基线：`main@6cc985e`（PR #13 合并后，314 passed）
- 本阶段最终：PR C 分支（332 passed / `ruff check .` 通过），合并提交见 PR
- 平台：macOS 26.2 arm64（aarch64），Python 3.12.13，`uv run pytest`
- 复验命令：
  ```bash
  uv run pytest -q          # 332 passed
  uv run ruff check .       # All checks passed!
  ```

## 1. 三个 PR 与六个任务

| PR | 任务 | 交付 | 合并 |
|---|---|---|---|
| A | Task 1 Task/Run + fencing | `tasks.py`、`revisions`、`run.start/run.state/run.fenced` | #7 / #8 / #9 |
| A | Task 2 资源身份、租约、作业认领 | `leases.py`、`jobs.py`、`workspace.py` 资源身份 | #7 / #8 / #9 |
| B | Task 3 C4 策略、审批、一次性 grant | `permissions.py`、`schemas/m2/*` | #10 |
| B | Task 4 受控派发与独立执行核验 | `dispatcher.py`、`tools.py` 执行器、`guard.py` | #11 |
| C | Task 5 持久游标、暂停/取消、改目标 | `loop.py` 游标、`run cursor/pause/resume/cancel` | #12 / #13 |
| C | Task 6 作业表与端到端验收 | `worker.py`、`cli worker`、`test_m2_acceptance.py` | 本 PR |

## 2. M2 总验收逐项

计划 §4 要求"必须同时成立"。逐条对应到测试与实际结果：

| # | 验收项 | 结论 | 证据（测试名 / 实际输出） |
|---|---|---|---|
| 1 | C4 绕过被拒绝 | 成立 | `test_dispatch.py::test_direct_execute_without_permit_is_rejected`、`test_old_claim_after_takeover_cannot_dispatch`、`test_engine_without_claim_cannot_write`；工具层 `execute()` 对副作用工具**只**接受 `AdmittedCall` |
| 2 | 审批过期/伪造被拒绝 | 成立 | `test_permissions.py::test_expired_approval_cannot_consume`、`test_approval_decision_requires_human_identity`、`test_model_cannot_construct_human_identity` |
| 3 | 删除与整文件替换有审批证据 | 成立 | `test_m2_acceptance.py::test_approval_survives_restart_and_executes_once`（`approval.requested` → `approval.decided` → `approval.consumed` → `receipt`，各 1 条） |
| 4 | 同一审批不被双消费 | 成立 | `test_permissions.py::test_approval_consumed_once`（条件 UPDATE）、`test_m2_acceptance.py` 断言 `approval.consumed == 1` 且 `tool.intent == 1` |
| 5 | 跨 Task / 跨进程 / 父子目录不重复写 | 成立 | `test_leases.py::test_parent_child_directories_conflict`、`test_job_claims.py::test_live_worker_loses_claim_to_another_process`、`test_m2_acceptance.py::test_two_workers_claim_once`（4 个真子进程，只 1 个认领、1 次写入） |
| 6 | 目标变更后旧授权失效 | 成立 | `test_run_control.py::test_goal_change_fences_pending_and_prepared_calls`；CLI 实测见 §6.6 |
| 7 | 暂停/等待状态重启可恢复 | 成立 | `test_run_control.py::test_resume_never_repeats_completed_calls`、`test_m2_acceptance.py::test_job_restart_preserves_pending_approval` |
| 8 | 已完成调用不重发 | 成立 | `test_run_control.py::test_resume_recovers_when_cursor_lags_behind_events`（游标落后一步仍不重跑） |
| 9 | unknown 阻断所有受控写入口 | 成立 | `test_recovery.py` 四故障点 + `test_m2_acceptance.py` 实测：`recover` 报 `无法核实 ['c1']` 之后下一次写入 `state=failed`，文件未生成（§6.7） |
| 10 | 旧日志仍可回放 | 成立 | `test_acceptance.py` 的 replay 校验、`test_debug_regressions.py`；CLI 每次 `run` 都附带 `replay: 通过` |
| 11 | 四目标证据状态透明 | **部分** | macOS arm64 实测通过；Windows（x64/arm64）、macOS x64 **未运行**，见 §8 |

### 2.4 多个 worker + 一棵目录：串行而不是瘫痪（压测）

压测脚本（`/tmp/stress_workers2.py`，不属于仓库）造 10 个排队作业，全部指向**同一棵**
工作区目录，同时起 6 个 `worker`：

```text
worker 退出码: [0, 0, 0, 0, 0, 0]
带 contended 的 worker: 5
全部 10 个 Run：各 1 次写入、都 succeeded、文件都在: True
```

第一版（未修）的实测是：抢不到租约的 worker 把这些作业**挂起**成"等人决定"，
队列里 8 条正常工作变成 8 条 `waiting`，退出码还是 1——把"排队"误报成"故障"。
修法见 `docs/REVIEW-PRA.md` §4.6.7。回归测试：
`test_concurrent_workers_serialize_on_one_directory`。

### 2.5 审批复用的边界（探针实测）

一张批准到底能覆盖多少东西？用 `/tmp` 探针在同一个 Run 上造四种"看起来很像"的变体，
看它们能不能蹭到已批准的那张：

| 变体 | 结果 |
|---|---|
| 改写入内容（同名文件、同 `call_id`） | 新开一张待批，**不复用** |
| 换 `call_id` | 新开一张待批 |
| 换 `attempt_id` | 新开一张待批 |
| 换目标文件 | 新开一张待批 |
| 原动作执行第二次 | `DispatchError: 这次调用已经派发过：call_id=c1（state=done）` |

也就是说：批准覆盖的是"这一次调用、这一份参数、这个目标、这个 revision/绑定"，
**只**去掉了 owner/epoch 这一个维度。改目标、改参数仍然要重新批（验收项 6）。

## 3. Task 6 专项：作业循环

### 3.1 接口与状态

```
jobs.state: pending → claimed → done | cancelled
                  ↘ waiting   （停下等人：等审批 / 已暂停 / 待核查）
```

- `run_worker_once(store, owner, *, stack, model, …) -> bool`：认领一个作业并推进；
- `Worker.run_once()` / `Worker.drain(max_jobs)`：循环入口，`cli worker [--once]` 用它；
- `park(claim, reason)`：把作业挂起（**不是**放回 `pending`）；
- `claim_next()`：`pending`，或 `claimed` 但持有者已死/已过期；
- `claim(job_id)`：默认**拒绝**从活着的 owner 手里接管，接管要显式
  `take_over_live=True`。

### 3.2 为什么挂起而不是放回队列

把"等审批"的作业放回 `pending`，一个轮询 worker 会立刻再认领、再停在同一个地方。
每轮 `claim_epoch +1`，而审批（修订前）绑在 epoch 上——**用户刚批的审批会被下一轮
作废**。`waiting` 让"该自动跑的"与"等人决定的"在数据里分开，`claim_next` 只看前者。
对应的测试：`test_m2_acceptance.py::test_worker_parks_waiting_run_instead_of_respinning`。

### 3.3 并发认领：判定与写入必须在同一个写事务里

原来的 `claim_next` 是"先读候选行 → 再 `claim()`"。B 在旧快照上看到 `pending`，
去 `claim` 时却从 A 刚提交的 `claim_epoch=1` 上接着 `+1`——两个进程同时持有同一个
Run 的 claim，各自都通过 `assert_claim`。四个 worker 抢一个作业时稳定复现：

```
rc=1 ids=['run_...'] err=['正由另一个实例持有…', '结算未生效：挂起被拒绝…']
rc=0 ids=['run_...'] err=[]          ← 两个进程都认为自己认领成功
```

修法：`claim_next` 用 `with self._log.transaction()`（`BEGIN IMMEDIATE`）把"扫描 +
判定 + 认领"包成一次写事务；`claim()` 里再核一次"owner 是否还活着"。
测试：`test_two_workers_claim_once`（4 真子进程）、
`test_claim_next_takes_over_dead_owner_without_waiting_for_ttl`。

### 3.4 心跳：阻塞调用期间谁来自证活着

默认 TTL/deadline 30 秒，而真实模型调用经常更久。宿主线程阻塞时没法自证活着，
所以心跳在**另一个线程**里跑（`HeartbeatLoop`），用**另一条 SQLite 连接**
（`_SideChannel`，`cross_thread=True`），续的东西与主循环 `heartbeat_once`
完全相同（同一个 `renew_claim`）。

正向与反例都用真实短 TTL 测（`test_heartbeat_loop_renews_while_the_host_thread_blocks`）：

```
有心跳（TTL 150ms / 心跳 40ms，模型阻塞 600ms）：另一条连接抢不走 → 抢走=False，beats≥2
无心跳（同上）：另一条连接在模型调用期间就抢走了 → 抢走=True，worker.errors 非空
```

## 4. 本轮改掉的契约：审批绑动作，不绑进程

**问题**（实测复现，不是推演）：

```
$ tianshu run start --task-id T          # 覆盖写入 → 需要审批
退出码 3，state=waiting_approval，待批 ['appr_x']
$ tianshu approvals approve appr_x
$ tianshu run resume --run-id R          # 另一个进程 → 新的 claim_epoch
退出码 3，state=waiting_approval，待批 ['appr_x', 'appr_y']   ← 又批一次，文件没变
```

旧契约里 `Approval.digest` 覆盖了 `job_id / owner_instance_id / claim_epoch`，
于是每个 `run resume`（新进程 = 新 epoch）都会让刚才的批准作废。README 写在纸面上的
主流程 `approvals approve` → `run resume` 永远走不完。

**改法**（`docs/PRD-v1.md` §5.4，提交 `e5cf172`）：两个摘要。

| 摘要 | 覆盖 | 用途 |
|---|---|---|
| `request.digest` | 全部授权字段，含 owner 与 `claim_epoch` | 派发/执行前核验"谁在什么身份下做" |
| `Approval.digest` | 动作字段（task/run/revision/lease_epoch/call/attempt/tool/tool_version/schema_hash/action/resources/args_hash/preconditions/policy/prompt/contract） | 人审查"做什么" |

执行时仍然要同时满足：当前 claim 有效且属于本实例、租约在手、Run 为 `RUNNING`、
`goal_revision/lease_epoch` 未变、`action_digest` 与审批一致、审批一次性消费。
改参数 / 改目标 / 改绑定 → 动作摘要变 → 旧审批 `superseded`（验收项 6 不变）。

回归测试：
`test_run_control.py::test_approval_survives_takeover_without_reapproval`、
`test_m2_acceptance.py::test_approval_survives_restart_and_executes_once`、
`test_job_restart_preserves_pending_approval`。

## 5. 已知限制（本轮不修，必须知道）

1. **多目录任务的相对路径**解析到"第一个可写绑定"（`AgentLoop._primary_root`）。
   绝对路径按绑定覆盖关系判，不受影响。
2. **取消不撤回已经发生的修改**：取消只保证"不再派发新的副作用"，已 `applied`
   的效果保留，需要人工 `undo`。
3. `unbilled_model_intents` 只统计"有 intent、没 result"的调用次数，**不**折算成
   预算上界；模型费用未知时 `budget_enforced=false` 如实落库。
4. **E2 生成作业**（v2）未实现：作业表只服务 E1/E3 的本地 Run。
5. `authority` 数据根与 `--db` 的逃逸防护（"换一个 `--db` 绕过已存在的目录阻断"）
   尚未实现，见 §8。
6. 完整 takeover 协议（持守卫核对未决副作用后再接管）仍只有"认领 + epoch 推进"，
   **没有**在接管前做一次 recover 语义的副作用核对。
7. `worker` 不自动继续"等人"的作业：批准之后必须由人 `run resume`（计划 §2.4 要求
   "收到 approve 不直接开始文件操作"）。
8. **"目录内修改自动放行"没有兑现**：唯一的修改工具 `modify_text` 是整文件替换，
   按删除类走审批。PRD §8.3 的工具级落表已写明这一点；在 diff 级写入工具出现之前，
   UI 不得宣称"改这个文件夹里的东西无需确认"。

## 6. 端到端演示（临时夹具，CLI 实录）

夹具：`mktemp -d` 一个空目录，`ws/` 是唯一工作区。

### 6.1 零目录对话

```text
$ tianshu task create --goal "只聊天"        # 不传 --workspace
$ tianshu run start --task-id T
run_id : run_810460232cce
state  : succeeded
outcome: noop  (ok)
效果   : none_applied（applied=0 unknown=0）
turns  : 1   tokens: 0
replay : 通过  (13 事件 / 1 次消息校验)
```

无绑定的任务不会隐式获得 cwd 权限（写工具会被 `c4.no_binding` / `dispatch.lease_lost`
拒绝）。

### 6.2 覆盖写入必须先审批

```text
$ tianshu run start --task-id T2          # 模型请求覆盖 ws/note.md
退出码 3
state= waiting_approval 待批= ['appr_9d69b2b0cf40']
文件: 原始内容                             ← 未改动
```

### 6.3 批准 → 另一个进程 resume → 恰好执行一次

```text
$ tianshu approvals approve appr_9d69b2b0cf40
$ tianshu run resume --run-id R2          # 新进程、新 claim_epoch
run_id : run_f6a51a9f43e6
state  : succeeded
outcome: succeeded  (ok)
效果   : applied（applied=1 unknown=0）
replay : 通过  (35 事件 / 2 次消息校验)
文件: Agent 改过的
剩余待批: []
```

### 6.4 两个 worker 抢同一个排队作业

```text
worker1: {"run_ids":["run_3575ee6bc9d4"],"idle":false,"beats":1,"errors":[]}
worker2: {"run_ids":[],"idle":true,"beats":0,"errors":[]}
$ tianshu run status --run-id R4
state= succeeded job= done epoch= 1
```

### 6.5 暂停之后必须显式 resume；worker 不许替用户取消暂停

```text
$ tianshu run pause --run-id R5
已暂停：run_d4b63be17418（交还了工作区租约，resume 会重新获取）
$ tianshu worker --once
{"run_ids":["run_d4b63be17418"],"idle":false,"beats":0,"errors":[]}   ← 认领后挂起，不驱动
run 状态: paused waiting
$ tianshu run resume --run-id R5
state  : succeeded
```

### 6.6 改目标 → 旧 Run 立即失效

```text
$ tianshu task revise --task-id T6 --goal "目标二" --workspace ws
task_33bf6d350abe 已改：goal_revision=2 lease_epoch=2
旧目标的 Run 已被 fence：它的待批请求已作废，不能继续执行
$ tianshu run resume --run-id R6
错误：run 已被 fence：run_03a4f95f088d
```

### 6.7 unknown 效果阻断后续写入

```text
$ TIANSHU_FAILPOINT=after_write tianshu run --workspace ws --task "写一半"
崩溃退出码=77
崩溃后 new.md: Agent 写的
$ printf '被人改成第三种内容\n' > ws/new.md
$ tianshu recover --run-id R7 --workspace ws
已确认执行 : （无）
确认未执行 : （无）
无法核实   : ['c1']
$ tianshu run --workspace ws --task "再写一次"
state  : failed
outcome: failed  (workspace 已被阻断，拒绝写入：workspace 存在未核实的副作用…)
other.md 存在: no
```

## 7. 本轮发现并修复的缺陷

这三条都是"没人走过这条路"才留下的，全部由端到端用例暴露：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 审批绑 `claim_epoch`，跨进程 `approve → resume` 永远重批 | 主流程走不完（§4） | 两个摘要（`e5cf172`） |
| worker 驱动的 Run 没有写前快照清单 | 崩溃后 `recover` 只能 `unresolved` 并**永久**阻断目录 | 清单由 `_drive` 建（幂等） |
| 改过文件却记 `noop` | `run.end=noop` / `run.finish=succeeded` / `effect_state=applied` 三处矛盾 | noop 只在 `effect_state=none_applied` 时成立 |
| `claim_next` 先读后写（判定与认领不在同一事务） | 两个 worker 同时持有同一 Run 的 claim | `BEGIN IMMEDIATE` 包住判定+认领 |
| `_finish_locked` 只收 `pending/claimed` 作业 | 取消一个"等审批"的 Run 会留下永不结束的作业 | 把 `waiting` 一起收 |
| 只读绑定被拒时报"租约不覆盖" | 拒绝理由说的是技术细节，不是真实原因 | 报 `c4.readonly_binding` |
| 心跳把 `database is locked` 当成丢 claim | 一次正常的慢写入会让 owner 被误判不活并遭接管 | 只对 locked/busy 重试（≤3 次） |
| 抢不到租约就 `park` | 队列里的正常工作被记成"等人决定"，一队全停 | 交还成 `pending` + `drain` 停下 |

细节与复现命令见 `docs/REVIEW-PRA.md` §4.6。

## 8. 未验证 / 未实现（不得当作已解决）

| 项 | 状态 |
|---|---|
| Windows x64 / arm64 | **未运行**。`msvcrt.locking`、`OpenProcess/GetProcessTimes`、`\\?\` 前缀路径（绕过规范化）只有实现，没有跑过 |
| macOS x64 | **未运行**（只有 arm64） |
| 真实断电 / 掉电后的 fsync 语义 | **未验证**。`os._exit` 只证明进程中断，不证明页缓存已落盘 |
| 四目标原生测试矩阵 | 未建立 |
| `authority` 数据根逃逸（换 `--db` 绕过目录阻断） | **未实现**（计划 §2.2 要求的那条防护） |
| 完整 takeover（持守卫核对未决副作用再接管） | 未实现 |
| E2 生成作业 | 不做（v2） |
| Rust Host / 认证 IPC / OS 级隔离 | 桌面接线，未做；不能把 Python 侧的检查称作"已交付的物理沙箱" |
| 真实模型端点压测 | 本轮全部用 scripted 模型；真实端点下的心跳行为未压测 |