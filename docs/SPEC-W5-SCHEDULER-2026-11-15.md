# SPEC · W5 调度/定时任务（确定性最小调度器）

- 日期：2026-11-15 · 基线：origin/main@0c5bb71 · 分支：`feat/scheduler-tasks`
- 账本行（`docs/ROADMAP-COMPLETE-AGENT-2026-11-14.md` 第 17 行）：「调度/定时任务 | 缺口出处：全库 0 匹配 | 契约影响：新事件族？C3 评估 | 冲突面 daemon.py（#146 后续会碰）| 外部阻塞无」
- 性质：调度器**新建任务/Run 但不执行任何副作用**，授权面不变——非高危改动；daemon.py 是热文件但 #146/#159 已合并落地、无在途占用，本 PR 改动集中于 `_driver_loop` 一个装配点 + 一个 tick 调用（见 §5）。

## 0. 实现范围判定（先给结论）

| 缺口 | 判定 | 一句话理由 |
|---|---|---|
| **interval 调度（到期 → 自动创建任务并入队 Run）** | **本 PR 实现** | 纯增量：新冷区模块 + daemon 一处接线；复用既有 `task.create` / `run.start` / enqueue 路径，零新事件族、零 RPC 方法、零 schemas/ 变更 |
| **cron 表达式** | **不做，留未决** | cron 解析要么引第三方依赖（违反零运行时依赖），要么自写解析器（v1 不该背这个正确性负担）；interval 起步已覆盖「每 N 秒跑一次」的最小场景 |
| **RPC / UI 可观测性** | **不做，留未决** | 加 RPC 方法要动 M5-desktop.md 契约表；UI 可观测性（schedule 列表/状态页）不是 v1 缺口分析的必须项。v1 只走 CLI |
| **schedule 生命周期事件（created/fired/missed/paused）** | **只交契约分析，不实现** | 任何新事件类型都要动 `schemas/c3/event.json` + `event_schema.py` 封闭集（三处同改纪律）；v1 的触发可见性已由 `task.created`（含 `run.start` 等既有事件）承载。逐条评估见 [CONTRACT-W5-SCHEDULER-2026-11-15.md](CONTRACT-W5-SCHEDULER-2026-11-15.md)（独立 `docs(contract):` commit） |
| **missed fire 的补偿计数/补跑 N 次** | **不做** | 追赶策略定为「补发一次并从当前时间重排」（§4）；按落下的 interval 数补跑 N 次会一次性制造 N 个 Run，既烧钱又不可预期，v1 不做 |

## 1. 现状代码事实（读到的，不是猜的）

