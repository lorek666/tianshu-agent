# G2 子代理阶段的施工规格（SPEC-G2-DELEGATION-2026-11-11）

- 立据：`docs/PRD-v1.md` §5.2.1（通用工具准入 / 带种类的资源 / 子任务也是资源）、
  §5.6.2（共享预算账本：**子任务同时受根账户与局部上限约束**、父子链、取消传播）、
  §8.1.1（能直接执行命令的子代理必须先过隔离门禁）、§11.1 **G2 完整用户体验**
  （「按网络 → MCP → 记忆 → 子代理推进；后续各阶段用同一任务集证明新增收益」）。
- 上游事实：[`resource-kinds.md`](resource-kinds.md) §7 与 `PRD-v1.md` §5.2.1 明写
  「`subtask://` 能解析、能做许可判定，**没有委派执行器**」；`permissions._classify_subtask`
  现在是**恒拒**（`c4.subtask_unavailable`），账本已有 `subtasks` 维度与 `parent_id`
  父子链。本规格只做**委派一段**；MCP / 记忆执行器不在本轮（§5 逐条登记）。
- 基线：`main @ 0f30d01`（Merge PR #114）。落笔时基线实测 **1905 passed / 7 skipped**、
  `ruff check .` 干净、运行时依赖为零。
- 判定口径（沿用 `SPEC-PRD-REVIEW-2026-11-11.md` §0）：**有回归测试、且做过负控
  （把这次加的保护回退后测试会红）才算做完**。要求写进 PRD ≠ 已实现；判定不了 =
  unknown + 阻断。**不许**用「起了一个空 Run」冒充「子任务可用」。
- 范围（本轮**只做**）：
  - **D1**：合同 + 实体 + 派发。`task.created` 增加 `max_subtasks`（未声明 = 不允许委派）；
    子任务表（父 Run / 父 Task / 子 Task / 子 Run / Job 一号贯通）；`subtask.spawn` /
    `subtask.status` / `subtask.result` 三个工具与执行器；任务级上限（深度 1、每父 Run
    最多 2 个未完成、每根 Task 最多 4 个）、局部 `max_turns` / `token` 上限；子任务共享
    根预算账户（`parent_id` 链）与 `subtasks` 维度记账；`subtask.spawn` 走人工批准。
  - **D2**：执行与收束。子 Run 由既有 Worker 驱动（同一 `AgentLoop` 路径）；父 Run 在
    「还有未完成子任务」时挂起作业（`job.park`）并在子任务收尾时被唤醒
    （`JobStore.wake`）；父取消 → 子 Run 取消传播；父改目标 → 子任务被 fence，结果不能
    回填；`subtask.cancel`；结果以有界摘要 + `content://` 引用回填，回放不重跑子任务。
  - **D3**：真实用户任务 `RU-D1@1.0.0`（本机、零外网：父任务派一个只读子任务调查两份
    本地文档，父任务汇总并在报告里引用子任务来源）+ 负控 + 文档对账。
- 本轮**不做**（§5 登记）：子任务写文件、子任务联网 / exec / MCP / 记忆写入、子任务
  再委派、异步流式结果、A2A / 外部 Agent、跨进程 worker 池扩容、子任务 UI 页面、
  子任务独立审批嵌套（子任务只能做**自动放行的只读动作**，需要审批的动作在子任务里
  一律**拒绝**而不是弹审批）。

## 0 施工纪律

1. 只在自己的 worktree（`/Volumes/Date/ts-wt-deleg`，分支 `feat/g2-delegation*`）里操作；
   禁止在主检出 `/Volumes/Date/天枢Agent` 与别的会话的 worktree（`ts-wt-mcp` 等）里跑
   `git` 或改文件。
2. **不加第三方运行时依赖**；委派只用标准库与既有模块（`jobs` / `tasks` / `loop` /
   `budget` / `worker`）。
3. 一提交一件事：`feat:` / `test:` / `docs:` 分开；提交正文写「改了什么、为什么、
   判据、负控摘要」。
4. **不许改判据让自己变绿**：既有断言只能因为「新要求改变了语义」而改，且必须在
   PR body 写明旧口径、新口径与理由。本轮已知会改到的旧口径见 §4。
5. 收工前：全量 `pytest` 绿、`ruff` 干净；数字以干净 `main` 上的实测为准，
   **不编 CI 数字**。三平台必过检查只有 ubuntu / macOS / `ui`；Windows 是已知基线红，
   既不修也不写成绿。
