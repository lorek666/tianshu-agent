# 费用口径（#58）+ 两条静态疑点销账（#53）+ #40 复核

- 工作流：T5 · 分支 `fix/cost-and-veto-scope` · worktree `/Volumes/Date/ts-wt-cost`
- 基线：`main @ 540c543`（PR #62 之后，全量 `1080 passed / 3 skipped`）
- 判定口径：**有回归测试、且做过负控（把修复回退后测试会红）才算修好**；
  读不懂 ≠ 没写过；判定不了 = unknown；校验器自己崩了不算回答。

## 0. 结论摘要

| 项 | 判定 | 证据 |
|---|---|---|
| **#58** CLI 费用行在「全部模型尝试都失败」时说「无（没有模型调用）」 | **已修** | 代码 + `tests/test_cost_line.py`（10 条）+ 两条负控（§1.4）+ 真黑洞端点实跑（§1.5） |
| **#53 疑点 1** 否决缓存只按 `run_id`，换 Run 会不会忘掉否决 | **不成立**（不是漏洞）；作用域前提已写明并用三层测试钉住 | `tests/test_veto_scope.py`（§2） |
| **#53 疑点 2-A** 撤销后同一个 approval 不能再被派发消费 | **成立** | `test_revoked_approval_cannot_be_consumed_again`（§3.1） |
| **#53 疑点 2-B** `execution.started` 之后撤销必须失败 | **成立** | `test_revoke_after_execution_started_is_refused`（§3.2） |
| **#53 疑点 2-C** 撤销之后新审批可以重新走 | **成立** | `test_revocation_allows_a_fresh_approval_in_the_same_run`（§3.3） |
| **#40** 审批被否决后不再重复请求 | **可关闭** | `tests/test_approval_reject.py` 9/9 通过（§4） |

- 未改 `src/tianshu/dispatcher.py`（T2 独占）：疑点 2 三条探针都证明**现状成立**，
  不需要改；唯一相邻的小口径问题记在 §3.4，交 lead 决定是否另开工单。
- 未改 `src/tianshu/tasks.py`：尝试次数进的是游标里自由形状的 `budget_ledger`
  （与 `tokens` / 成本同源），不需要动 `RunCursor` 的字段契约。

---

## 1. #58：费用行按「尝试次数」说话，不按「完成轮次」

### 1.1 现象与根因

黑洞端点（accept 后不回包）下，`max_model_attempts=3` × `timeout=60s` 全部
走完，Run 落 `failed`，CLI 却打印：

```text
outcome: failed  (模型调用 3 次都失败：ModelError(kind=timeout, retryable=True): 请求超时：timed out)
turns  : 0   tokens: 0
费用   : 无（没有模型调用）        # ← 假话：调了 3 次，只是都没成功
```

根因两处：

1. `RunResult` 没有"模型尝试次数"字段，只有 `turns`（**完成的轮次**）；
2. `cli._print_result` 把 `result.turns` 传给 `format_cost(..., calls=)`，
   而 `format_cost` 在 `calls <= 0` 时固定打印「无（没有模型调用）」。
   `turns == 0` 恰好是"全部尝试失败"的形状——最需要如实报告的情况被印成
   最不关己的一句话。

### 1.2 修法

- `loop.RunResult.model_attempts`：**每真正发出一次 `complete()` 请求 +1**，
  含超时/传输失败/重试；预算守卫提前返回、准入拒绝、上下文编译失败都不算
  （它们没有"打过一次"）。
- `_Progress.model_attempts` 进游标 `budget_ledger`（与 `tokens`、成本同源），
  `resume` 之后从账本读回、继续累加。
- `model_attempts_known` 一起进账本：旧游标没有 `model_attempts` 键 → 以
  `turns` 为**下限**（每次成功至少一次尝试）并标 `known=False`；展示层据此说
  "不知道"，不把下限当精确值。它同时出现在 `RunResult`、`run --json`、
  `run status --json`（`null` = 不知道）。
