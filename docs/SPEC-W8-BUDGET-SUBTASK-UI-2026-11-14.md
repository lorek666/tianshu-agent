# SPEC-W8：预算账本 UI + 子任务 UI（2026-11-14）

> 路线图 W8（`docs/ROADMAP-COMPLETE-AGENT-2026-11-14.md`，随 PR #151 交付）：「预算账本
> UI + 子任务 UI | 缺口出处 §7 挂账④⑤ | 契约影响：无 | 冲突面 ui/src（冷）| 外部阻塞无」。
> 基线 `main@88ec0ab`，分支 `feat/budget-subtask-ui`。**本文是设计与判据记录，先于代码**；
> §8 回填实测结果，未回填前不声称"已验证"。

## 0. 占用面自查（2026-11-14 实测）

对在途 PR 各跑 `gh pr view <N> --json files --jq '[.files[].path]|join(" ")'`：

| PR | 触及文件（与 W8 相关的结论） |
|---|---|
| #140 | `.gitignore` `AGENTS.md` `README.md` `docs/KNOWLEDGE.md` 与 7 份 docs——无 ui/ |
| #141 | `docs/PRD-v1.md` `docs/TECH-ARCH.md` |
| #146 | `README.md` 与 docs/ 证据目录——无 ui/；`src/tianshu/control.py` 按任务红线视为占用 |
| #150 | `metrics.py` `tests/test_metrics.py` + docs |
| #151 | `credentials.py` `keychain_macos.py` `tests/test_credentials.py` + ROADMAP/SPEC |
| #152 | `net.py` `tests/conftest.py` `tests/test_net_resilience.py` + docs |
| #154 | `cli.py` `loop.py` `memory*.py` `embedding_openai.py` `tests/httpserver.py` 等 |

**W8 改动面 = `ui/src/**` + `ui/dist`（构建产物）+ 本 SPEC + `docs/KNOWLEDGE.md`（仅追加）**
——与上表零交集（KNOWLEDGE.md 属任务豁免的追加条目）。后端 `src/tianshu/**` 零改动。

## 1. 现状代码事实（全部逐行核实过）

### 1.1 控制面 RPC：没有预算/子任务查询端点

`src/tianshu/control.py:222-255` 的 `_methods` 共 25 个方法：`state` / `task.{list,create,
revise}` / `run.{start,list,status,pause,resume,cancel}` / `approvals.{list,decide}` /
`events.run` / `undo.{plan,apply}` / `memory.*`（9 个）。**没有 `budget.*`，没有任何子任务
查询方法**；`run.status` 的 `_run_view`（control.py:532）只给状态机/效果计数，不带账本。

`src/tianshu/budget.py` 的读取 API（`account()` / `usage()` / `pending()` /
`reconcile()`）已存在，但 `BudgetLedger` **不写任何事件**（全文无 `tx.append` / `append`
调用——账本是事件库同 SQLite 里的纯表投影，只有 reserve/settle/cancel 改表）。因此：
UI 无法从 `events.run` 重建**账户级**账本状态（上限设置、未决预占清单、下载字节维度、
`overrun` 标志、账户取消状态）。这些数据的唯一出口是 `ControlPlane._methods` 里加端点，
而 `control.py` 是禁改文件（PR #146 占用；路线图 W3 亦标它为冲突面）——**本工作流停在
契约**，见 §6。

### 1.2 事件面：既有 `events.run` 端点已携带两个面板需要的全部数据

选中 Run 的事件流（App.tsx `refreshRun` → `events.run` 分页拉全 → `timeline` state，
`normalizeEvent` 保留完整 payload）里，与 W8 相关的事件与字段：

