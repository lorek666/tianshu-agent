# PR #7 独立复审报告（M2 · PR A：Task/Run 与租约）

- 复审对象：PR #7 `feat(m2): Task/Run fencing + 目录身份与跨进程租约`
- 复审基线：`7f77597`（合并提交，88 tests passed）
- 复审方式：不读作者自查结论，按计划 `docs/superpowers/plans/2026-11-08-m2-permissions-tasks.md`
  §2.1/§2.2/§3 的 Task 1–2 验收项逐条对照，并写探针脚本与真子进程复现
- 复审结论：**PR #7 不能作为 PR A 的验收**。找到 13 项缺陷（其中 5 项高危、
  3 项直接指向"会写坏文件"），本分支修掉 12 项，第 9 项只交付了锁本身；
  另有 8 项能力缺失与未验证边界，见 §4。
- 修复分支：`fix/m2-pr-a-review`（PR #8）；补完计划 Task 2 剩余部分：`feat/m2-job-claims`（PR #9）
  最终 `154 passed`，`ruff check .` 全绿

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
| F2 | **高（阻塞 PR B）** | 事务不可组合：`EventLog.append_many`、`TaskStore._tx`、`LeaseStore.*` 各自 `BEGIN IMMEDIATE`，组合即嵌套报错。PR B 必须"审批消费 + grant + 租约 + intent + 事件"一次提交，这个形状根本拼不出来 | 已修：`EventLog.transaction()`（已在事务中则复用，不重复 BEGIN）+ 各 store 走同一入口；`tests/test_transactions.py` 7 个用例 |
| F3 | **高** | 写守卫只比 `run_id`：任何进程拿着同一 `run_id` 就能通过 `assert_held`，`release` 能删掉别人的租约（P9）。pid 会被复用，"同 pid"也不等于"同进程" | 已修：`ProcessIdentity{instance_id, pid, started_at}`，租约行记 `owner_instance_id`；`assert_held`/`renew`/`release` 都要求实例匹配；`test_foreign_instance_cannot_pass_guard` |
| F4 | **高** | fence/收尾不释放租约：Run 结束或被 fence 后租约行留到 TTL 到期，同一 Task 的新 Run 被自己上一个 Run 挡住（P10/P11） | 已修：`LeaseStore.release_run()`，`finish_run`/`_fence_run` 在同一事务内释放；`test_revise_goal_fences_running_run_and_frees_workspace` |
| F5 | **高（契约）** | Task/Run 根本不是事件的投影：状态直接写表，一条事件都没有（P4/P8）。计划 §Architecture 写的是"事件仍是事实源，Task/Run/Approval/Grant/Lease 为同库事务更新的可重建投影"，回放因此无法解释某 Run 为什么被 fence | 已修：`task.created` / `task.revised` / `run.start` / `run.finish` / `run.fenced` 五类事件与投影同事务提交；`rebuild_from_events()` + 重建测试 |
| F6 | 中 | `finish_run` 接受任意 `outcome`/`effect_state` 并把 `state` 写成任意值（P3），Run 永远不是终态，同一 Task 又能开新 Run | 已修：白名单校验；`test_unknown_outcome_or_effect_state_is_rejected` |
| F7 | 中 | store 依赖外部连接已设 `row_factory`（P5），换一个调用方就 `TypeError` | 已修：store 自行设置 |
| F8 | 中 | 测试证据不成立：所谓"两个进程"是同一进程两条连接，过期/接管靠 `sleep(0.05)` 猜时序；计划明确要求 subprocess + barrier，不用 sleep | 已修：`test_two_processes_one_resource`、`test_live_process_blocks_another_process` 用真子进程 + stdout 屏障；TTL 用注入时钟推进 |
| F9 | 中（新增能力） | 缺 `execution_guard`（计划 Task 2 明确要求）。没有它，`assert_held` 是"读一下"，读完到真正写之间可被接管，文档里"保证过期后不会再写"是过度承诺 | **部分修复**：`execution_guard()`（POSIX flock / Windows `msvcrt.locking`）+ 跨进程互斥、超时、硬退后 OS 释放三个用例已就位；**未强制**"所有 epoch 变更必须在 guard 内"，见 §4 第 3 条 |
| F10 | 低 | 默认 TTL 60000ms 与计划 30000ms 不一致；`renew` 失败静默返回 0，心跳失败不会被发现 | 已修：`DEFAULT_TTL_MS=30_000`、`HEARTBEAT_MS=5_000`；`renew` 续不满即抛 `LeaseLost` |
| F11 | 低 | `create_task` 的目录只有路径字符串，没有 D6 决定的 `mode`/`snapshot_policy`；空目录任务与"读 A 写 B"无法表达 | 已修（数据模型）：`WorkspaceBinding{root_path, mode, snapshot_policy}`，旧字符串自动升级；策略判定本身属 PR B |
| F12 | 低 | `revise_goal` 不检查 Task 状态、空 goal 可写入 | 已修 |
| F13 | 中 | store 在已打开的事务中构造时，`executescript` 会隐式 COMMIT 掉外层事务，等于静默提交半份状态 | 已修：构造时显式拒绝 |

