# 共享预算账本（PRD §5.6 的落地与边界）

- 立据：`docs/PRD-v1.md` §5.6；施工规格 `docs/SPEC-G1-ADMISSION-2026-11-11.md` §3。
- 状态：账本与主循环接线**已实现**（`src/tianshu/budget.py` + `loop.py` +
  `control.py`）；**默认未启用**（见 §5），这是一条明确登记，不是"忘了打开"。
- 判据：`tests/test_budget_ledger.py`（20 条）。负控 9 项逐条实测变红（见 §7）。

## 1 为什么单 Run 的累加计数不够

`loop._Progress` 的 `tokens` / `cost_total_micros` 是**事后**记账：只知道"已经花了
多少"，回答不了"现在还能不能发下一个请求"。三种情况下它会给出错误答案：

| 情形 | 事后计数的错误 | 账本的做法 |
|---|---|---|
| 两个请求同时读到最后一份余额 | 都以为自己是最后一个 → 花超 | 发送前一次原子预占，额度不足直接拒绝 |
| 请求发出后进程崩了 | 账本上什么都没记 → 用户以为没花钱 | 预占已落库；`reconcile()` 报告未结清项 |
| provider 断流、没有 usage | 记 0 或干脆不记，两者都在撒谎 | 维度记未知、占用**不释放**、不记零 |

它不是 `_Progress` 的替代：`_Progress` 仍负责展示`已知费用`；账本是它前面的闸。

## 2 数据模型

建在**事件库同一个 SQLite 文件**里（与 §5.3「单提交点」一致，不制造第二个事实源）：

| 表 | 作用 |
|---|---|
| `budget_accounts` | 账户：`parent_id`（父子）、`cancelled`、`deadline_ms`、`run_id` |
| `budget_limits` | 每账户每维度的上限（只对声明过的维度设闸） |
| `budget_counters` | `reserved`（未结清占用，含未知）+ `settled`（已结清已知用量） |
| `budget_reservations` | 每次真实 attempt 一行；唯一键 `(account_id, attempt_id)` |

维度是封闭集：`tokens` / `micros`（微美元）/ `requests` / `bytes` / `subtasks`。
截止时间不是"花了多少"，所以放在账户列 `deadline_ms`，不混进维度。

**父账户的计数器本身就是子树聚合**：预占沿 `parent_id` 链逐级扣，所以父看到的花费
天然含子账户；反过来对后代再求和会把同一笔算两次（PRD §5.6 第 2 条）。

## 3 七条规则 → 代码

| PRD §5.6 | 实现 |
|---|---|
| 1 发送前原子预占；attempt 独立身份；幂等结算 | `reserve()` 在 `BEGIN IMMEDIATE` 内"检查 + 插入 + 扣减"；`attempt_id` 唯一；`settle()` 第二次调用只回报、不改账 |
| 2 子任务同时受根与局部上限约束；父汇总不二次计入 | `reserve()` 沿 `_chain()` 逐级校验并逐级扣；结算只发生一次 |
| 3 多维度记账；估算不冒充上界 | `DIMENSIONS`；`estimate_tokens()` 注释写明是 4 字符≈1 token 的本地估算 |
| 4 未知用量保留未决占用，不按零结算 | `settle(known=False)` → 状态 `unknown`，`reserved` 不变 |
| 5 预算耗尽只保存状态并通知，不再调用模型 | 预占失败抛 `BudgetRefused`，`_complete_with_retry` 记 `model.error` 后直接返回，不重试、不调用 |
| 6 deadline / 取消传递；不把取消当退款 | `deadline_ms` 在预占时校验；`cancel()` 沿子树标记；已发出请求保持原状 |
| 7 等待时不持写事务；恢复先核对预占 | 事务只包住预占/结算；`reconcile()` 只报告，不清零、不猜测 |

### 分维度结算（`partial`）

token 有 usage 但价格查不到时，不能把"金额未知"抹成 0，也不能因此把"token 已知"
一起拖成未知。`settle(..., known_dimensions={...})` 按维度结清：已知的释放并入账，
未知的继续占用，状态记 `partial`。