| 事件 | 字段（出处） | 用途 |
|---|---|---|
| `subtask.spawned` | `subtask_id` / `child_task_id` / `child_run_id` / `job_id` / `goal_digest` / `limits{max_turns, max_tokens, deadline_ms}`（`event_schema.py:924`，`_SUBTASK_LIMITS` 全必填） | 子任务卡片 |
| `subtask.finished` | `subtask_id` / `state`（7 态枚举 `queued/running/cancelling/succeeded/failed/cancelled/fenced`，`event_schema.py:450`）/ `result_ref?` / `error?` / `usage{turns, tokens}?`（`event_schema.py:944`） | 子任务收尾与用量 |
| `run.start` | `payload.limits{max_turns, max_total_tokens}`（`control.py:454` 恒写；`event_schema.py:892` 中该键不在 required——旧事件可能缺） | 预算面板的 Run 限额 |
| `model.result` | `usage{input_tokens?, output_tokens?}`（均可空 = 未知用量）+ `cost`（`pricing.CostRecord.to_payload()`：`cost` 微美元整数或 `None` = 费用未知，`pricing.py:103`） | 已知/未知费用与 token 用量 |
| `policy.denied` / `dispatch.rejected` | `payload.rule_id ∈ RULE_IDS`：`dispatch.budget_refused`（`rules.py:139`）、`dispatch.subtask_limit`（`rules.py:118`，dispatcher.py:3099-3138 三条限额检查的真实产生路径）、`dispatch.budget_refused`（dispatcher.py:1279/1604/1853 预占被拒） | C4 拒绝记录 |
| `model.error` | `error`（自由文本）/ `attempt?` / `retryable?`（`event_schema.py`） | 模型调用失败计数（不做字符串猜测归类） |

两个子任务事件都写在**父 Run 的 run_id** 下：`subtask.spawned` 由派发层在创建事务内补写
（`tasks.py:1996` 注释 + `tasks.py` 派发路径），`subtask.finished` 由收尾
`tx.append(row.parent_run_id, "subtask.finished", …)` 写（`tasks.py:2462-2478`）——
父 Run 的 `events.run` 天然能看到全部委派。`subtasks` 表只是这些事件的投影
（"行是当前结论，事件是当时结论"，`tasks.py:2436` 注释），UI 读事件即读事实源，
**不需要新端点**。

### 1.3 既有 UI 组件与约定

- `App.tsx:812-813` 已把 `timeline: EventView[]` 经 `reduceEventTimeline` / `summarizeCost`
  喂给 `RunDetail`；`lib/timeline.ts` 的 `readEventCost` 已实现"已知费用（微美元累加）与
  未知费用（单独计数，不当 0）"的完整口径（两种历史 cost 形状都认）。
- `lib/humanize.ts` **没有** `subtask.spawned` / `subtask.finished` 条目（目前走兜底句）；
  `ACTION_LABELS` 没有 `dispatch.budget_refused` / `dispatch.subtask_limit` 的中文映射。
- 组件测试风格：`renderToStaticMarkup` + 字符串断言（`components/__tests__/memory-panel.test.tsx`）；
  纯函数测试直调（`lib/__tests__/timeline.test.ts` 等）。
- demo：`demo/world.ts` 播种 Run 只发 `run.started` / `model.intent` / `model.result`
  （cost 裸数字）/ tool/receipt 事件，不发子任务事件、不带 usage / limits；
  `demo/host.ts` 不实现 `memory.*`（演示模式的能力缺口是既有事实）。

### 1.4 挂账原文（§7）

- **G1 收口条目**（KNOWLEDGE.md §7，PR #91/#92/#96/#97/#99 段）：「⑤ 账本没有 UI 展示」。
- **G2 网络阶段条目**（同节）：「**仍不成立**：子任务只读、深度 1、单机同 Worker 池、取消
  不退款、读不到输出时摘要为空、**无 UI 页面**、Windows 未验证」。

## 2. 设计

### 2.1 原则

1. **纯前端消费既有端点**：零 RPC 变更、零后端文件改动。两个面板的全部输入 =
   App 已加载的选中 Run 事件（`timeline: EventView[]`）+ `state.runs`（子 Run 状态回填）。