## 3. 修复后验证

```bash
cd /Volumes/Date/天枢Agent
uv sync --python 3.12
uv run pytest        # 117 passed（修复前 88）
uv run ruff check .  # All checks passed!
```

新增/重写用例：

- `tests/test_transactions.py`（新，7 项）：投影+事件一起回滚、跨 store 组合提交、三层嵌套复用外层事务、
  内层失败回滚外层、另一连接无法插进已开始的事务
- `tests/test_tasks.py`（重写，21 项）：0 目录合法、binding 持久化、未知 outcome 拒绝、
  `assert_current`/`assert_write_allowed`、5 类事件序列、**清空投影后按事件重建逐字段相等**
- `tests/test_leases.py`（重写，26 项）：父子目录双向冲突、符号链接别名冲突、
  注入时钟推进 TTL、外来实例不能过守卫/续租/释放、真子进程互斥、
  执行守卫跨进程互斥 + `os._exit` 后释放、旧库缺列自动迁移

## 4. 未修复 / 未验证（不得当作已解决）

PR #7 计划覆盖的 Task 1–2 交付物当时缺以下部分。**其中 1、2、3、7 已在 PR #9 补齐**
（见 §4.1），Task 3 见 §4.2，Task 4 见 §4.3。下列 4、6、8 仍未做：

4. `authority` 数据根与 `--db` 逃逸防护（计划 `test_changed_db_cannot_escape_authority`）。
5. ~~`schemas/m2/task.json`、`run.json`、`job.json`~~ — 已在 PR #10 连同 approval/grant 一起补上。
6. 计划 §2.2 的完整 takeover 协议：TTL 到期不等于旧 worker 已停；接管必须先拿 guard、
   核实 owner、核对未决副作用。目前只做到"过期 / 进程消失 / pid 被复用即可接管"，
   且**接管本身仍不持守卫**（`LeaseStore.acquire` 不知道 data_root）。
8. 未验证平台与边界：Windows 原生进程（`msvcrt.locking`、`OpenProcess` 分支一条都没跑过）、
   真实断电/fsync、租约与真实文件写入的交叉（dispatch 尚未存在）、跨用户/跨会话的
   `%TEMP%` 差异、长任务里 TTL 与心跳的实际配合（心跳循环在 Task 6）。

### 4.1 PR #9 补的部分

| 原缺口 | 现状 |
|---|---|
| 1. `jobs.py` / `JobClaim` / claim_epoch | `jobs.py`：`enqueue`/`claim_next`/`heartbeat`/`finish`/`requeue`，全部按 `(owner_instance_id, claim_epoch)` 条件更新；一个 Run 只允许一个未完成作业（唯一索引兜底）。缺 claim_epoch 时"活着但已过期"的旧 worker 拦不住，这是唯一能拦住它的机制 |
| 2. `RunCursor` / `save_cursor` / `commit_model_turn` | `tasks.py`：游标与模型结果都必须带 `JobClaim`，写入时同事务核验 claim + run 未被 fence；`commit_model_turn` 对同 `turn_id` 幂等（重启不重复计费），同 id 不同内容直接报错 |
| 3. `execution_guard` 强制使用 | `begin_execution()`：守卫内复核 run/epoch/租约 → 续租 → 写 `execution.started` → yield，with 块即"已获准执行"的窗口；`revise_goal()` 拿**同一把**守卫改 revision/epoch。两边都拿锁才有意义，只让一方拿等于没锁 |
| 7. PID 复用检测 | `pid_started_at()`（Linux `/proc`、macOS `ps etime`、Windows `GetProcessTimes`），判定单向：只有"进程启动晚于租约获取"才算复用，宁可晚接管也不误接管（误接管=双写）。Windows 存活探测也从"恒为 True"改成 `OpenProcess`+`WaitForSingleObject` |