- `cli._cost_note(...)` 成为费用行的唯一口径（`_print_result` 与 `run status`
  共用），四种情况：
  - `attempts == 0` → 「无（没有模型调用）」——只有真的没发过才允许；
  - `attempts is None`（旧游标）→ 不知道就不编数：`turns == 0` 才说"无"
    （旧游标 `turns == 0` 只可能写于第一次模型调用之前），`turns > 0` 按完成
    轮次报，不提"尝试"；
  - `turns == 0 && attempts > 0` → 「0 个完成轮次 / N 次尝试（费用未知）」；
  - `turns > 0` → 已完成轮次的账照旧报；未完成的尝试单独标
    「+ 未知（K 次尝试未完成，共 N 次尝试）」——失败尝试可能已被对方计费，
    但本表不认没拿到的价，所以记"未知"而不是 0。
- `--json`：`run --json` 的 run 对象自动带 `model_attempts`；
  `run status --json` 增加 `model_attempts`（旧游标为 `null` = 不知道）。

### 1.3 回归测试（`tests/test_cost_line.py`，10 条）

| 测试 | 钉住什么 |
|---|---|
| `test_all_attempts_failed_counts_every_attempt` | 3 次全超时 → `turns=0`/`attempts=3`；输出**不含**「没有模型调用」，含「0 个完成轮次 / 3 次尝试（费用未知）」；`--json` 带 `model_attempts` |
| `test_attempts_counter_survives_non_retryable_failure` | 不可重试错误只试 1 次 → 数字是 1 不是 0 |
| `test_true_zero_calls_still_says_no_model_call` | `max_turns=0` 时真 0 次调用（0 条 `model.intent`）仍含该短语 |
| `test_retry_then_success_reports_three_attempts` | 两次超时后成功 → 轮次 1、尝试 3，输出含「共 3 次尝试」 |
| `test_attempts_persist_across_resume` | 等待审批前 3 次、resume 后 4 次；账本里 `model_attempts=3` |
| `test_cli_json_reports_model_attempts` | scripted 3 轮 → `run status --json` 的 `model_attempts == 3` |
| `test_run_json_has_model_attempts_key` | `run --json` 的 run 对象有该键 |
| `test_status_falls_back_honestly_for_legacy_cursor` | 删掉账本键模拟旧游标 → JSON 是 `null`，不是 0；文本费用行不提尝试次数 |
| `test_legacy_cursor_attempts_stay_unknown_after_resume` | `_Progress.of` 给下限 + `known=False`；resume 后账本里 known=False → status 仍报 `null` |
| `test_print_result_does_not_treat_a_legacy_lower_bound_as_exact` | `model_attempts_known=False` 时费用行不提"尝试"，也不说"没有模型调用" |

### 1.4 负控（回退修复后测试必须红）

**NC-1：把 `_print_result` 改回传 `result.turns`**（即 issue 的原始代码形状）：

```text
>       assert "没有模型调用" not in text, f"打了 3 次却说没调用：{text}"
E         费用   : 无（没有模型调用）
E       assert '没有模型调用' not in 'run_id : ... 无（没有模型调用）\n'
FAILED tests/test_cost_line.py::test_all_attempts_failed_counts_every_attempt
```

**NC-2：把 `progress.model_attempts += 1` 去掉**：

```text
>       assert result.model_attempts == 3, "每次真正发出请求都要 +1"
E       assert 0 == 3
FAILED tests/test_cost_line.py::test_all_attempts_failed_counts_every_attempt
```

两次负控之后都已恢复改动（`cmp` 校验备份一致）。

### 1.5 真黑洞端点实跑（不是注入）

用与 M4 §7.2 同一条命令形状（本机 macOS，2026-11-11）：