## 4 主循环怎么用

`Limits.budget_limits = {"tokens": N, "requests": M, ...}` 打开账本。每次模型
attempt：

1. `_reserve_attempt()`：输入字符 → 估算 + 25% 余量 + `output_reserve_tokens`
   预留 → 原子预占（`attempt_id = <request_id>#<attempt>`）；
2. 预占失败（`BudgetRefused` / `BudgetCancelled`）→ 记 `model.error`（`retryable=False`）
   → Run 停止，**一次模型调用都不会发生**；
3. 成功拿到回执 → `_settle_attempt()`：`requests` 已知；有 usage 则 `tokens` 已知；
   查得到价格则 `micros` 已知；其余留给 `reconcile()`；
4. 模型抛错（传输 / 超时）→ `requests` 已知、token / 金额未知，占用保留。

控制面 `run.cancel` 会 `budget.cancel("run:<run_id>")`：之后不再预占，也就不会再有
新请求；已发出的不退款。

## 5 默认为什么是关的（明确登记）

`Limits.budget_limits` 为 `None` 时行为与之前**完全一致**（只有事后计数）。原因：

- 预占用的是**估算**（PRD §5.7：估算不是保证）。默认打开会让"临界 run"提前被拒，
  这是行为变更，需要单独评审（例如：余量比例、输出预留值、估算偏低时怎么重试）；
- 当前还没有并发委派 / 网络计费（G2），共享账本暂时没有真实竞争对象；
- 默认打开前要先把"拒绝时用户看到什么"想清楚（现在只有 Run 失败原因与账本状态）。

**这不是"已经满足 §5.6 的发布前置条件"**：PRD 说账本是"网络计费 / 子代理并发前的
硬性前置条件"，接入 G2 时必须默认打开，并把 `budget` 摘要接进 UI / `state`。

## 6 未做 / 不成立的部分

| 缺口 | 现状 |
|---|---|
| 默认启用 + UI 展示（已知费用 vs 未知用量分别显示） | 未做；账本 API 有 `usage()` 材料 |
| 子任务账户（`subtasks` 维度的真实父子） | 账本支持父子链与 `subtasks` 维度；没有子代理，所以没有真实调用方 |
| 网络下载字节 / MCP 计费 | `bytes` / `micros` 维度在账本里，但没有外发能力，没有真实数据 |
| 跨**进程**并发 | 测试用两条独立 SQLite 连接模拟；没有多进程实测（本机单 daemon 场景足够，但别写成已证） |
| 硬上限模式的"可验证上界" | 未做：估算不是上界，`budget_limits` 只保证"不因我们主动超发而超额"，不保证 provider 侧绝不超 |
| Windows | 未在 Windows runner 上单独验证账本（随全量测试跑；命名管道问题 issue #50 无关） |

## 7 负控（回退即红，实测 9/9）

| 回退 | 变红的测试 |
|---|---|
| 未知结算按零（释放全部维度） | `test_unknown_settlement_keeps_occupation` / `test_partial_settlement_keeps_only_unknown_dimensions` |
| 预占不沿父链 | `test_parent_limit_binds_child` |
| 取消不传播到后代 | `test_cancel_propagates_to_descendants` |
| 结算不幂等 | `test_settlement_is_idempotent` |
| 预占不认 attempt 身份 | `test_reserve_is_idempotent_per_attempt` |
| deadline 不检查 | `test_deadline_refuses_new_reservations` |
| 主循环不预占 | `test_budget_exhaustion_stops_before_any_model_call` |
| 主循环不结算 | `test_run_settles_actual_usage_into_the_ledger` / `test_model_without_usage_leaves_unknown_occupation` |
| 控制面取消不传递 | `test_control_plane_cancel_propagates_to_the_ledger` |

并发用例 `test_concurrent_last_unit_is_admitted_once` 的负控是去掉预占的
`BEGIN IMMEDIATE`（读-改-写不再互斥）：实测 **5/5 次变红**（两条连接都读到
`used=0`，各扣一份 → 占用 12 > 上限 10）。这条不比其它负控弱，一并登记。