1. **全库 0 处调度/定时实现**：`grep -rn "schedule\|cron" src/tianshu/` 无业务命中（README/PRD 里的「调度」均指子代理作业调度，非定时任务）。
2. **模块自建表先例**：`budget.py:84`（`SCHEMA`，`BudgetLedger.__init__` 里 `self._conn = log._conn` + `executescript(SCHEMA)`，248–250 行）与 `dispatcher.py`（`DispatchStore.__init__` 里 `self._conn = log.conn` + `executescript(SCHEMA)`，558–560 行）——**表建在事件库同一个 SQLite 文件、同一条连接上**，因此能与事件/状态同事务提交。调度器沿用此模式。
3. **事务可组合**：`events.py:398` `EventLog.transaction()`——已在事务中时不重复 `BEGIN`，复用外层事务；`TaskStore.create_task`（`tasks.py:660` 起）内部已用 `self._log.transaction()` 把任务行与 `task.created` 事件绑成同一次提交。外层再包一层 `transaction()` 即可把「任务创建 + Run 入队 + schedule 记账」做成原子。
4. **task.create 的 Control 路径**（`control.py:362` `_task_create`）：`_require_str(goal)` → workspaces 列表校验 → `_exec_policy`（`allow_exec_cmd` 必须是可执行文件绝对路径列表，布尔拒绝；`require_isolation` 必须伴随 `allow_exec_cmd`）→ `tasks.create_task(goal, workspaces, exec_policy=...)`。
5. **Run 入队路径**（`control.py:426` `_run_start`，只入队不驱动）：`dispatcher.prepare_run(task_id, execution_policy)` → 构造 payload（`task`/`out`/`limits`/`tools`，工具清单用 `_exposed_tool_ids(execution_policy)`）→ `tasks.start_run(...)` → `claims.enqueue(run.run_id)`。注释明确「认领者（daemon 驱动线程）负责 created → queued → running」；租约由 worker 的 drive 路径获取（`loop.py:872`），入队侧不碰。**没有传 `execution_policy` 时 control 默认 `EFFECTS`**。
6. **idem_key 核对（如实记录）**：`events.py` 的 `events` 表有 `idem_key` 列（167 行）且 `EventLog.append` 只是**原样存储**（442–492 行），**没有 UNIQUE 约束、没有任何去重逻辑**；`task.create` / `create_task` / `start_run` 全路径都不使用 idem_key 去重。全库对 idem_key 的既有用法（loop/dispatcher/recovery/undo）都是「把业务幂等键写进事件供审计」，不是「靠它防重复」。**结论：v1 的触发幂等不靠事件层 idem_key，靠「同一事务记账 + 单写者」**（§4.3），如实写进本文档，不假装事件层有原子去重。
7. **daemon 结构**：`daemon.py:620/628` `control_log` 与 `driver_log` 是指向同一个 `events.db`（WAL）的两条线程专用连接；`_driver_loop`（1251 行）在驱动线程运行，循环体内先消费 resume 请求，再 `worker.run_once()`，空闲时 `stop.wait(driver_interval_s)`。`Worker` 就是在 `_driver_loop` 开头装配的（1264 行）——调度器照此装配。
8. **`AgentLoop.start` 不适合调度用**：它「新建 Run 并**立刻驱动**」（`loop.py:781`），同步跑完整个循环；调度 tick 若走它会占死驱动线程。正确路径是 `_run_start` 的「只入队」形状，由同一个驱动循环的 `worker.run_once()` 认领驱动。
9. **ExecPolicy 可往返**：`execpolicy.py:253` `to_dict()` / `:271` `from_dict()`（任务级策略已这样持久化，`tasks.py:2921`）。`WorkspaceBinding.of` 接受 str | dict（`workspace.py`），`parse_bindings` 接受列表。
10. **CLI 子命令先例**：`data_cli.py` 独立模块 `add_parser(sub)` + `run(args)`，在 `cli.py` 两处接线（374 行注册、2117 行分发）。
11. **daemon 单实例**：`acquire_lock(self.root)`（daemon.py:610）保证同一数据根只有一个 daemon；驱动线程是 tick 的唯一调用方——「单写者」由进程模型保证，不靠锁表。
12. **占用面**：在途 OPEN PR #140/#141（纯 docs）、#161（maintenance/events/data_cli/net/migrate/restore*）、#162（desktop/release）逐一比对文件清单（见 PR body）；本 PR 触碰 `src/tianshu/scheduler.py`（新）、`src/tianshu/schedule_cli.py`（新）、`src/tianshu/cli.py`（2 处接线钩子）、`src/tianshu/daemon.py`（_driver_loop 装配点 + tick 调用）、`tests/test_scheduler.py`（新）+ 文档。与 #161 的 `data_cli.py` 零交集（本 PR 不改 data_cli.py）。

## 2. 数据模型：`schedules` 表

建在事件库同一个 SQLite 文件（`log.conn`），模块自建表先例（§1.2），`CREATE TABLE IF NOT EXISTS` 幂等：

