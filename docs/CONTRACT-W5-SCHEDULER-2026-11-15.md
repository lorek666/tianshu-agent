# CONTRACT · W5 调度器的 C3 契约评估（v1 不新增事件族，v2 影响面划界）

- 日期：2026-11-15 · 对应实现：[SPEC-W5-SCHEDULER-2026-11-15.md](SPEC-W5-SCHEDULER-2026-11-15.md)（分支 `feat/scheduler-tasks`）
- 性质：**纯契约分析，不夹带代码**——本文档不做任何 schemas/ 或 event_schema.py 改动。
- 账本行依据：`docs/ROADMAP-COMPLETE-AGENT-2026-11-14.md` W5 行「契约影响：新事件族？C3 评估」。

## A. 结论

**v1 不新增事件族，C3 封闭集不动。** 触发可见性由既有事件承载：调度器触发 = 走 `task.create` + `run.start` 的同一条存储路径，事件流自然出现 `task.created`（`run_id=TASK_SCOPE=""`）→ `run.start` → 后续 Run 事件。**「谁触发的」问题的答案在 Task 行与 `task.created` payload 里**（v2 扩展点见 §C.3）。schedule 自身的生命周期（创建/启停/删除/失败）v1 只落在 `schedules` 表（`schedule list` 可见，`last_error` 记失败原因），不进事件流。

## B. 为什么 v1 不需要新事件族

1. **触发可见性已经闭合。** 每个 fire 恰好产生一个 `task.created`（goal、workspaces、exec_policy、创建者 actor 全在 payload），随后 `run.start` 入队。事件消费方（UI 订阅、`task list`、`run status`）看到的世界与人手 `task create` 完全一致——调度器不是一条新的业务语义，只是又一个任务创建者。为同一个事实再造 `schedule.fired` 事件，等于让「一次触发」有两个权威记录源，回放时还要解释两者的对应关系。
2. **没有执行语义。** C3 的每个类型都对应回放可重建的状态转换（run/job/tool/approval…）。schedule 生命周期不改变任何 Run/Job 状态——`tick` 的输出是既有事件，不是新状态机。事件流里没有它的位置，直到 v2 决定「调度历史本身要可回放」。
3. **封闭集的代价是结构性的。** 新增一个类型要三处同改（`events.py` `EVENT_TYPES` + `schemas/c3/event.json` `type.enum` 与 `$defs.payload` + `event_schema.py` 逐类型契约），且 `event_schema.ensure_registry` 导入期校验 + `tests/test_events.py` 双向绑定，改一处忘另一处直接红。没有消费方读的事件不值得付这个代价。
4. **失败可见性的最低替代已在表里。** `schedules.last_error`（触发失败原因）+ daemon 日志（note 回调）覆盖了 v1 的排障需求；它是表列不是事件——代价是**不可回放、不可订阅**，这正是 v2 评估要补的（§C.1 的 `schedule.missed`）。

## C. v2 影响面（逐条评估，供 v2 立项直接引用）

### C.1 需要哪些新事件类型

| 类型 | 语义 | 触发条件（v2 设计意图） |
|---|---|---|
| `schedule.created` | 一条定时任务被创建 | CLI create（v1 已有表行，缺事件） |
| `schedule.fired` | 一次到期触发已完成 | tick 内 `_fire` 提交成功后追加（与触发同事务） |
| `schedule.missed` | 一次触发失败被跳过 | `_record_error` 路径（当前只写 `last_error` 列 + 日志） |
| `schedule.paused` / `schedule.resumed` | 启停 | `set_enabled`（v1 只 UPDATE 表） |
| `schedule.removed` | 删除 | `remove`（v1 只 DELETE 表） |

不建议做的：`schedule.updated`（改 goal/interval）——v1 没有更新接口，v2 若加再随实现评估。

### C.2 payload 形状（草案）

统一用 `schedule_id` 做主键引用，`run_id` 沿用 `TASK_SCOPE=""` 的伪 scope 先例（task.created 已用 `run_id=""`；调度事件不属于任何 Run）：