2. **不造假数据**：面板只渲染事件里真实存在的字段；缺 = "未知"，绝不编造 0 / 默认值
   （PRD §5.6 第 4 条："尝试过但全失败不得显示没有模型调用"、"未知不混成 0"）。
3. **不做字符串黑名单推断**：`model.error` 的自由文本不参与"这是不是预算拒绝"的判定；
   拒绝归类只认封闭集 `rule_id`（红线"不用字符串黑名单做安全/判定"）。
4. **机器枚举不上主界面**（PRD 决策 D1 / ui/README）：中文人话为主，原始 `rule_id` /
   状态枚举只在详情折叠区出现。

### 2.2 新增文件（全部在 ui/src）

| 文件 | 职责 |
|---|---|
| `lib/budgetView.ts` | 纯函数 `buildBudgetView(events: EventView[]): BudgetView`：从 `run.start` 读 `limits`；从 `model.result` 累计 usage（显式整数才计，缺失的尝试进 `unknownUsageAttempts`）与费用（复用 `readEventCost` / `summarizeCost` 的口径，直接调用 `summarizeCost` 保证一致）；从 `policy.denied` / `dispatch.rejected` 按 `rule_id` 封闭集（`dispatch.budget_refused` / `dispatch.subtask_limit`）收集拒绝记录；统计 `model.intent` / `model.error` 计数。 |
| `lib/subtasks.ts` | 纯函数 `buildSubtaskViews(events, runs): SubtaskRow[]`：按 `subtask_id` 合并 `subtask.spawned` + `subtask.finished`（后者缺 = 进行中）；usage / limits / error 透传；`child_run_id` 能在 `state.runs` 里找到时附子 Run 状态人话。 |
| `components/BudgetPanel.tsx` | 预算面板：限额（轮次 / 累计 token）→ 已用量（token 已知/未知、调用次数、已知/未知费用）→ 拒绝记录（C4 人话 + 详情折叠区原始 rule_id）。空输入返回 null（不渲染空卡片）。 |
| `components/SubtaskPanel.tsx` | 子任务面板：每张卡片 = 目标摘要（goal_digest 前 8 位 + "子任务"）+ 状态 pill + 限额三项 + 用量 + 子 Run 状态；`error` 文本转义展示。无子任务返回 null。 |

### 2.3 接线与数据流

- `App.tsx`：把已有 `timeline`（选中 Run 的已加载事件，上限 2000 条）作为新 prop
  `events` 传给 `RunDetail`；`state.runs` 已经在 App 手里，随 `RunDetail` 现有 props 链
  传下去（不改 RPC、不加轮询）。
- `RunDetail.tsx`：在 cost-card 之后渲染 `<BudgetPanel events={…} />` 与
  `<SubtaskPanel events={…} runs={…} />`（两者内部自行判空）。
- `lib/humanize.ts`：新增 `subtask.spawned`（"派发了子任务（限额 轮次 N · token M）"）、
  `subtask.finished`（按 state 人话："子任务完成 / 失败 / 已取消 / 已被取代"）两类句子；
  `ACTION_LABELS` 补 `dispatch.budget_refused`（"预算额度不足，请求没有发出"）与
  `dispatch.subtask_limit`（"委派限额不满足"）。
- `styles.css`：新增面板需要的少量类（沿用 `cost-card` / `summary-item` 的既有风格）。
- **demo**：`demo/world.ts` 播种 Run 时补发 `subtask.spawned` + `subtask.finished`
  （合成子任务 + usage）与 `run.started` 的 `limits`、`model.result` 的 `usage` ——
  演示数据只存在于 `?demo=1` 场景（ui/README §演示模式的边界既有先例）。

### 2.4 口径细节（写死在实现里）