| 列 | 类型 | 说明 |
|---|---|---|
| `schedule_id` | TEXT PRIMARY KEY | `sched_` + uuid4 hex[:12]（与 `task_`/`run_` 同风格） |
| `goal` | TEXT NOT NULL | 创建任务的目标文本（非空，创建时校验） |
| `workspaces_json` | TEXT NOT NULL | 绑定列表 JSON（`parse_bindings` 可解析的形状；创建时预解析校验，触发时再走 `create_task` 的正式校验） |
| `exec_policy_json` | TEXT | 任务级 ExecPolicy 的 `to_dict()` 形状；NULL = 无 exec（只读任务）。**不是** Run 级 execution_policy（v1 固定 EFFECTS，§4.2） |
| `interval_seconds` | INTEGER NOT NULL | ≥ 1（创建时校验，防 tick 风暴） |
| `next_fire_at` | INTEGER NOT NULL | 下次到期时刻（epoch 毫秒，与事件 `ts` 同单位） |
| `enabled` | INTEGER NOT NULL DEFAULT 1 | 0 = 不触发（disable 不删行，保留配置） |
| `created_at` | INTEGER NOT NULL | epoch 毫秒 |
| `last_fired_at` | INTEGER | 上次成功触发的时刻；NULL = 从未触发 |
| `last_error` | TEXT NOT NULL DEFAULT '' | 上次触发失败的原因（成功触发时清空）；v1 无 `schedule.missed` 事件（见契约分析），这列是失败可见性的最低限度替代，仅 CLI 可见 |

- **不存** out_name / execution_policy / memory_scopes / outbound_allow / mcp_servers / max_subtasks：v1 触发形状固定为「EFFECTS + 无出网 + 无 MCP + 空输出名」（§4.2），少一个可配置项就少一类要论证的授权面。
- schedules 表**不是**事件投影、不参与回放：它是「未来的意图」而非「已发生的事实」，生命周期不走事件流（这是 v1 与事件完整性纪律的边界，契约分析文档里论证了 v2 的补法）。
- 历史非法值纪律不适用（新表，无历史）；`enabled` 只接受 0/1，其他值创建/更新时报错，不存兜底值。

## 3. 模块与接口

### 3.1 `src/tianshu/scheduler.py`（新，冷区自持）

```python
class Scheduler:
    def __init__(self, log, tasks, dispatcher, claims, *, clock=None, limits=None): ...
    # log: EventLog（schedules 表建在它的连接上）
    # tasks/dispatcher/claims: 同一 Stack 的三个 store（build_stack 产物，共享同一条连接）
    # clock: Callable[[], int] 毫秒 epoch（测试注入；缺省 time.time*1000）
    # limits: Limits（run.start payload 的 max_turns/max_total_tokens；缺省 Limits()）

    # --- 管理面（CLI 用） ---
    def create(self, goal, workspaces, *, interval_seconds, exec_policy=None, now=None) -> dict
    def get(self, schedule_id) -> dict | None
    def list(self) -> list[dict]
    def set_enabled(self, schedule_id, enabled: bool) -> dict
    def remove(self, schedule_id) -> None

    # --- 触发面（daemon 驱动线程用） ---
    def tick(self) -> list[str]   # 返回本次触发产生的 run_id 列表
```

- `create` 时点做**预校验**：`parse_bindings`、`ExecPolicy(...)` 构造、`goal` 非空、`interval_seconds >= 1`——把非法 schedule 挡在创建时，而不是让它在每次触发时永久失败。
- `tick()` 语义见 §4。管理面写操作走 `log.transaction()` 短事务。

### 3.2 CLI：`src/tianshu/schedule_cli.py`（新）+ `cli.py` 两处钩子

仿 `data_cli.py` 先例（独立模块 `add_parser` + `run`，cli.py 注册/分发各一处）：

```
tianshu schedule create --goal TEXT --workspace PATH [--readonly PATH ...]
                        --interval-seconds N [--allow-exec-cmd PATH ...]
                        [--require-isolation BOUNDARY ...] [--db PATH] [--json]
tianshu schedule list   [--db PATH] [--json]
tianshu schedule show SCHEDULE_ID [--db PATH] [--json]
tianshu schedule enable SCHEDULE_ID  / disable SCHEDULE_ID / remove SCHEDULE_ID [--db PATH]
```

（修正说明：管理面只读写事件库里的 schedules 表，不需要 authority 数据根，
故没有 `--data-root`；与 `task` 子命令一致只有 `--db`，默认 `.tianshu/events.db`。）