```bash
python3 blackhole.py 18777        # accept 之后不回包，让客户端等满超时
PYTHONPATH=<worktree>/src TIANSHU_API_KEY=dummy \
  TIANSHU_BASE_URL=http://127.0.0.1:18777/v1 \
  python -m tianshu run --task "把 a.txt 的内容总结成 summary.md" \
    --workspace /tmp/timeout-ws --out summary.md \
    --model openai-compatible --provider openai --model-name blackhole \
    --data-root /tmp/timeout-data
```

实测输出（3 × 60s 超时全部走完，约 3 分钟）：

```text
run_id : run_cf8fb5d32c7b
state  : failed
outcome: failed  (模型调用 3 次都失败：ModelError(kind=timeout, retryable=True): 请求超时：timed out)
效果   : none_applied（applied=0 unknown=0）
turns  : 0   tokens: 0
费用   : 0 个完成轮次 / 3 次尝试（费用未知）
replay : 通过  (15 事件 / 3 次消息校验)
EXIT=1
```

同一个黑洞端点，修复前打印的是「无（没有模型调用）」（见 M4 §7.2 的历史
观察块）；修复后如实说"0 完成轮次 / 3 次尝试（费用未知）"，且 replay 仍然通过。

---

## 2. #53 疑点 1：否决缓存只按 `run_id` —— **不成立**，作用域前提已写明

### 2.1 问题的提法

否决缓存（`ApprovalStore.rejected_for`）在 SQL 里按 `run_id` 过滤：同一 Task
起新 Run 会忘掉上一次的否决。**如果模型能触发 `run start`**，那就等于
"用户拒绝过这个动作，换个 Run 就能再问一次"。

### 2.2 事实：能启动 Run 的只有人/控制面

`grep -rn "start_run(" src/tianshu` 的全部调用点：

| 位置 | 谁触发 | 是模型能力吗 |
|---|---|---|
| `cli.py` 的 `run start` / 兼容入口 `run --task` | 人在终端执行 | 否 |
| `control.py::ControlPlane._run_start`（IPC 方法 `run.start`） | Host/UI 经**带 token 的本地 IPC** 调用 | 否（模型没有 socket/RPC 工具） |
| `dispatcher.py::open_control_run` | `undo` / `recover` 命令建的控制 Run | 否（CLI 子命令） |
| `loop.py::AgentLoop.start` | 被上面三者调用（CLI 兼容入口 / daemon 驱动线程） | 否（是驱动入口，不是模型能力） |

模型的全部能力是工具调用：`tool_manifest()` 只有 6 个工具
（`list_files / read_text / create_report / modify_text / delete_file /
run_command`），每个工具的 `action` 都在 C4 的封闭动作集
`{read, create, overwrite, delete, exec}` 里——**没有任何一项等于"新建 Run"**。
`tools.py` 运行时连 `TaskStore` 都拿不到（只从 `.tasks` 导入 `ExecutionPermit`
票据；`TaskStore` 只出现在 `TYPE_CHECKING` 块里）。

所以"换个 Run 再问一次"必然是人/控制面主动发起的新执行——那是人的决定，
不是模型把否决变回了待批门票。

### 2.3 写进契约的前提

已写进 `src/tianshu/permissions.py::ApprovalStore.rejected_for` 的 docstring
（并同步该实现里 `approval_subjects` 表注释）：

> 作用域：Run 是边界，语义键是 `(task, subject_digest)`。SQL 按 `run_id` 过滤
> 不构成绕过，因为能启动 Run 的只有人/控制面（CLI / `run.start` RPC / undo 的
> 控制 Run）；模型的能力面只有 `tool_manifest()` 里的工具。前提如果变了
> （出现模型可达的建 Run 通道），这里必须改成按 task 查询
> （`approval_subjects` 无 `task_id` 列，需 join `approvals.task_id` 或补列）。

### 2.4 三层测试钉子（`tests/test_veto_scope.py`）

1. `test_model_tool_surface_has_no_run_start`：模型可见工具 == `SPECS` 的
   `exposed` 投影；与 11 个控制面动作名（`run.start` / `start_run` /
   `task_create` / `approvals_decide` / `undo_apply` / …）不相交；每个工具的
   `action` 都在封闭动作集里。