- token 累计：只认 `usage.input_tokens` / `usage.output_tokens` **存在且为有限整数**的
  `model.result`；两者都缺失（断流 / 超时可能计费但无 usage）→ 该次进 `unknownUsageAttempts`。
- 费用：直接调用既有 `summarizeCost(events)`，本工作流不发明第二种 cost 解释。
- 轮次进度：`model.intent` 计数 ≈ 已发生的请求轮次（与 RunResult 口径一致的近似，
  界面措辞用"请求次数"而非"轮次上限已用 X/Y"这种假精确）。
- 拒绝记录字段：`{seq, ruleId, reason}`（`payload.reason` 原文）；重复 rule_id 不去重
  （每次拒绝都是一次真实事件）。
- 畸形容错：payload 不是对象 / limits 缺键 / usage 是字符串 → 按"未知"显示，不抛错
  （对齐 `normalize.ts` 的保守哲学）；子任务 spawned 缺 `limits` → 三项都显示"未知"。
- 时间线截断提示：事件上限 2000 条时，面板数据可能不完整——面板标题下方固定注明
  "基于已加载的最近事件"。

## 3. 判据表（每条 ↔ 测试）

| # | 判据 | 测试 |
|---|---|---|
| J1 | spawned+finished 合并出正确状态与用量 | `lib/__tests__/subtasks.test.ts` |
| J2 | 只有 spawned（未 finished）→ 进行中，不冒充终态 | 同上 |
| J3 | usage 缺失 → 显示未知，不当 0 | `lib/__tests__/budgetView.test.ts` |
| J4 | 费用已知/未知分列与 `summarizeCost` 一致 | 同上 |
| J5 | token 只累计显式 usage；未知尝试单独计数 | 同上 |
| J6 | `run.start` limits 展示；缺 limits 显示未知，不造默认值 | 同上 |
| J7 | `policy.denied` rule_id=`dispatch.budget_refused` / `dispatch.subtask_limit` → C4 人话 + 详情区原始编号 | `lib/__tests__/budgetView.test.ts` + `components/__tests__/budget-panel.test.tsx` |
| J8 | 无子任务 / 无预算相关事件的 Run：两个面板都不渲染 | `components/__tests__/run-panels.test.tsx` |
| J9 | 畸形 payload（非对象 / 缺键 / 错型）不崩、显示未知 | `lib/__tests__/budgetView.test.ts` + subtasks 测试 |
| J10 | `subtask.finished.error` 任意文本被转义（XSS 负例） | `components/__tests__/run-panels.test.tsx` |
| J11 | humanize 新增两类子任务句子 + 两个 C4 编号中文映射 | `lib/__tests__/humanize.test.ts` 追加 |
| J12 | demo 世界能驱动两个面板出真实形状（demo 场景限定） | `src/__tests__/run-mode.test.tsx` 追加或独立 demo 测试 |
| J13 | 既有前端套件全绿（回归不破） | `pnpm --dir ui run test` 全量 |
| J14 | 后端零改动 ⇒ 全量 pytest 数字与基线一致 | `uv run --frozen pytest -o addopts="" -q` |

## 4. 负控清单（变异 → 红 → 还原 → 绿；证据在 §8 回填）

| # | 变异点（改什么） | 期望红的判据 |
|---|---|---|
| NC1 | `lib/budgetView.ts`：把 usage 缺失的尝试从 `unknownUsageAttempts` 改成按 0 处理（"未知用量当 0"） | J3 / J5 红 |
| NC2 | `lib/budgetView.ts`：拒绝收集的 rule_id 集合删掉 `dispatch.budget_refused`（C4 编号渲染漏一类） | J7 红 |
| NC3 | `lib/subtasks.ts`：spawned 无 finished 时把状态兜底成 `succeeded`（未完成冒充终态） | J2 红 |

三条全部实跑：变异后目标测试文件必须红（记录失败的测试名），还原后复跑绿。
不许编造负控证据；负控变异只在本地临时改，不进任何提交。