- 语义与 `task create` / `run --task --workspace` 同口径：`--allow-exec-cmd` 绝对路径列表（布尔拒绝）、`--require-isolation` 必须伴随 `--allow-exec-cmd`（control.py `_exec_policy` 的同一规则，就地复刻并注明来源）。
- CLI 直接开 `EventLog(root/"events.db", blob_dir=root/"artifacts"/"blobs")` 构造 Scheduler 操作，用完关闭。daemon 运行中亦可管理（WAL 下短写事务并发安全，与审批落定同性质）。
- `create` 后 `next_fire_at = created_at + interval*1000`。

## 4. 触发语义

### 4.1 tick：到期检查 + 追赶

```python
def tick(self):
    now = self._clock()
    fired = []
    for row in self._due_rows(now):          # enabled=1 AND next_fire_at <= now，按 next_fire_at 排序
        try:
            run_id = self._fire(row, now)
            fired.append(run_id)
        except Exception as exc:             # 单条失败不拖垮其他 schedule、不杀死驱动循环
            self._record_error(row, now, exc)  # own 事务：last_error + next_fire_at = now + interval
    return fired
```

**追赶策略**：`_fire` 成功后 `next_fire_at = now + interval*1000`（**从当前时间重排**，不从旧 next_fire_at 连乘）。daemon 停机 10 分钟后，interval 60s 的 schedule 在启动后的第一个 tick **补发一次**，而不是补发 10 次——不需要单独的启动钩子，第一个 tick 天然就是追赶。落下的具体次数 v1 不记录（无 `schedule.missed` 事件，见契约分析）。

**失败策略**：触发抛异常（如工作区绑定在两次运行之间失效）→ 事务回滚（不产生半截任务），`last_error` 记原因、`next_fire_at` 仍从 now 重排（跳过本次，下个 interval 再试）——确定性坏 schedule 不会每个 tick（默认 50ms）刷重试风暴；失败在 daemon 日志与 `schedule list` 的 `last_error` 可见。v1 没有失败计数/熔断（未决）。

### 4.2 `_fire`：与 `task.create` + `run.start` 完全同路径

```python
with self._log.transaction():                     # 外层事务；内部所有 store 复用同一 BEGIN
    task = tasks.create_task(goal, workspaces, exec_policy=exec_policy)   # → task.created
    dispatcher.prepare_run(task.task_id, EFFECTS)                          # 派发请求记录
    payload = {"task": goal, "out": "", "limits": {...}, "tools": _exposed_tool_ids(EFFECTS)}
    run = tasks.start_run(task.task_id, payload=payload, execution_policy=EFFECTS)  # → run.start
    claims.enqueue(run.run_id)                                             # 作业入队
    # 同事务：UPDATE schedules SET last_fired_at=now, next_fire_at=now+interval, last_error=''
```

- 事件流出现的是**既有封闭集**：`task.created`、`run.start` 及后续驱动事件——零新事件族（契约影响评估见独立契约文档）。
- Run 入队后由同一个驱动循环的 `worker.run_once()` 认领驱动；租约、审批、预算、Receipt 全部走既有路径。
- v1 固定 `execution_policy=EFFECTS`（与 control `_run_start` 缺省一致）；run payload 的 `limits` 来自装配时的 `Limits`（与 control 面同一份）。

### 4.3 幂等与崩溃窗口（如实记录，不假装原子性来自 idem_key）