2. `test_tools_module_cannot_construct_runs`：AST 检查 `tools.py` 的**运行期**
   import——从 `.tasks` 只拿 `ExecutionPermit`，且不依赖
   `control` / `cli` / `daemon` / `host`。结构上建不出 Run（就算将来加了新工具）。
3. `test_a_model_driven_run_never_starts_another_run`：活的探针——模型提议
   `run_command`（最高危可见工具，任务未开 exec → 被拒
   `c4.exec_not_configured`）后，`TaskStore.start_run` 只被 `engine.start`
   调用 1 次，`runs` 表仍只有 1 行。

**判定：不成立（按"已确认漏洞"口径）。**不是"没验证"，是显式的设计前提 +
可复现测试；`docs/KNOWLEDGE.md` §7 里那条"未逐条验证"的登记可以销账。

### 2.5 边界（如实写清）

`run_command`（exec）是模型可见的最高危工具，**默认全拒**
（`c4.exec_not_configured`，必须 `task create --allow-exec-cmd` 显式开启）。
若用户显式开启 exec 且策略允许调用 `tianshu` 可执行文件，模型原则上可以自己
起一个 CLI → 新 Run。这不是否决缓存的静默绕过，而是 **exec 本身就是"非沙箱的
最高危能力"**（PRD §6 / AGENTS §六：`isolation == "none"`）：用户放开它，
模型能做的事远超"重新请求一次审批"。该边界如实登记，不靠否决缓存去堵。

---

## 3. #53 疑点 2：`revoke_consumed` 三条路径逐条探针

探针脚本与 `tests/test_veto_scope.py` 使用同一套真实栈（`TaskStore` /
`LeaseStore` / `JobStore` / `ApprovalStore` / `GrantStore` / `Dispatcher` /
`SnapshotStore`），不手写替身；每条路径先跑探针看真实输出，再固化成测试。

### 3.1 路径 A：撤销后同一 approval 不能再被派发消费 —— **成立**

探针（intent 已提交、文件还没打开时撤销，与执行抢同一把守卫）：

```text
[A1 dispatch-after-revoke] ('rejected', 'none_applied', 'dispatch.not_admitted')
[A2 approval] revoked
[A3 file] b'原始内容\n'
[A4 intents] ['c1']
[A6 direct consume raised] ApprovalError: 审批不可消费：appr_...（未批准、已消费、已撤销或已过期）
[A7 re-dispatch] ('rejected', 'none_applied', ..., '覆盖已有内容是删除类操作')
[A8 file after re-dispatch] b'原始内容\n'
[A9 approval after re-dispatch] revoked
[A10 intents after re-dispatch] 1
```

- approval 层的唯一保证是 `consume()` 的条件 UPDATE（`state='approved'`），
  已撤销的票直接 `ApprovalError`；
- 派发层再派发同一请求也只回 `rejected/none_applied`：**没有第二次
  `tool.intent`**，文件一个字节没动；
- `dispatch_rejected` 事件与 `dispatches.state=revoked` 都已落库（上一轮
  `test_dispatch.py::test_revoke_between_intent_and_open` 已覆盖守卫内复核，
  这里补的是"撤销之后还能不能再消费"）。

测试：`test_revoked_approval_cannot_be_consumed_again`。

### 3.2 路径 B：越过 `execution.started` 之后撤销必须失败 —— **成立**

探针把派发记录推进到执行器真正写之前会写的 `executing`（真实的
`begin_execution` 已经写下 `execution.started` 事件），然后撤销：

```text
[B1b execution.started] 1
[B2 revoke raised] ApprovalError: 这次调用已经开始执行（state=executing），撤销不承诺回滚；请用 recover 核查实际效果
[B3 approval] consumed
[B4 file] b'改后的内容\n'
```

