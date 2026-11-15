# SPEC · state 响应 8MiB 上界（W3）

> 日期：2026-11-15 · 分支：`feat/state-response-bound` · 基线：`main@0c5bb71`
> 路线图：[ROADMAP-COMPLETE-AGENT-2026-11-14.md](ROADMAP-COMPLETE-AGENT-2026-11-14.md) 工作流 W3
> 性质：**契约增量 + 行为修复**——`state` 响应新增三个可选布尔字段
> （`tasks_truncated` / `runs_truncated` / `approvals_truncated`），走
> `docs(contract)` 单独 commit；其余是 `control.py` 行为修复。
> 冲突面核查（2026-11-15）：在途 PR #140 / #141（纯 docs）、#161（maintenance /
> events / data_cli / net / migrate / restore*）、#162（desktop / release）均不触碰
> `src/tianshu/control.py`、`tests/test_control.py`、`tests/test_daemon.py` 与
> `docs/M5-desktop.md`；本工作流只改这些文件与 docs。
> 关联登记：KNOWLEDGE §7「指标」条「`state` 响应可超 IPC 8MiB（`control.py` 所有，
> 已登记未修）」；[SPEC-METRICS-ROTATION-2026-11-14.md](SPEC-METRICS-ROTATION-2026-11-14.md)
> 明确把它留作另行工单——即本工单。

## 1. 现状（代码事实，修复前）

- `ipc.py:88` `MAX_MESSAGE_BYTES = 8MiB`；发送侧 `encode()`（417–418）编码超限
  抛 `ProtocolError`。daemon 的响应不是主循环同步发送的：`_ClientConnection`
  有界队列 + 每连接发送线程，`_send_loop` 里 `raw.send(frame)` 抛
  `ProtocolError`（`ipc.IpcError` 子类）会被
  `except (OSError, ValueError, ipc.IpcError)` 捕获 → `self.drop("send_error")`
  → **整条连接被静默断开**，客户端拿到的是
  `ConnectionClosed: 对端在消息结束前关闭`（EOF），不是任何 typed 错误。
- `control.py` `_state()`（修复前 :311）组装响应无任何体积核算：
  `tasks` = 50 条（`DEFAULT_TASK_LIMIT`，`task_view` 带**无上界的 goal 全文**）+
  `runs`（全部非终态 + 20 终态）+ `approvals`（pending 全量）+ `event_seq_head`
  + `daemon`。goal 没有长度校验（`tasks.create_task` 只查非空；daemon 配了
  `blob_dir`，>256KB 的事件 payload 落外部 blob，task.create 本身能成功），
  所以「50 个多 MB goal 的任务」可以把 `state` 响应顶过 8MiB。
- 两个失控点都在复现测试里证实了（见 §5）：
  (a) **响应体超限**：50 × ~313KB goal → 响应 14.4MB → `encode()` 抛
      `ProtocolError` → 发送线程 drop 连接 → state RPC 每次失败，UI 首屏永久坏。
  (b) **组装过程失控**：`_approval_context()`（修复前 :593）每次
  `read_types(["approval.requested"])` **全表扫整份事件日志**（还逐条 SHA256
  验签）——连早已决定的审批也重读。state 与 approvals.list 的内存/CPU 随全部
  历史线性涨，虽然这些数据并不进响应体。

## 2. 目标与非目标

**目标**
1. `state` 响应（result 部分）在预算内组装，永不超过一帧 IPC 上限；
2. 放不下时**按条停**并置截断标记（UI 得到「列表不完整」信号，可再拉分页）；
3. **单条目自身超预算 → 类型化 `ControlError`（fail loud）**：静默丢条目等于
   UI 谎报「这个任务不存在」，截断 goal 文本又是偷改字段语义——v1 两条路都堵死；
4. `_approval_context` 改有界读：读取量只随 **pending 集合所在 Run** 走，
   与事件日志总历史无关；
5. 契约形状不删不改语义：M5 §3.4 五键原样保留，新增字段只增不改。