## 5. 文档对账

- `docs/KNOWLEDGE.md` §8 追加本次会话日志（做了什么 / 发现了什么 / 决定了什么）。
- `docs/KNOWLEDGE.md` §7：G1 收口条目的「⑤ 账本没有 UI 展示」与 G2 条目的「无 UI 页面」
  按事实更新——事件面展示已交付，账户级账本视图仍缺 control 端点（指向本 SPEC §6）。
  只动这两处短语，不重写历史条目。
- `ui/README.md`：界面结构表与文件地图补两个面板。
- 本 SPEC §8 回填门禁数字与负控红绿。

## 6. 边界与未决（缺的后端端点契约，本工作流不实现）

**结论：账户级账本视图需要 `control.py` 新端点，`control.py` 是禁改文件 → 只交付契约。**

建议端点（供后续后端工作流走独立 PR，届时 UI 直接消费）：

```
method: budget.view
params: { run_id?: string }        # 缺省 = 全部账户
result: {
  accounts: [{
    account_id: string             # "run:<run_id>" 等，与 budget.create_account 一致
    run_id: string | null
    label: string
    cancelled: bool                # budget.cancel 已发生
    bound: "estimate" | "upper"    # 预占口径（PRD §5.6 第 3 条）
    limits: { <dimension>: int }   # 实际设闸的维度；空 = 默认记账不设闸
    counters: { <dimension>: { reserved: int, settled: int } }
    pending: [{ reservation_id, resource, quantities, created_at }]   # 未决占用（未知用量）
    parent_id: string | null       # 父子链（子任务账户指向根 Run 账户）
  }]
}
语义：只读；数据来自 BudgetLedger.account()/usage()/pending()，不重算、不清零
（reconcile() 只报告的既有纪律保持）。
```

UI 侧对应留白（当前不展示、不编造）：账户上限设置、未决预占清单、下载字节维度、
子任务数量维度的账本计数、`overrun` 标志。面板文案不暗示"看到的就是账本全部"。

其他边界：账本无控制面写端点 → UI 不提供任何预算调整操作；子任务只读 / 深度 1 /
取消不退款是后端既有事实，面板如实展示状态，不承诺更多。

## 7. 高危判定

纯前端展示组件 + 纯函数选择器 + demo 夹具；不碰授权 / 租约 / 并发写 / 事件日志 /
计费的任何代码路径（`src/tianshu/**` 零改动）→ **非高危**，不触发 CONTRIBUTING §7
强制独立复审；仍按 §3 判据逐条验收 + §4 负控实跑。

## 8. 实测回填（2026-11-14，本会话填）

**实现规模**：新增 `ui/src/lib/budgetView.ts`（151 行）、`ui/src/lib/subtasks.ts`（171 行）、
`ui/src/components/BudgetPanel.tsx`、`ui/src/components/SubtaskPanel.tsx`、测试 3 个文件；
修改 `App.tsx`（传 `events`/`runs` 两个 prop）、`RunDetail.tsx`（挂两个面板）、
`lib/humanize.ts`（`SUBTASK_STATE_LABELS` 7 态、`BUDGET_REFUSAL_LABELS` 2 条、
`subtask.spawned`/`subtask.finished` 两类句子、`ACTION_LABELS` +2）、`lib/format.ts`
（`formatInt` 确定性千分位，避免 `toLocaleString` 的环境差异）、`demo/world.ts`
（demo Run 补 `limits` / `usage` / 子任务两个事件——只存在于 `?demo=1`）、
`styles.css`（面板样式段）、`ui/README.md`（结构表 / 限制 / 文件地图）。
**后端 `src/tianshu/**` 零改动**（`git status` 核对）。

**前端检查（CONTRIBUTING §5 四条命令，全部实跑）**：