6. 干不完就如实报：完成了什么、卡在哪、哪些宣称仍然不成立。

## 1 三个增量与依赖

| 增量 | 内容 | 依赖 | 用户可见效果 |
|---|---|---|---|
| D1 | 合同（`max_subtasks`）+ 子任务实体 + 三个工具 + 限额 + 共享预算 + 审批 | 无 | 父任务可以**排队**一个只读子任务并拿到子任务身份与状态 |
| D2 | Worker 驱动子 Run + 父挂起/唤醒 + 取消传播 + fence + 结果回填 + 回放 | D1 | 子任务真的会跑完；父任务等它，结果可核验 |
| D3 | `RU-D1@1.0.0` + 负控 + 文档对账 | D2 | 评测里能复跑「一个父任务派子任务 → 汇总 → 来源可核验」 |

D1 单独合入时子任务**不会被执行**（只有排队与查询），这是刻意的：先把身份、限额与
预算的账做实，再接执行。D1 的判据必须包含「未接 Worker 时子任务状态停在 queued，
符合状态机，不假装成功」。

## 2 施工口径

### 2.1 实体与父子链

- `tasks` 表加列（`_ensure_column`，旧库升级安全）：
  `parent_task_id TEXT`、`parent_run_id TEXT`、`subtask_id TEXT`、`depth INTEGER NOT NULL DEFAULT 0`、
  `max_subtasks INTEGER NOT NULL DEFAULT 0`。
- `runs` 表加列：`parent_run_id TEXT`、`subtask_id TEXT`。
- 新表 `subtasks`（与事件库同一 SQLite，和事件同生共死）：

  ```sql
  CREATE TABLE IF NOT EXISTS subtasks (
    subtask_id      TEXT NOT NULL,          -- 父作用域内的稳定 id（模型给的 slug）
    parent_task_id  TEXT NOT NULL,
    parent_run_id   TEXT NOT NULL,
    child_task_id   TEXT NOT NULL,
    child_run_id    TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    goal            TEXT NOT NULL,          -- 子任务目标（有界摘要，≤2 KiB）
    limits          TEXT NOT NULL,          -- {"max_turns":..,"max_tokens":..,"deadline_ms":..}
    state           TEXT NOT NULL,          -- queued|running|succeeded|failed|cancelled|fenced
    result_ref      TEXT,                   -- content://sha256:...
    summary         TEXT,                   -- 有界结果摘要（≤4 KiB）
    error           TEXT,
    created_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    PRIMARY KEY (parent_run_id, subtask_id)
  );
  ```

  **主键是 `(parent_run_id, subtask_id)`**：不同父 Run 可以各有一个同名 slug；同一父
  Run 里重复 spawn 同一 slug 是**拒绝**（`dispatch.subtask_exists`），不是静默复用。
- 子 Task 的字段固定为：`bindings = 父任务的全部绑定，逐个改写成只读`（去重）；
  `exec_policy = None`；`outbound_allow = ()`；`memory_scopes = ()`；
  `max_subtasks = 0`；`depth = 1`；`is_control = False`。
  于是子任务在既有策略下**天然只能做纯读**：写文件 → `c4.readonly_binding`、
  出网 → `c4.outbound_not_allowed`、exec → `c4.exec_not_configured`、
  MCP → `c4.mcp_not_configured`。这些**都是拒绝**，不是「弹审批」——子任务里不产生
  嵌套审批，也不消耗父任务的审批票据。

### 2.2 工具与资源

| 工具 | args | resources | action | 审批 |
|---|---|---|---|---|
| `subtask.spawn` | `subtask_id`（slug）、`goal`（≤2 KiB）、可选 `max_turns` / `max_tokens` | `subtask://<subtask_id>` | `delegate` | **必须**（`c4.subtask_spawn`） |
| `subtask.status` | `subtask_id` | `subtask://<subtask_id>` | `read` | 自动（纯读本地状态） |
| `subtask.result` | `subtask_id` | `subtask://<subtask_id>` | `read` | 自动（读已落库结果，不触发执行） |
| `subtask.cancel` | `subtask_id` | `subtask://<subtask_id>` | `delegate` | 自动（`c4.subtask_cancel`：只会减少执行，不扩权） |

- `subtask_id` 形状：沿用 `resources._OPAQUE_ID`（`[A-Za-z0-9_.:-]{1,128}`），
  **必须**是 `subtask://` 资源里的那一段原样；解析/规范化在 `resources.py` 已有，
  本轮不新增解析器。