### 4.2 PR #10（PR B · Task 3）补的部分

PR B 的第一步：**判断与记录**，还不碰文件。

| 内容 | 说明 |
|---|---|
| `canonical.py` | RFC 8785（JCS）规范化：键按 **UTF-16 码元**排序（`sort_keys=True` 按码点，星平面字符会排错）、控制字符转义、非 ASCII 原样保留；**浮点与超过 2^53-1 的整数直接拒绝**——手写 ECMAScript 数字序列化出错比不做更危险，摘要会悄悄算错 |
| `permissions.py` | `PolicyEngine.classify`：目录内读/新建自动、覆盖/删除/目录外需审批、只读绑定拒绝、0 目录拒绝、未支持动作拒绝（不是给一个无法履行的"需要提权"） |
| `ActionRequest.digest()` | 绑定 19 个授权字段（含 claim_epoch / owner_instance_id / preconditions），改任何一个摘要必变 |
| `ApprovalStore` | pending→approved/rejected/expired/revoked/superseded；`HumanIdentity` 只能由 `local_cli()` 构造，模型参数里的同名结构被拒；批准一次只能消费一次 |
| `GrantStore` | 一次性票据，默认 30 秒且不得超过租约剩余期限；消费按 `(digest, subject, 剩余次数, TTL)` 条件更新 |
| `schemas/m2/*.json` | Task/Run/JobClaim/Approval/Grant 五份；配一个"schema required 必须等于 dataclass 必填字段"的对照测试，改了代码忘改契约会红 |

### 4.3 PR #11（PR B · Task 4）补的部分

派发层是"权限与审批闭环"真正合上的那一步：判断层（Task 3）只回答"该不该批"，
派发层回答"现在能不能真的动文件，凭什么"。

| 内容 | 说明 |
|---|---|
| `dispatcher.py` 阶段一 | **一个 SQLite 提交**里做完：当前状态复核（Run 未被 fence / revision / epoch / 租约 / claim）→ 审批条件 UPDATE 消费（`state='approved' AND expires_at > now`，rowcount=0 即失败）→ 签发票据（`issuer` 为 `approval:<decided_by>` 或 `policy:auto`，且不得超过租约剩余期限）→ 消费票据 → `tool.intent` → 派发记录。四件事同生同死 |
| `dispatcher.py` 阶段二 | `begin_execution` 拿守卫后再核一遍（claim / grant 来源与摘要 / 剩余次数 / TTL / revision-epoch / 目标身份 / 写前状态），记 `execution.started`，然后才打开文件。撤销与执行抢同一把守卫：撤销先到 → `approval.revoked` + 派发记录 `revoked` + 字节不变；执行先到 → 撤销明确报错并发"请用 recover 核查"，不假装成功 |
| `FileExecutor` | **不接受** `approved=True` 这类自述字段。票据只是身份，执行器自己回库核对 8 项：claim 当前 owner、Run 未 fence、目录租约覆盖目标、派发记录必须是 `intent` 且摘要/grant/epoch/owner 都对、grant 已消费且未过期、覆盖/删除必须有已消费的审批、目标 `(st_dev,st_ino)` 未被换掉且在可写绑定内、写前状态未变 |
| `delete_file` / `restore_file` | `delete_file` 对模型暴露（审批 + preimage + 不支持递归）；`restore_file` **不暴露**，只给 undo 用。删除的 `after_state` 是显式的 `absent`，凭据链路（`build_receipt` / `verify_receipts`）已同步 |
| 所有写入口收口 | `tools.execute()` 对有副作用的工具一律要求已核验票据；**全库唯一一处**放行是 `loop.run()` 的 `allow_legacy_unauthenticated_write`（M0 遗留同步循环；**已在 §4.4 删除**）。`SnapshotStore.undo` 的直写分支同样改为必须显式 `allow_direct_write`，M2 的 CLI 走 `apply` 回调 |
| `recovery` 服从资源占用 | 待核对路径（或它所在工作区）若被**另一个活着的 Run** 持租约，记 `unresolved` 并阻断，不做实时观测的猜测 |
| 执行中途崩溃 | 结论只能是 `unknown`，并且立刻写 `recovery.blocked` 挡住该工作区后续写入——"确定没写"和"不知道写没写"是两件事 |
| CLI | `undo` 两步：不带 `--approve` 只预演（退出码 3、不写、不登记审批）；带 `--approve` 才创建控制 Run/JobClaim、逐动作走派发。`approvals list/approve/reject/revoke` 补上人工侧。撤销中途失败记 `undo.failed` + 控制 Run `partial` |