| 命令 | 结果 |
|---|---|
| `pnpm --dir ui install --frozen-lockfile` | exit 0（锁文件未动） |
| `pnpm --dir ui run typecheck` | exit 0 |
| `pnpm --dir ui run test` | **16 文件 / 123 条全绿**（基线 13 文件 / 91 条 + 新增 3 文件 27 条 + humanize 4 条 + demo 接线 1 条） |
| `pnpm --dir ui run build` | exit 0；`ui/dist` 产物更新（旧 hash 资产删除、新 hash 资产与 `index.html` 随 PR 提交） |

**判据覆盖**：J1–J12 逐条由新增测试钉住（J1/J2 `subtasks.test.ts`、J3/J4/J5/J6/J7/J9
`budgetView.test.ts`、J7/J8/J10 `run-panels.test.tsx`、J11 `humanize.test.ts` +4、
J12 `run-mode.test.tsx` demo 接线测试）；J13 = 上表 test 行；J14 见下 pytest 行。

**负控三条（全部实跑；变异只在本地，不进提交）**：

| # | 变异 | 红的测试（实跑输出） | 还原后 |
|---|---|---|---|
| NC1 | `budgetView.ts` `hasExplicitUsage` 改为恒真（usage 缺失按 0 处理） | `budgetView.test.ts > 用量口径（J3/J5） > token 只累计显式整数 usage；缺失 usage 的调用进未知计数，不当 0`、`run-panels.test.tsx > 限额、用量、费用与未知口径一起出现（J5/J6）` —— 2 red | 全绿（19 passed 定向 + 套件 123） |
| NC2 | `BUDGET_REFUSAL_RULE_IDS` 删掉 `dispatch.budget_refused` | `budgetView.test.ts > budget_refused / subtask_limit 两条编号都进拒绝记录并带 reason`、`budgetView.test.ts > 拒绝记录按 seq 升序`、`run-panels.test.tsx > C4 拒绝编号渲染成人话` —— 3 red | 全绿（19 passed 定向） |
| NC3 | `subtasks.ts` spawned 行 `finishedState` 初始化为 `"succeeded"`（未收尾冒充终态） | `subtasks.test.ts > 只有 spawned（未 finished）= 进行中`、`subtasks.test.ts > 多个子任务按派发顺序排列`、`run-panels.test.tsx > 进行中的子任务显示进行中与限额` —— 3 red | 全绿（套件 123 passed） |

还原后核对两个源文件无残留变异标记（grep `负控变异` = 0）。

**Python 门禁**：`uv lock --check` exit 0（8 packages resolved）；`uv run --frozen ruff
check .` exit 0（All checks passed!）；`uv run --frozen pytest -o addopts="" -q` 全量
**3199 passed / 8 skipped（545.59s，macOS arm64 本机）**——与 KNOWLEDGE §2 记录的
main 基线数字（3199 passed / 8 skipped）一致，后端零改动成立（J14 ✓）。

**未决（原样保留）**：账户级账本视图需要 `control.py` 新端点（§6 契约），本 PR 不实现；
demo 世界不实现 `memory.*`（既有缺口，与本工作流无关）。

**真实页面验证（demo 模式，`?demo=1`，本机静态服务器 + 浏览器）**：翻译任务（成功 Run）
两个面板都渲染——预算面板显示「轮次上限 / Token 上限 / 已知费用 $0.07（精确
$0.069800）」（= 0.0412 + 0.0286，与事件 cost 一致），子任务面板显示「限额：轮次 2 ·
token 3,000 · 期限 300s / 用量：轮次 2 · token 1,842」，时间线出现「派发了子任务（轮次
≤ 2 · token ≤ 3000）」「子任务完成（轮次 2 · token 1842）」人话；失败 Run 显示拒绝记录
「委派限额不满足 + 根任务累计子任务数已达上限 4」与时间线「未获放行：委派限额不满足」；
无 usage 的种子 Run 显示「用量未知不当作 0」，不显示 0。截图已核对布局无破版。