- `_classify_subtask` 从「恒拒」改成真分类器：`delegate` → 审批（spawn）/ 自动（cancel，
  按工具 id 区分），`read` → 自动；其它动作 → `c4.action_resource_mismatch`。
  **不是**「有 `subtask://` 就放行」：工具归属、父任务身份、限额、预算、fence 都在
  `_authority_subtask()` 里判，分类器只回答「决策形状」。
- `loop._target_for` 增 `subtask` 分支（在「任务必须绑定目录」之前）：从 args 取
  `subtask_id` → `subtask://<id>`；零目录任务同样合法。

### 2.3 派发与审批（D1）

`_authority_subtask()`（`prepare()` 里走完，**弹批准之前**）：

1. 工具归属：非子任务工具带 `subtask://` 资源 → `dispatch.subtask_rejected`（沿用
   `c4.action_resource_mismatch` 的形状，规则 id 统一为 `dispatch.subtask_rejected`）；
2. 父任务必须声明 `max_subtasks > 0`：否则 **拒绝**（`c4.subtask_not_configured`）——
   「未声明即无委派能力」，与 `outbound_allow` 同一条纪律；
3. 父任务 `depth` 必须为 0：子任务不能再委派（`dispatch.subtask_depth_exceeded`）；
4. spawn 的 slug 在 `(parent_run_id, subtask_id)` 上必须不存在：
   存在 → `dispatch.subtask_exists`；
5. 每父 Run 未完成子任务 < 2、每根 Task 已 spawn 总数 < 4、请求的 `max_turns` ∈ [1,8]
   且 `max_tokens` ∈ [256, 8000]（超过上限就拒绝，不静默夹到上限）：
   任一条不满足 → `dispatch.subtask_limit`；
6. status / result / cancel 的目标必须存在于该父 Run 下：否则 `dispatch.subtask_not_found`
   （**不**用「不存在」冒充成功或空结果）。

`subtask.spawn` 的 `_dispatch_subtask()` 在一个事务里做四件事：建子 Task（事件
`task.created` 带父链字段）→ 建子 Run（state=`created`）→ enqueue 子 Job（`job.enqueued`）
→ 写 `subtasks` 行 + 父 Run 的 `subtask.spawned` 事件。**四件事同一事务**：少一件就等于
「父任务说派了，子任务不存在」或反过来。
- 结果：`succeeded` + `effect_state=none_applied`（本产品账本里没有文件副作用；
  子任务的副作用由它自己的调用记录），`output` 带 `{subtask_id, child_task_id, child_run_id,
  job_id, state:"queued", limits}`。
- 崩溃恢复：`recover` 看到 `subtask.spawn` 的 intent 没有 result 时，按
  `(run_id, subtask_id)` 查 `subtasks` 表——存在 → 补一条与当时一致的 `succeeded` 结果
  （身份是**存下来的**，不是猜的）；不存在 → 补 `unknown`：可能已建也可能没建，
  不能猜「没建」。这条写进 `recovery` 分支与测试。
- 幂等：同一 `call_id` 的重复派发由既有 dispatcher 唯一约束挡住；不同 `call_id` 但同
  slug → `dispatch.subtask_exists`。

### 2.4 限额与共享预算

- 任务级声明：`task.created` payload 增 **required** 键 `max_subtasks`（整数 0–4）；
  旧事件（v0）缺席按 0 处理（`LEGACY_SCHEMAS`），新任务一律显式写。CLI：
  `task create ... --max-subtasks N`；不传 = 0。
- 子任务局部上限（`limits` 列）：`max_turns` 默认 8、`max_tokens` 默认 2000、
  `deadline_ms` 固定 300_000（不可由模型抬高）。**上限只收窄不放大**：请求值超过
  声明范围 → 拒绝（`dispatch.subtask_limit`）。
- 共享预算：子 Run 的账户 `run:<child_run_id>`，`parent_id = 根任务账户`（父任务所属
  根 Run 的 `run:<root_run_id>`）；子账户只声明局部上限，**不**继承父任务剩余余量当
  自己的上限（父链负责扣减）。spawn 时在父账户的 `subtasks` 维度 `reserve` 1，
  子任务收尾时按实际结果 `settle`（成功/失败/取消都 settle 1；`abandon_unsent` 只在
  子任务**确定未执行**即终止时用——判据是子 Run 仍 `created`、作业从未被认领
  （`claim_epoch=0`）、作业仍 `pending`；父取消、fence、`subtask.cancel` 三条路径都按
  这个判据，而不是"父取消"这一个来源）。子任务未声明可用额度 → 预占失败 → spawn 拒绝
  （`dispatch.budget_refused`，复用既有编号）。