测试：`tests/test_dispatch.py` 27 例，全量 **273 passed**，`ruff check .` 通过。

仍未做（**不得当作已解决**）：

- **`loop.run()` 仍未接 Task/Run/Job 生命周期**，写入靠上面那一处显式开关放行。
  也就是说"未经派发的写入"在这条遗留路径上仍然存在，只是必须由调用方显式打开。
  **已在 §4.4（PR #12）解决**：开关删除，`loop.run()` 改走完整生命周期。
- **目录外写入**：策略层按契约给 `approve`，但执行侧明确拒绝
  （`c4.outside_workspace_unsupported`）——M2 的证据边界按 workspace 根存
  manifest，没有"目录外单文件"的 preimage 归属。批准了也不会执行，这一点必须
  让用户看得见，不能假装支持。
- **`undo --approve` 把"人敲 --approve"当批准**：审批摘要绑定 `claim_epoch`，
  跨进程两次调用必然换 epoch，所以"先另开终端批、再回来跑 undo"这条路走不通；
  本 PR 选的是"同一条命令里先预演、再带 --approve 执行"。这是 UX 取舍，不是安全
  边界（`HumanIdentity.local_cli()` 本来就只声称"模型构造不出来"）。
- **等待人工批准时的租约心跳循环**仍未实现（Task 6 的 worker）。当前
  WAITING_APPROVAL 期间谁在续租，还没有真实代码承担。
- §4 第 4、6、8 条（authority `--db` 逃逸、完整 takeover 持守卫、Windows/断电证据）仍未做；
  本 PR 只额外补了 SQLite 有界 `busy_timeout` 与跨连接并发派发的一次性消费证据。

### 4.4 PR #12（PR C · Task 5）补的部分

Task 4 只让"写入必须派发"，但派发的调用方还是 M0 那个一次跑完的同步循环：
它没有 cursor，停下就是结束，重启只能从头再问模型。Task 5 把这条路径换掉了。

| 内容 | 说明 |
|---|---|
| 控制请求与生效状态分离 | Run 投影里多了 `control_requested ∈ {pause, cancel}`，与 `state` 两回事。执行中的 Run 只记录请求，执行器在**安全点**确认；确认请求与清空请求是同一次提交，所以恢复之后不会把同一条暂停请求再执行一遍（否则"点了继续"会立刻又停） |
| 安全点的含义 | 暂停落在一次文件写入中间时，那一次照常完成并如实计入 `effect_state`，之后不再开始新的。`RUNNING` 之外的 Run（`PAUSED` / `WAITING_APPROVAL`）留着有效租约和 claim 也不行——`begin_execution` 与 `_authority` 都只认 `RUNNING`，绕过主循环直接派发同样被拒（`dispatch.not_running`） |
| 持久游标 | `run_cursors` 保存已提交消息、待执行提案、已完成调用、审批引用与预算账本；`model.result` + `run_turns` + cursor 在**同一个事务**里提交。重启只从游标继续，不重问模型——重问既重复计费又可能得到不同答案。`run.end` 里新增 `unbilled_model_intents`：有 intent 没 result 的模型调用次数，"可能花了但没记账"必须留痕，不能按 0 结算 |
| 恢复同一次执行 | `resume_run` 属同一个 Run：预算不清空、已完成的调用不重派、失败重试要开新 Run。三种情况分开处理：自己仍是 owner → 复用同一 claim（epoch 不变，批准继续有效）；别人还活着 → 拒绝抢占；无人认领（暂停/进程死亡/租约过期）→ 重新认领，epoch +1 使旧批准作废 |
| 崩溃窗口 | 工具结果由派发层提交、游标由主循环提交，中间有真实窗口。恢复时**以事件为准**：`dispatch_record` 已完成 → 从 `tool.result` 补回消息，不再派发；仍是 `intent`/`executing` → 转 `RECONCILING` 并要求先 `recover`。拿游标当唯一事实就会重复执行一次写入 |
| 改目标 | `revise_task(goal?, bindings?)` 在同一把守卫里推进 `goal_revision` 与 `lease_epoch`，fence 旧 Run，一并作废它的待批审批。绑定变更只对之后的新 Run 生效，不扩张旧授权。旧 Run 的历史不改写 |
| 写入口 | `tools.execute()` 的 `allow_legacy_unauthenticated_write` **已删除**：会改文件的工具在那条路上只会收到 `ToolNotAllowed`。`loop.run()` 保留为兼容入口，但不再是捷径——它建 Task、开 Run、拿租约、认领作业、走派发 |
| 装配 | 新增 `AgentLoop` 与 `build_stack`：CLI、测试、`run()` 共用同一套 store 装配，避免"CLI 能用、库函数不能"的绕过点。`build_stack` 的 `clock`/`identity`/`dispatcher_cls` 注入点只为测试 TTL 与接管 |