```json
// schedule.created / removed / paused / resumed（version 1）
{ "schedule_id": "sched_<hex12>", "goal": "…", "interval_seconds": 60,
  "workspaces": [{"root_path": "…", "mode": "writable"}], "enabled": true }

// schedule.fired（version 1）——桥接既有事件流
{ "schedule_id": "sched_<hex12>", "task_id": "task_<hex12>", "run_id": "run_<hex12>",
  "due_at": 1700000000000, "fired_at": 1700000061000,
  "catchup": false }

// schedule.missed（version 1）——失败原因进事件，可回放可订阅
{ "schedule_id": "sched_<hex12>", "error_kind": "TianshuError",
  "message": "工作区在两次运行之间失效（截断至 500 字符）",
  "next_fire_at": 1700000121000 }
```

设计约束：payload 只引用（`task_id`/`run_id`），不复制任务内容——内容以 `task.created` 为权威，避免第二个事实源；`catchup=true` 标记追赶补发（对应「从当前时间重排」语义），让回放能区分正常节奏与停机补发。actor_kind 用 `system`（枚举里已定义、当前无写入点——harness 注释说的正是这类预留）。

### C.3 「调度器触发」在 v1 事件里的既有痕迹与 v2 的最小扩展

v1 的 `task.created` payload 不含创建者来源字段（actor_kind=core 与人工 RPC 创建不可区分）。v2 若要 UI 区分「定时创建」与「人点出来的」，有两条路：

1. **不扩 C3**：消费方按 `schedule.fired` 的 `task_id` 反查关联（需要 C.1 落地）；
2. **扩 `task.created` payload**：加可选键 `origin: {"kind": "schedule", "schedule_id": "…"}`——这是既有类型 payload 加可选键，按 `event-payload-contract.md` 的版本纪律走（新增可选键 + 版本号递增 + legacy 读取不补值），不新增类型。

推荐 v2 先落 C.1 的 `schedule.fired`（桥接已含 task_id/run_id），`task.created` 加键留作后续——少动一个高频类型的 payload 契约。

### C.4 对 schemas/c3/event.json 与 event_schema.py 的影响面（v2 工作量清单）

若 v2 落地 §C.1 全部五个类型，需要**三处同改 + 两处测试绑定**（改一处忘另一处会红的机制）：

1. `src/tianshu/events.py` `EVENT_TYPES`：追加 5 个类型（frozenset）。
2. `schemas/c3/event.json`：`properties.type.enum` 追加 5 项；`$defs.payload` 追加 5 个逐类型对象契约（§C.2 形状，`additionalProperties: false` + required 键集）。
3. `src/tianshu/event_schema.py`：`_PAYLOAD_SCHEMAS` 追加 5 个逐类型 schema（与 event.json 逐字对齐的权威实现）。
4. `tests/test_events.py`：type.enum ↔ EVENT_TYPES 双向绑定用例扩 5 项。
5. `tests/test_event_schema.py`：逐类型 payload 契约的正/反用例（缺 required 键、多余键、错误类型各一条）。

成本估计：机械量大、风险低——都是既有模式（`subtask.spawned`/`undo.completed` 是最近的先例）。**前提**是 v2 先回答产品问题：调度历史是否需要被 UI 订阅与回放。在此之前，v1 的表列 + 日志是诚实且足够的可见性，不冒充事件。

### C.5 红线对照（v2 落地时同样适用）

- 事件 append-only：schedule.* 事件照常走 `EventLog.append`，schedules 表的 UPDATE/DELETE 照旧合法（意图表不是事件表，红线 1 约束的是 events）。
- 同事务纪律：`schedule.fired` 必须与任务创建/记账同一事务（复用 `EventLog.transaction()` 组合语义）——否则出现「任务已建但 fired 事件缺失」的半截状态，那正是红线 11 说的「崩溃后无法判定状态」。
- 封闭集：新增枚举三处同改（AGENTS.md §七），`schedule.missed` 的 `error_kind` 若做成枚举，须与错误类型封闭集对齐，不收自由字符串（或明确声明它只是截断后的展示文本、不是契约枚举——二选一，不骑墙）。

## D. 本次 PR 的边界重申

本 PR（实现 + 本分析）**不改**：`schemas/`（任何文件）、`event_schema.py`、`events.py` 的事件类型集合、RPC 方法表（M5-desktop.md）。若实现过程中发现 v1 无法避开上述任何一处，按工单约定停下只交分析与 SPEC——实际情况是避开了（触发路径复用既有事件族，管理面走表 + CLI）。