### 2.5 执行、等待与唤醒（D2）

- 子 Run 由**既有 Worker**（`Worker.drain` / `run_worker_once`）认领并驱动，走
  `AgentLoop` 同一路径（准入、审批、事件、游标）。不新写调度器。
- 父 Run 在「仍有未完成子任务」时结束一轮：`loop` 把 `job.park(reason="waiting_children")`，
  Run 状态保持 `RUNNING`（不新增 Run 状态），游标照常落库。
- 子任务收尾（成功 / 失败 / 取消 / fence）：在子 Run 收尾的同一事务里
  1. 更新 `subtasks.state / result_ref / summary / error / ended_at`；
  2. 往**父 Run** 追加一条 `subtask.finished` 事件（跨 Run 写事件用既有事件日志的显式
     `run_id` 参数，不另建日志）。**fence 例外：只改行、不写 `subtask.finished`**——
    事件名是"完成"，把一次"结果作废"写进去会让父 Run 的审计里出现一个不该被采信的结论；
     父 Run 要问 fenced 结果就调 `subtask.status`（读行，不读事件）；
  3. 若该父 Run 已无未完成子任务 → `JobStore.wake(parent_job)` 把父作业从 `waiting`
     置回 `pending`（新 API；`wake` 只对 `state='waiting'` 生效，不抢别人的 claim）。
- `subtask.status` 返回 `{subtask_id, state, child_task_id, child_run_id, child_run_state,
  job_id, limits, created_at, elapsed_ms, error}`；`state` 是子任务状态
  （queued/running/succeeded/failed/cancelled/fenced），`child_run_state` 是子 Run 状态机
  ——两者都给出，不互相冒充。
- `subtask.result`：未完成 → `{subtask_id, state, done:false}`（**不是**失败，也不是空结果）；
  完成 → `{subtask_id, state, done:true, summary, result_ref, error}`，另有可选的
  `usage:{turns?, tokens?}`——**未知的维度不写**（不拿 0 补齐，只有账本里显式记着 0 才写 0）。
  `result_ref` 指向子 Run 最后一轮模型输出的**有界文本**（≤4 KiB 内联 + `content://` 全文引用）。
  子任务没有文件写入，结果只经内容存储与事件保存，**不**要求父任务落地成用户文件。
  没有 `sources` 字段（D1 规格里写过，D2 未实现，这里按代码改口）。

### 2.6 取消、fence 与恢复（D2）

- 父 Run 收到取消（`control_requested=cancel`）时：先把所有未完成子任务的子 Job
  `request_cancel`、子 Run 标记取消请求，再按既有安全点收尾；父 Run 的 `run.end`
  `reason` 带 `subtasks_cancelled=N`。**不声称**已经产生的子任务费用被退回。
- 父任务改目标（`goal_revision` 变化）→ 子任务 fence：子 Run 在每次派发前核验父 Run 的
  `(goal_revision, lease_epoch)` 与 `subtasks` 行里记录的一致；不一致 →
  `dispatch.parent_fenced`，子任务状态置 `fenced`，结果不能回填父 Run。
- `subtask.cancel`：未完成 → 子 Job 取消请求 + 状态 `cancelling`（**不是** `cancelled`；
  执行侧在安全点确认）；已完成 → 幂等返回当前状态，不报错、不假装撤销。
- 崩溃恢复：**Worker 启动时**（`Worker.run_once` 的首次调用）先做两件事，再认领作业：
  ① 子任务行停在未完成、子 Run 已终结 → 以子 Run 终态为准修正（**以证据为准，不按进程
  退出猜**）；② 父作业停在 `waiting`、`subtasks` 表里已无未完成行 → 补一次 `wake`。
  `recover` CLI **未接**这两步（它是单 Run 对账，不做全库扫描），见 §5 边界 14。

### 2.7 事件、规则、工具 id 增量

- 事件类型 41 → **43**：`subtask.spawned`（父 Run；payload：subtask_id、child_task_id、
  child_run_id、job_id、goal_digest、limits）、`subtask.finished`（父 Run；payload：
  subtask_id、state、result_ref、error、usage）。两者都必须进 `schemas/c3/event.json`
  镜像与 `event_schema` 逐类型契约。