**非目标**
- 不给 `task.list` / `run.list` / `approvals.list` 加体积预算（它们有条数上限：
  `MAX_TASK_LIMIT=200` / 历史全在 / pending 全量；同样的 goal 对抗数据也能撑大
  `task.list` 响应，登记为未决问题 §6）；
- 不做 goal 输入侧长度上限（那是 PRD/契约层的独立决策）；
- 不改 `ipc.py`、`events.py`、`daemon.py`（在途 PR 占用面 + 最小改动面）；
- 不做单条文本截断（改字段语义）。

## 3. 设计

### 3.1 预算与份额

- `STATE_RESULT_BUDGET_BYTES = MAX_MESSAGE_BYTES // 2`（4 MiB）。响应外层信封
  （`{"id","ok","result":…}`）+ 节名 + 数组语法只有几百字节，2× 余量足以盖住
  「逐条估算与真实整帧编码」的差异；即使估算系统性偏大几 KB，真实帧仍远低于
  8MiB。**真正的帧上限检查仍在 `ipc.encode`**，预算是控制面的第一道闸。
- 三节份额：tasks 1/2、runs 1/4、approvals 1/4。理由：goal 是 state 里唯一的
  无界自由文本字段，大头给 tasks；`RunView` / `ApprovalView` 字段天生有界
  （id/枚举/计数/时间戳 + 有界的 reason/resources/isolation），1/4 各自足够。

### 3.2 装填算法（两趟）

```
第一趟（按契约顺序 tasks → runs → approvals）：
    allowance_i = budget × share_i + donated        # 前面节没用完的顺延进来
    在 allowance 内逐条装填；装不下就停，本节置 truncated
    donated += allowance_i − spent_i
第二趟（只对第一趟被截断的节，按原顺序）：
    leftover = budget − 第一趟总花费
    用 leftover 继续装填，直到用尽或装完
```

- **逐条度量**：`_json_encoded_size()` 与 `ipc.encode` 同参序列化
  （`ensure_ascii=False`、紧凑分隔符）取字节数；视图逐条构建、逐条度量，
  超额即停，不做 O(n²) 的整响应重编码。
- **fail loud 判定用整份预算，不用份额**：单条 > 4 MiB 才抛
  `ControlError("internal")`（错误码封闭集 §3.2 的 internal）。单条 ≤ 4 MiB 但
  超过本节份额的，第一趟先跳过、第二趟由顺延预算兜底——「tasks 只有一条大
  goal、runs/approvals 全空」的场景里整份响应装得下它，不该为了份额把它藏起来。
- **截断语义**：`consumed < len(views)` 即该节 `*_truncated: true`。标记是
  「列表不完整」信号；条目文本一字不动，字段语义不变。

**不变量**
1. 装填后整份 result 的编码 ≤ 预算（每节花费 ≤ 自己的 allowance，两趟共用
   同一个 budget 池），加信封后必然 ≤ 8MiB 帧上限；
2. 任何条目要么逐字完整地出现在响应里，要么被计数标记覆盖；不存在第三种
   「部分文本」状态；
3. 单条 > 预算 → 调用方收到 typed `ControlError`，不会拿到缺条目的假列表。

### 3.3 `_approval_context` 有界读

- 旧实现：`read_types(["approval.requested"])`——跨 Run 全表扫 + 逐条验签。
- 新实现：入参 `(approval_ids, run_ids)`（调用方给出**这次需要补上下文的
  审批集合**与它们的 Run）；对每个 Run `read_run(run_id, verify=False)`，
  客户端过滤 `type == "approval.requested"` 且 `approval_id ∈ approval_ids`。
- 两个调用点：
  - `_list_approvals`：pending 审批集合（expired 已先清，pending 天然有界且
    它们的 Run 全部是非终态——本来就在 state 的 runs 列表里被 `_run_view`
    读过一轮）；
  - `_approvals_decide`：决定后的那张审批已不是 pending，显式点名它自己的
    `approval_id` + `run_id`，否则响应视图的 rule_id/isolation 会退化成空值
    （有专门测试钉住）。