- **机制**：`last_fired_at`/`next_fire_at` 的更新与任务创建/入队在**同一个可组合事务**里（§1.3）。要么全部提交（任务存在且 schedule 已重排），要么全部回滚（无任务、schedule 仍到期、下个 tick 重试）。
- **核对结论**（§1.6）：事件层 `idem_key` 无 UNIQUE、无去重，task 创建路径不使用它。因此「同 idem_key 不重复建任务」在 v1 的含义是：**触发记账原子性 + 单写者**（daemon 锁保证同一数据根单 daemon；`tick` 只被驱动线程调用）使得同一次到期恰好产生一次任务。负控 NC2 把记账更新拆掉来证明它是承重墙（§6）。
- **残余崩溃窗口（如实）**：
  1. 提交成功后、进程在返回前死亡——任务已存在、schedule 已重排，重启后不会重复触发。无窗口。
  2. `BEGIN IMMEDIATE` 后进程被 `kill -9`——SQLite 回滚整个事务，同上无窗口。
  3. **真正的不原子点在 v1 之外**：若未来把 schedules 表挪到第二个数据库文件（脱离 `log.conn`），「建任务」与「记账」就回到两个事务，届时必须引入显式 idem_key 去重（事件层或表层 UNIQUE）。这是把表钉在事件库连接上的结构性理由，写进代码注释。
- **可安全重试的判定**：触发失败回滚后重试属于 `none_applied` 语义（什么都没发生），符合红线 5；不存在「结果未知」的中间态，因为记账与效果同事务。

### 4.4 授权面论证（红线 2/4/6 逐条）

- **红线 4（授权只在执行侧判定）**：调度器只做「创建 Task + 入队 Run」，自己不执行任何工具、不产出任何副作用。Run 被驱动后的每个动作与人工 `task create` 创建的 Run **走完全相同的门**：派发层 PolicyEngine 判定（`c4.auto`/`c4.overwrite`/`c4.delete`/`c4.readonly_binding`…）→ 需审批的动作由人在控制面批准（模型无审批工具）→ 派发时执行侧重判 + 审批一次性消费 + intent 落库 → 执行守卫内执行 → Receipt。调度器不是授权输入，它只是又一个任务创建者（与 RPC/CLI 的调用者同地位）；「定时」不改变任何判定点、判定者、判定侧。
- **红线 2/6（intent 先行 + Receipt）**：触发路径不产生新种类的副作用；Run 内副作用照旧走既有 intent/result/Receipt 链，可独立核对。
- **不把权限写进 system prompt / 不做字符串黑名单**：调度器没有给模型增加任何提示词内容，也没有新增任何拒绝逻辑；schedule 里的 `exec_policy` 是任务级声明（与 CLI `--allow-exec-cmd` 同一 ExecPolicy 对象、同一执行侧判定），不是调度器自己解释的字符串规则。
- **单用户假设（D2）**：schedule 由本机用户经 CLI 创建，等价于用户本人按时手动创建任务；不存在多租户下的「替别人定时」问题。

## 5. daemon 接线（改动区域声明）

`daemon.py` 只改 `_driver_loop`（约 1251–1306 行区域）：

1. **装配点**：`_driver_loop` 开头、`Worker(...)` 旁，构造 `Scheduler(self.driver_log, self.driver_stack.tasks, self.driver_stack.dispatcher, self.driver_stack.claims, limits=self.limits)`——与 Worker 同一处、同一线程（驱动线程），遵守「driver_log 连接只由驱动线程使用」的既有纪律（连接建在 serve 线程、`cross_thread=True`、用全在驱动线程；若在 `_setup` 里构造，`executescript` 建表就会从 serve 线程碰这条连接，破坏该纪律）。
2. **tick 调用**：循环体内、resume 消费之后、`worker.run_once()` 之前：

```python
try:
    scheduler.tick()
except Exception as exc:  # noqa: BLE001 - 调度器故障不带走驱动循环
    _log(f"调度器 tick 异常：{type(exc).__name__}: {exc}")
```

   tick 内部已按条 try/except（§4.1）；外层再兜一层防 tick 框架本身异常。调度器异常**不**计入 `consecutive_failures`（那是驱动循环自身的健康指标，混入调度器故障会让一个坏 schedule 停掉整个 daemon）。

3. 启动追赶：driver 线程在 `_setup`（含恢复扫描）完成后才启动，第一个 tick 即完成全部过期 schedule 的补发（§4.1），无需额外钩子。

## 6. 测试与负控

`tests/test_scheduler.py`（新文件，不触碰既有测试文件）：