- 规则 id 59 → **65**：`c4.subtask_spawn`（判定：需审批）、`c4.subtask_cancel`（判定：自动）、
  `c4.subtask_not_configured`、`dispatch.subtask_exists`、`dispatch.subtask_not_found`、
  `dispatch.subtask_limit`、`dispatch.subtask_depth_exceeded`、`dispatch.parent_fenced`
  ——**实际新增 8 条**（59→67），分组进「资源种类」与「身份失配」，`DECISION_RULE_IDS`
  增两条判定。数字以实施时 `RULE_IDS` 实测为准，规格里的数字必须在 PR body 对账。
- 工具 id 19 → **23**：`subtask.spawn` / `subtask.status` / `subtask.result` /
  `subtask.cancel`（`_TOOL_ID` 封闭集 + `_TOOL_ID` 镜像）。
- `task.created` payload：新增 **required** 键 `max_subtasks`，与可选父链字段
  `parent_task_id` / `parent_run_id` / `subtask_id` / `depth`；`LEGACY_SCHEMAS` 为旧事件
  提供 `required=[]` 的 v0 形状（旧事件合法，新事件一律带齐）。

### 2.8 CLI 与回放

- CLI：`task create --max-subtasks N`；`run status` 输出里列出父 Run 的子任务
  （id / state / child_run）。**不做**独立 UI 页面（§5）。
- 回放：`replay` 对 `subtask.spawn` / `subtask.result` 结果做形状校验：
  `subtask.spawn` 缺 `subtask_id` / `child_run_id` / `job_id` → `problem`；
  `subtask.result` 的 `done:true` 缺 `summary` 键 → `problem`，**有正文**
  （`summary` 非空）却缺合法 `content://` 引用 → `problem`，`result_ref` 存在但
  不是合法 `content://` → `problem`（即使摘要为空）。`done:true` +
  空摘要 + 无引用是**合法**形状（子任务确实可能没有输出；fenced / 取消也只写行
  不写摘要）——把这条判红就是审计工具在自己的正常产出上误报。回放**绝不**执行
  子任务、不唤醒作业、不重算预算。

## 3 判据与负控总表（回填实测）

**D1（合同与派发，11 条）**

| # | 判据 | 钉住它的测试 |
|---|---|---|
| D1-c1 | `task.created` 必填 `max_subtasks`；旧事件（缺键）按 0；非法值拒绝 | `test_task_max_subtasks_contract`（参数化） |
| D1-c2 | 未声明委派能力（`max_subtasks=0`）→ `c4.subtask_not_configured`，**在弹批准之前** | `test_spawn_without_declaration_is_rejected_before_approval` |
| D1-c3 | 子任务 `depth=1`、只读绑定、无 exec/外发/记忆/再委派 | `test_child_task_is_read_only_and_depth_one` |
| D1-c4 | spawn 四件事同一事务：子 Task、子 Run、子 Job、`subtasks` 行、`subtask.spawned` | `test_spawn_is_atomic` |
| D1-c5 | 同 slug 重复 spawn → `dispatch.subtask_exists`；不同父 Run 同 slug 合法 | `test_subtask_slug_scoped_to_parent_run` |
| D1-c6 | 限额：未完成 >2 / 累计 >4 / `max_turns` 越界 → `dispatch.subtask_limit`（不静默夹取） | `test_spawn_limits_are_exact` |
| D1-c7 | 未知 slug 的 status/result/cancel → `dispatch.subtask_not_found` | `test_unknown_subtask_is_explicit` |
| D1-c8 | 共享预算：spawn 在父账户 `subtasks` 维度预占；父额度耗尽 → `dispatch.budget_refused` | `test_spawn_reserves_subtasks_dimension` |
| D1-c9 | `subtask.spawn` 必须人工批准；批准后子任务 queued、未执行 | `test_spawn_requires_approval_then_queues` |
| D1-c10 | 崩溃恢复：intent 无 result 且行存在 → 补身份一致的结果；行不存在 → `unknown` | `test_recover_spawn_is_evidence_based` |
| D1-c11 | 契约镜像：事件 43 / 规则按实施实测 / 工具 23、`schemas/c3` 一致 | `test_event_schema_mirror`、`test_tool_id_enum` |

**D2（执行与收束，10 条）**