测试：`tests/test_run_control.py` 21 例；全量 **300 passed**，`ruff check .` 通过。

**这一轮没有完成作者之外的人复审。** 曾尝试让独立的复审 agent 通读
`main...HEAD` 的完整 diff，但它没有在合并前返回报告；因此本 PR 是作者自查后
合并的。按 §5 的建议，这属于流程缺口，而不是"已经复核过"。自查中修掉的 5 个
问题写在提交记录里（`fix(m2): 崩溃后的 Run 必须能被接管`、
`fix(m2): 派发阶段被拒时主循环必须补写 tool.result`、
`fix(m2): 重建投影时清掉 Run 上的控制请求`、
`fix(m2): 读操作在游标落后时被重复记录`、
`test(m2): 把 intent ⊆ result 补完`），但"自查通过"不等于"独立复核通过"。

仍未做（**不得当作已解决**）：

- **等待审批/暂停期间的心跳循环**没有自动代码承担：`heartbeat_once` 提供能力并由
  测试显式调用，周期循环属于 Task 6 的 worker。也就是说当前"一直挂着等人批"
  需要外部反复调用，否则 30 秒后租约过期、接管者拿到新 epoch、要重新批一次。
- **多目录任务的相对路径解析规则**：本阶段把相对路径解析到"第一个可写绑定"，
  绝对路径按绑定覆盖关系判。真正按工具参数选目录（每个工具声明目标 workspace）
  是后续工作；现在这一点写在 `AgentLoop._target_for` 的注释里。
- **取消不撤回已发生的修改**：`effect_state` 如实记录，`run.end` 里 `error`
  说明"已停止派发新的副作用"。没有"回滚到最后一致状态"的自动行为，回滚仍靠
  `undo`（本身是受审批的写操作）。
- **`run.end` 的 `unbilled_model_intents` 目前只记录不结算**：预算账本没有把
  "可能发生的调用"折算成上界，所以"预算未超"这句话在有中断时是近似值。
- **Windows 原生、真实断电、`authority --db` 逃逸防护、完整 takeover 持守卫**
  仍未做（见 §4 第 4、6、8 条），本 PR 未新增这些平台的证据。
- **E2 生成作业**仍未实现，`queued/running` 两个状态目前只服务 E1/E3。

## 5. 对流程的意见

- M2 剩余 PR 建议保持"作者之外的人复审后再合并"。本轮 6 个缺陷全部属于
  "写的人看不出、读的人十分钟看得出"的类型。
- 计划里的验收项是**契约**，不是建议。PR #7 漏掉的不是边角，是计划正文用加粗写明的那几条
  （父子目录、跨进程证据、投影可重建、execution_guard）。合并前应逐条对照打勾，
  而不是看测试数量。
- "测试通过"不等于"验收项通过"：88 个用例里没有一个真子进程，也没有一个 sleep 之外的
  时间控制手段，所以"跨进程"这四个字在 PR #7 里没有证据。