**单元（注入时钟，禁 sleep）**：
1. 到期触发：interval=60s 的 schedule，clock=T0 时 tick → 恰好 1 个 task/run/job；事件流有 `task.created` 与 `run.start`；`last_fired_at=T0`、`next_fire_at=T0+60s`。
2. 未到期不触发：clock=T0-1 tick → 0 触发。
3. 间隔内不重复：触发后 clock=T0+30s tick → 仍 1 个。
4. interval 到再触发：clock=T0+61s tick → 2 个。
5. **追赶**：`next_fire_at = T0-3600s`（模拟停机 1 小时），clock=T0 首个 tick → **恰好 1 次**触发（不是 60 次），`next_fire_at=T0+interval`。
6. disabled 不触发：`enabled=0` 后任何 tick 都 0 触发；re-enable 后恢复。
7. 触发失败：绑定指向不存在的路径等 → 无任务行产生、`last_error` 记录、`next_fire_at` 从 now 重排；其余 schedule 不受影响。
8. 重启存活：关闭 EventLog → 重新打开 → 新 Scheduler `list()` 看到同一 schedule；到期则首 tick 触发（持久化 + 追赶）。
9. 管理面：create 预校验（空 goal / interval<1 / 非法绑定 / 布尔 exec 拒绝）；enable/disable/remove 幂等语义；未知 schedule_id 报错不静默。

**daemon 集成（test_scheduler.py 内自建小型 Harness，仿 test_daemon.py）**：
10. 预置一条 `next_fire_at` 已过期的 schedule → 起 daemon（`driver_interval_s=0.02`）→ **有界轮询**事件库直到 `task.created` 出现（deadline 断言，非 sleep 猜测）→ daemon 自己的 worker 认领并驱动（ScriptedModel end_turn 收尾）→ 断言 Run 进入终态且 schedule 的 `next_fire_at` 已重排。
11. daemon 重启后 schedule 仍存活且按追赶触发（停 daemon → 新到期 → 起 daemon → 触发一次）。

**负控（实跑变红 → 还原，红输出进 PR body）**：
- **NC1（到期检查承重）**：把 `tick()` 的 `_due_rows` 调用改为返回空列表（拆掉到期检查）→ 用例 1 红（预期 1 次触发，实际 0）。还原后绿。
- **NC2（同事务记账承重）**：把 `_fire` 里的 `UPDATE schedules SET last_fired_at=...` 变为 no-op（拆掉触发记账）→ 同一 clock 读数连续两次 tick → 用例 3 变体红（预期共 1 个任务，实际 2 个 = 双发）。还原后绿。

## 7. 验收清单

- [x] `uv lock --check` 0 退出
- [x] `uv run --frozen ruff check .` 0 退出
- [x] 全量 `uv run --frozen pytest` 绿（**3408 passed / 11 skipped（686.57s）**，macOS arm64；跑前抢 `/tmp/tianshu-suite-lock`、df ≥ 3GiB）
- [x] NC1/NC2 红输出摘录进 PR body（NC1 拆到期检查 9F/4P；NC2 拆同事务记账 6F/7P，主会话独立重放 NC2 亲证红→还原绿）
- [x] ROADMAP W5 行状态更新（如实档位）；KNOWLEDGE.md §8 追加（纯追加）；README 如实写「无 cron / 无 RPC / 无 UI」
- [x] 契约分析文档独立 commit，不夹带代码（`0eaed32`）
- [x] `git status` 每次提交后干净；无 `git add -A`/`- .`
- [x] CI：attempt1（run43693131633）Windows shard 0 红 = 新测试 `_terminal_run` 谓词未过滤终态（快机器碰巧绿、慢 runner 抓到 `created`）——谓词补 `AND r.state IN ('succeeded','failed')`（`28fff94`），同 shard 其余 12 条调度测试全过证接线无 Windows 缺陷；修复后 push 遇 **GitHub 侧 PR 事件异常**（synchronize×2 与 reopened PR 的 opened 事件均零 runs，与 #154 同族），重开 [PR #167](https://github.com/lorek666/tianshu-agent/pull/167) 等待触发，终态以 Actions 为准。