| # | 判据 | 钉住它的测试 |
|---|---|---|
| D2-c1 | Worker 驱动子 Run 到终态，子任务状态与子 Run 终态一致 | `test_worker_drives_child_run` |
| D2-c2 | 父 Run 有未完成子任务时作业 `waiting`（不是 `pending`、不是 `claimed`） | `test_parent_parks_while_children_pending` |
| D2-c3 | 最后一个子任务收尾 → 父作业 `wake` 回 `pending`，父 Run 可继续 | `test_last_child_wakes_parent` |
| D2-c4 | `subtask.result` 未完成 → `done:false`；完成 → 摘要 + `content://` 引用 | `test_subtask_result_shape` |
| D2-c5 | 父取消 → 子 Job 取消请求 + 传播；已送出费用如实计入 | `test_parent_cancel_propagates` |
| D2-c6 | 父改目标 → 子任务 fence，结果不回填 | `test_parent_revision_change_fences_children`、`test_child_finish_after_parent_revision_is_not_backfilled` |
| D2-c7 | `subtask.cancel` 未完成 → `cancelling`；已完成 → 幂等 | `test_subtask_cancel_is_not_instant` |
| D2-c8 | 父作业 waiting 但子任务已终结（崩溃窗口）→ 启动恢复补 wake；恢复与收尾用同一套 fence 判定 | `test_recovery_wakes_stale_parent`、`test_terminal_parent_is_not_woken`、`test_recovery_fences_instead_of_backfilling` |
| D2-c9 | 回放不执行子任务、缺字段 → `problem`；合法空结果/取消/fence **不**误报 | `test_replay_subtask_shapes`、`test_replay_flags_missing_subtask_identity`、`test_replay_flags_done_without_ref_or_summary`、`test_replay_does_not_execute_or_wake_children`、`test_replay_accepts_legit_empty_result`、`test_replay_accepts_legit_cancelled_result`、`test_replay_accepts_fenced_result` |
| D2-c10 | 负控：§4 每条回退后对应测试变红（实测回填 PR body） | 见 §4 |

**D2 实测回填（2026-11-12，`feat/g2-delegation-d2`）**：

- 测试文件：`tests/test_g2_delegation_exec.py`（c1–c5、c7、c8 + 事务原子性 + 结算守卫 + 无输出不编造 + CLI，12 条）、
  `tests/test_g2_delegation_fence.py`（c6 两条路径 + 恢复路径的 fence 一致性，3 条）、
  `tests/test_g2_delegation_replay.py`（c9 四条 + 合法空结果/取消/fence 不误报，7 条）。
- 全量 **2069 passed / 7 skipped**（354.14s，macOS arm64；基线 `main @ 7dc9350` 同机 2047/7、354.21s；+22）。负控 6 条全部实测变红（跑 7 个 `test_g2_delegation_*.py` 共 57 条，失败数 24/2/1/3/15/2，变异方式见 PR body；另加复审整改的 3 条负控）。额外用
  `test_child_finish_is_one_transaction` 钉住「行/事件/预算/唤醒同一事务」：
  在预算结算处注入异常，回滚后 Run 仍 `running`、`subtasks` 行未提交、
  父 Run 无 `subtask.finished`、预占仍占着。
- 口径细节（不改判据，只把实现选择写清）：父取消时**确定未开始**的子任务
  （子 Run 仍 `created`、作业从未被认领）当场以 `cancelled` 收尾并
  `abandon_unsent`，其余先 `cancelling` 由执行侧安全点确认——否则会留下
  永远 `created` 的子 Run 与永远 `cancelling` 的行。

**D3（评测与对账，4 条）**

| # | 判据 | 钉住它的测试 |
|---|---|---|
| D3-c1 | `RU-D1@1.0.0`：父任务派一个只读子任务，子任务读两份本地文档并回报，父任务汇总 | `test_eval_real_tasks.py::test_delegation_task_spawns_readonly_subtask_and_aggregates` |
| D3-c2 | 子任务是只读的：子任务尝试写文件/出网在评测里被拒，报告如实呈现 | `test_delegation_child_attempts_are_refused_and_report_says_readonly` + `test_delegation_readonly_negative_control` |
| D3-c3 | 子任务失败/超时 → 父任务报告写「没有取得」，不编造 | `test_delegation_failure_is_reported_without_fabrication` + `test_delegation_failure_negative_control` |
| D3-c4 | 文档对账：PRD / KNOWLEDGE / README / 交付对账更新，含未做清单 | 文档 diff（本文件 §6、`real-user-tasks.md`、`PRD-v1.md` §10.5/§11.1、`KNOWLEDGE.md` §8、`README.md`、`PRD-REVIEW-DELIVERY-2026-11-11.md`） |

**D3 实测回填（2026-11-12，`feat/g2-delegation-d3`）**：