- **不变量论证**：读取量 = Σ(目标审批所在 Run 的事件数)。state 本来就要对
  runs 列表里每个 Run 调 `read_run(verify=False)`（`_run_view` 的效果计数），
  所以新实现引入的额外读取量 ≤ 「pending 审批所在 Run 再读一遍」，没有引入
  任何**Run 之外**的读取量；与事件日志总长度、历史审批总数（含早已决定的）
  彻底解耦。`verify=False` 与 `_run_view` 同口径——视图路径不做逐条验签，
  完整性由 append 时落库的 payload_hash 与按需的验证路径保证。
- 取舍说明（为什么不做「近窗」）：`read_after(seq, limit)` 是按条数取窗、
  **不筛类型**，对抗数据下窗口内塞满大 payload 事件反而比全表扫
  `approval.requested`（小 payload）更贵；按 Run 精确取所需事件是唯一
  「既不碰 events.py、读取量又可论证」的方案。

## 4. 契约变更（docs(contract) 单独 commit）

- `docs/M5-desktop.md` §3.4 `state` 行：结果补三个可选布尔
  `tasks_truncated` / `runs_truncated` / `approvals_truncated`（加字段不升版本，
  旧客户端忽略不认识的键——ipc.py 顶部注释口径），并注明预算装填与
  单条超预算 → `internal`。
- PRD 无 state 响应字段级引用，不需要改（grep 无 `event_seq_head`/首屏命中）。

## 5. 测试与负控证据

复现测试（基线 `0c5bb71` 上实跑红）：

| 用例 | 红现象 |
|---|---|
| `test_state_response_with_adversarial_goals_stays_within_ipc_frame` | `ProtocolError: 消息 14419496 字节，超过上限 8388608`（14.4MB 响应） |
| `test_state_single_item_over_budget_fails_loud` | `Failed: DID NOT RAISE ControlError`（无界组装静默返回 5MB 单条） |
| `test_state_approval_context_does_not_rescan_full_history` | `AssertionError: state 不得全表扫 approval.requested，实际调用了 [['approval.requested']]`（300 张历史审批在库） |
| `test_state_has_exact_shape_and_millisecond_timestamps` | `AssertionError: Extra items in the right set: 'tasks_truncated' / 'runs_truncated' / 'approvals_truncated'`（state 缺新字段） |
| `test_state_rpc_survives_adversarial_task_goals`（daemon 端到端） | `ConnectionClosed: 连接已断开：对端在消息结束前关闭`（发送线程 drop 连接） |

负控（变异红 → 还原绿，还原以 SHA256 比对确认字节一致）：

- **NC1**（拆掉条数/预算守卫：`_state` 回到无界组装）：两条对抗用例红
  （`ProtocolError: 消息 14419622 字节，超过上限 8388608` /
  `Failed: DID NOT RAISE ControlError`）→ 还原后绿。
- **NC2**（拆掉 `_approval_context` 有界读：恢复
  `read_types(["approval.requested"])` 全表扫）：spy 用例红
  （`state 不得全表扫 approval.requested，实际调用了 [['approval.requested']]`）
  → 还原后绿。

修复后：`tests/test_control.py` 33 passed、
`tests/test_daemon.py + test_slow_subscriber.py + test_integration_m5.py`
42 passed（2026-11-15 本机 macOS arm64，Python 3.12）。

## 6. 边界与未决问题

- `task.list`（limit ≤ 200）与 `approvals.list`（pending 全量）没有体积预算：
  同样的对抗 goal 也能撑大它们的响应帧，发送线程同样会 drop 连接。W3 范围
  只钉 `state`（路线图条目原文）；这两条登记为后续工单候选。
- state 截断只发生在「单节花费顶到预算」时；正常数据集（哪怕 50 × 10KB goal）
  远低于 4 MiB，标记恒为 false，行为与旧实现无差别。
- 事件日志的 run 内读取（`read_run`）仍按 Run 全量载入 payload（`_run_view`
  既有行为）；`_approval_context` 不放大它，但也没有缩小它——Run 级事件读取
  的分页/惰性化属于 W15（保留期/GC）或独立工单。
- 平台陈述：以上行为验证在 macOS arm64 本机；未声称 Windows/Linux 实测
  （CI 矩阵会覆盖 Linux 与 Windows 的套件结果）。
