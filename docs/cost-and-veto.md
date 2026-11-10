# 费用口径（#58）+ 两条静态疑点销账（#53）+ #40 复核

- 工作流：T5 · 分支 `fix/cost-and-veto-scope` · worktree `/Volumes/Date/ts-wt-cost`
- 基线：`main @ 540c543`（PR #62 之后，全量 `1080 passed / 3 skipped`）
- 判定口径：**有回归测试、且做过负控（把修复回退后测试会红）才算修好**；
  读不懂 ≠ 没写过；判定不了 = unknown；校验器自己崩了不算回答。

## 0. 结论摘要

| 项 | 判定 | 证据 |
|---|---|---|
| **#58** CLI 费用行在「全部模型尝试都失败」时说「无（没有模型调用）」 | **已修** | 代码 + `tests/test_cost_line.py`（10 条）+ 两条负控（§1.4）+ 真黑洞端点实跑（§1.5） |

- 未改 `src/tianshu/tasks.py`：尝试次数进的是游标里自由形状的 `budget_ledger`
  （与 `tokens` / 成本同源），不需要动 `RunCursor` 的字段契约。
- #53 两条静态疑点的销账与 #40 复核见后续提交（会把对应结论补进本文件的
  §0 表与 §2–§5）。

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