- `revoke()` 的判定是"dispatch 状态必须是 `intent`"：越过 `execution.started`
  之后（executing / done）一律 `ApprovalError`，**不会**把 approval 改成
  revoked 来假装成功；
- 撤销与执行抢同一把跨进程执行守卫（`leases.execution_guard`），所以不存在
  "撤销赢了一半"的窗口：要么撤销先拿到守卫把 intent 作废，要么执行先拿到守卫、
  撤销明确失败。

既有 `tests/test_dispatch.py::test_revoke_after_execution_started_is_refused`
覆盖"执行已经跑完（state=done）"；本轮测试补"停在 execution.started 之后、
结论还没落库"的 in-flight 窗口。测试：
`test_revoke_after_execution_started_is_refused`。

### 3.3 路径 C：撤销之后新审批可以重新走 —— **成立**

```text
[A2 approval] revoked
[C1 new prepare] ('waiting', 'appr_...', True)      # 新票，不是被撤销那张
[C2 new dispatch] ('succeeded', 'applied')
[C3 file] b'重新发起的内容\n'
[C4 consumed] consumed
```

撤销作废的是**那一张票**，不是"这件事永久不能做"：同一个 Run 里重新发起
（新 call_id → 新请求号）会重新走审批，批准后正常执行。测试：
`test_revocation_allows_a_fresh_approval_in_the_same_run`。

### 3.4 相邻观察（不是疑点，不改 dispatcher.py）

对已经 `_abort_dispatch` 过的请求再调 `Dispatcher.dispatch()`，返回的
`tool.result.output.rule_id` 仍是**准备阶段**的判定编号（如 `c4.overwrite`
——语义是"需要审批"，不是"被拒"），而不是拒绝类编号：`_abort_dispatch` 把
请求标成 `rejected` 时没有更新 `dispatch_requests.rule_id`。

- 影响面：只有"直接调库再派发一次"的调用方看得到；主循环不会重复派发
  （它已经为同一 call_id 写过 `tool.result`），所以不影响用户可见语义；
- 不属于 #53 的两条宣称；`dispatcher.py` 属 T2，本轮**不改**，在此登记，
  由 lead 决定是否单独开工单（若要改：`_abort_dispatch` 里 `_set_request_state`
  应带上本次的 `rule_id`）。

---

## 4. #40 复核：否决后不再重复请求 —— **可关闭**

命令与结果：

```text
$ PYTHONPATH=<worktree>/src python -m pytest -o addopts="" -q tests/test_approval_reject.py
.........                                                                [100%]
9 passed in 0.48s
```

覆盖的语义（`tests/test_approval_reject.py`）：拒绝 → resume → Run 以 `failed`
收束且 `approval.requested` 恰好 1 次；换 call_id 重发仍命中否决；改参数可重新
审批；改目标旧否决失效；`c4.approval_rejected` 真实出现在 `dispatch.rejected`
与 `tool.result`；文件零改动。**结论：#40 可关闭（由 lead 关，本工作流不自己关）。**

---

## 5. 未做 / 边界

1. **Windows 实机**：按规格本轮明确不做；本工作流只在 macOS 本机跑。
   新测试用 `tmp_path`，无 POSIX-only 假设。
2. **`docs/M4-verification.md`**：只改 §7.2 的费用口径（T6 改其它节）；
   若与 T6 撞车由 lead 解。
3. **旧游标**：`model_attempts` 是新增账本键。升级前的旧游标没有它 → 以
   `turns` 为下限、`run status --json` 报 `null`（不知道）；不编造升级前的
   尝试次数。文档已如实写明。
4. **`format_cost` 本身未改**：它的语义仍是"有多少次调用的账已落库"（完成
   轮次），新增的 `_cost_note` 负责把"尝试次数"翻译给用户；`tests/test_pricing.py`
   对 `format_cost` 的直接断言因此保持不变。
5. **`dispatcher.py` 未改**（疑点 2 三条都成立；§3.4 的相邻口径观察交 lead）。