- 任务与 runner：`src/tianshu/eval/tasks.py` 新增 `RU-D1@1.0.0`（公开，第 8 条）与
  `DELEGATION_FAILURE_TASK`（测试内失败路径，不进公开分母）；`eval/cases.py` 增
  `max_subtasks` / `subtask_scripts` / `SubtaskScript`，`eval/runner.py` 在父 Run
  park 时用 `Worker` + 逐子任务 `engine_factory` 驱动子作业，再 `resume_run(parent)`；
  父子脚本必须分开，未声明的子任务**拒绝驱动**，往返上限 8 轮，失败原因写进样本
  `error`。`eval/checks.py` 新增 5 个判据工厂（`subtask.spawned` /
  `subtask.finished_state` / `subtask.result_quoted` / `subtask.readonly_boundary` /
  `subtask.output_unavailable`），其中两条 safety 判据在
  `test_eval_negative_controls.py` 登记负控。
- 测试：`tests/test_eval_real_tasks.py` **22 → 29** 条（D3-c1/c2/c3 + 2 条负控 +
  1 条 fail-closed + 1 条分母回归）；全量 **2076 passed / 7 skipped**（356.20s，macOS arm64 本机；
  基线 `main @ 5eed5ee` 同机 **2069 passed / 7 skipped**、358.23s；+7）。
  `run_real_user_tasks(repeat=3)`：`scope.real_user` cases 8 / runs 24，全绿，26.73s。
  `ruff check .` 干净。负控逐条实测见 PR body（含 runner 驱动、子脚本选择、子事件
  采集、只读边界、失败不编造）。
- 口径：公开真实用户任务从 7 条变 8 条，`REAL_USER_CASES` 分母变 8/24；契约基线
  20 条与 `SUITE_VERSION=m4.6` 未动（任务版本独立）。`fixture_digest` 纳入
  `max_subtasks` 与 `subtask_scripts`，改委派声明/子脚本会让这条 case 不可比。
  **这两个键只在非默认时写入**，所以既有 27 条 case 的 digest 逐字节不变（实测
  `main @ 5eed5ee` 同机对比一致），不需要为此 bump `SUITE_VERSION`；新增 `RU-D1`
  让 case 集合变化，旧报告与新报告本就不可比（集合不同）。

## 4 口径冲突与旧口径变更（必须写进 PR body）

| # | 旧口径 | 新口径 | 理由 / 影响 |
|---|---|---|---|
| 1 | `subtask://` 能解析、能做许可判定；`_classify_subtask` **恒拒**（`c4.subtask_unavailable`） | 委派可执行（受 `max_subtasks` 声明 + 人工批准 + 深度/数量限额约束） | G2 子代理接线；`c4.subtask_unavailable` 从「恒拒」变成**保留未使用**（委派已可用，但仍保留编号给历史日志） |
| 2 | `EXECUTABLE_KINDS = {file, memory, network, mcp?}`（MCP 由另一会话推进） | 增 `subtask` | 委派有执行器了 |
| 3 | `task.created` payload（含 `outbound_allow` 必填） | 再增必填 `max_subtasks`；可选父链字段 | 能力声明 fail-closed；旧事件 v0 合法 |
| 4 | 工具 id 19（+MCP 会话可能追加） | +4（23 及以上） | 四个工具需要稳定 id |
| 5 | 事件类型 41（+MCP 可能追加） | +2：`subtask.spawned` / `subtask.finished` | 父 Run 侧的委派审计 |
| 6 | 规则 id 59（+MCP 可能追加） | +8（67 及以上） | 每条拒绝/判定可对账 |

## 5 诚实边界（交付后仍然不成立 / 未验证）

1. **子任务是只读的**：不能写文件、不能出网、不能 exec、不能调 MCP、不能写记忆、
   不能再委派。写类工作仍由父任务自己（走正常审批）完成。
2. **没有嵌套审批**：子任务中任何需要人工批准的动作都是**拒绝**（不是弹审批）。
   所以「子任务里问用户」这件事**不支持**。
3. **深度固定 1、并发与总量固定上限**（每父 Run 2 个未完成、每根 Task 4 个），
   不做配置化扩容、不做 worker 池扩容。
4. **子任务不是一个独立进程**：它复用同一个 Worker / 同一台机器；「真多进程并发争抢
   最后一份额度」的验收仍只覆盖账本层，不声称跨机器调度。
5. **父任务等待会占用一个等待中的作业**：父作业 `waiting` 时让出执行槽，但没有优先级
   与公平性保证；`wake` 只保证「可以被重新认领」，不保证立刻被认领。
6. **结果只有有界摘要 + 引用**：子任务的完整轨迹在它自己的 Run 事件里，不复制进父
   Run；父任务若需要细节必须自己再读（`result_ref` 指向最后一轮有界输出）。
7. **取消不退款**：已发生的模型/网络费用照实计入；取消只保证「不再派发新副作用」。
8. **没有子任务 UI 页面**：CLI `run status` 与事件里有，桌面 UI 不新增面板。
9. **没有 A2A / 外部 Agent / 流式结果**：结果在收尾时一次性落库。
10. **Windows 未验证**：委派复用同一套 Job/Worker，Windows 基线红不因本轮改口。
11. **`max_subtasks` 是任务级静态声明**：变更要新任务（与 `outbound_allow` 一致），
    不支持运行中扩大。
12. **子 Run 收尾是两笔事务**（`claims.finish` 先提交，`run.end` + 子任务行 +
    预算 + wake 后提交）：若恰在两笔之间崩溃/被 OOM 杀掉，会留下"子作业已 done、
    子 Run 非终态、`subtasks` 行未收尾、父作业 waiting"。启动恢复**修不了**这种
    状态（它只认子 Run 终态），需要手动 `run resume <child_run_id>`（`run status`
    能列出子 Run id）。窗口只有几条语句宽，但没有自动化兜底。
13. **父 Run 失败时不传播取消**：模型超时/不可重试错误让父 Run `failed` 时，已派出的
    子任务仍在队列里照常跑完，并在父 `run.end` **之后**写 `subtask.finished`（父 Run
    不等待、也不读取它们）。费用照实结算；关闭这些子任务目前只能手动
    `subtask.cancel` / `run cancel`。规格 §2.6 只要求 cancel 传播，故不视为违约，
    但属未自动化。
14. **启动恢复只在 Worker 启动时跑**：`recover` CLI 还是单 Run 对账，不做全库扫描；
    所以"没人起 worker"时，停在 `waiting` 的父作业不会自己动。另外 `reconcile_finished_subtasks`
    在修正成功收尾的子任务行时会把说明性文案写进 `error` 列（如"启动恢复：按子 Run 终态
    succeeded 修正"）——这是审计注记，不是失败；`subtask.result` 会把该列原样给出。
15. **D3 评测是脚本模型，不是真实模型分数**：父/子两套脚本都是 `ScriptedModel`；
    父报告汇总文字预先写好，判据证明"派发 → Worker 驱动 → 结果回填 → 逐字汇总 →
    引用"这条产品链路，不证明真实模型会写摘要。`subtask.result_quoted` 要求产物
    **逐字**包含子任务摘要；真实模型改写摘要会判**完成度**失败（不是安全失败），
    要改口径必须换任务版本。
16. **D3 只覆盖一条公开委派任务 + 一条测试内失败路径**：失败路径 `RU-D1-F`
    （`DELEGATION_FAILURE_TASK`）只在 `tests/test_eval_real_tasks.py` 里按 repeat=3
    跑，**不计入** `REAL_USER_TASKS` 的 8 条公开分母；它证明"子任务失败 → 父报告不
    编造"，但不是第二条公开用户任务。多子任务并发、子任务取消/fence 的**评测**仍未做
    （D2 的单元/集成测试覆盖，不是真实用户任务评测）。
17. **子任务只读边界是 D1/D2 的产品实现，D3 只是测量**：评测断言"写/外发被拒"的
    规则编号与"无成功副作用"，不重复证明 D1 的只读绑定实现；若 D1 的绑定被回退，
    D3 判据会红，但根因归属要看 D1 的测试。

## 6 交付清单（合入后回填实测）

| # | 内容 | PR | 状态 |
|---|---|---|---|
| 1 | 本施工规格 | 本 PR | 已合入 |
| 2 | D1 合同 + 实体 + spawn/status/result + 限额 + 预算 | #120 | 已合入（`main @ 6140d9c`） |
| 3 | D2 Worker 驱动 + 等待/唤醒 + 取消/fence + 回放 | #121 | 已交付（分支 `feat/g2-delegation-d2`；实测见 §3） |
| 4 | D3 评测任务 + 文档对账 | 待填（分支 `feat/g2-delegation-d3`） | 已交付（D3-c1…c4；2076 passed / 7 skipped，基线 2069/7，+7；负控见 PR body） |
