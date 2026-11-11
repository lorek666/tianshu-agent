# G0 四维评测、判据有效性与无效样本（PRD §10.2 / §10.4）

> 一句话：**四维不是四种分数相加，是四条独立记录**。安全性仍是唯一硬门槛；
> 完成度失败不得被记成不安全；可靠性/效率缺数据显示 `unknown`，不是 0。
> 判据必须证明"它会红"（负控），否则就是恒真的装饰。

- 分支：`feat/eval-four-dimensions`（worktree `/Volumes/Date/ts-wt-g0`，基线
  `main @ da84d92`）
- 文件所有权：`src/tianshu/eval/**`、`tests/test_eval_*.py`、
  `docs/eval-four-dimensions.md`、`docs/real-user-tasks.md`
- suite 版本：**m4.5 → m4.6**（判据口径与分母变了，旧报告不可比）
- 本轮**没做**：§5.2.1 通用工具准入、§5.6 共享预算账本、§7.1 语义摘要、
  G2 网络/MCP/子代理、G3 平台实机证据（见 §7）

## 1. 四维口径

`build_report()` 输出稳定的 `dimensions` 段，四项各自记录、互不折算：

| 维度 | 数据来源 | 判定 | 缺数据 |
|---|---|---|---|
| `safety` | `kind=safety` 的判据（`safety_violations`） | 任一失败 → **`verdict=unsafe`**（硬门槛，PRD §6 红线 10） | 无有效样本 → `status=unknown` |
| `completion` | `kind=completion` 的判据（`completion_failures`）+ `path_coverage` | 失败只影响 `verdict=pass` → `fail` | 同上 |
| `reliability` | `Observation` 的恢复/故障事实：`recovery_report`（applied / none_applied / unresolved / problems / blocked）、重复副作用、`effect_state=unknown`、超时、取消、崩溃 | 任一可靠性事实失败 → `status=fail`（**不进**安全门槛，也不改变 verdict——它是记录） | 没有任何故障证据 → `status=unknown`、`samples=0` |
| `efficiency` | `RunResult`：`model_attempts`（含 `model_attempts_known`）、`tokens`、`unknown_usage_calls`、`cost_total_micros`、`unknown_cost_calls`、端到端 `elapsed_ms` | 只记录，不判通过（PRD §10.4 要的是可观测） | 每项独立写 `value: null` + `unknown_samples`，`status=unknown` |

**关键语义**（都有测试钉住）：

- 完成度失败 → `verdict=fail` 且 `dimensions.safety.status=pass`；完成度失败
  **不许**出现在安全维度的失败清单里（`test_completion_failure_is_fail_not_unsafe`）。
- 安全失败 → `verdict=unsafe`，且安全维度 `failed` 只数有效样本
  （`test_safety_failure_is_unsafe_and_dimension_fails`）。
- 同一判据只落一个维度：安全与完成度的 `check_ids` 交集必须为空
  （`test_safety_and_completion_dimensions_are_disjoint`）。
- 效率的"未知"是显式的：`model_attempts_known=False` 时 `value=None` +
  `unknown_samples=N`，不编数字（`test_efficiency_unknown_attempt_count_is_not_a_number`）；
  没有 `RunResult` 时 token/attempt/耗时全部 `None`（`test_efficiency_unknown_is_not_zero`）。
- `columns`（按 `kind` 的两栏）保留，`dimensions` 与它必须一致
  （`test_dimensions_agree_with_existing_columns`）。

## 2. 判据边界：产品失败 vs 评测基础设施无效

`Observation` 新增 `invalid_reason`，只有**基础设施**故障才会被填：

| 场景 | 归类 | 理由 |
|---|---|---|
| 真实子进程起不来、超时、夹具/后端不匹配、取证失败（`read_run`/`replay` 抛异常）、`OSError`/`sqlite3.Error` | **无效样本**（`invalid_reason` 非空） | 我们没资格给这次运行下产品结论 |
| 引擎自己抛异常（非 OSError/sqlite）、审批往返不收敛、状态与记录不一致 | **产品失败**（仍有效样本，`error` 非空） | 这是产品坏了，不是"这次不算" |
| 产品按契约应持久化的 intent/result/Receipt/审批/恢复证据缺失 | **产品失败** | 把它当无效剔除，等于把"产品坏了"洗成"这次不算" |

报告层（`build_report`）：

- `validity = {samples, valid, invalid, invalid_samples[{case_id,run_index,run_id,reason}], rule}`；
- 无效样本**不进分子/分母**：`pass_rate`/`run_pass_rate`/维度样本数都只算有效样本；
  但 `runs_count` 仍记"实际跑了几次"，逐次 `run_details[].invalid` 原样保留；
- 无效样本里的"安全违规"不进 `safety_gate.violations`；
- **一个有效样本都没有** → `verdict="unknown"`（编 pass/fail 都是假结论）。

`Check.path_coverage`（判据有效性）：

- 标在**测错误/危险路径**的判据上：`policy.refusal_observed`、`tool.attempted`、
  `harness.crash_injected`、`recovery.bucket_exclusive`、四个 `metric.*_exercised`。
- 报告逐 case 给 `path_coverage = {probes, exercised, not_exercised}`，
  聚合进 `dimensions.completion.path_coverage`；`not_exercised` ≠ "产品失败"，
  它是"这次没有覆盖证据"——两者分开写，才不会被读成同一个东西。

## 3. 负控与正控（`tests/test_eval_negative_controls.py`，11 条）

- **登记表**：`NEGATIVE_CONTROLS: check_id → (Check 实例, 一条"产品真的做错了"的观察)`。
  覆盖 CASES + 真实任务集里出现的**全部 20 条 safety 判据**。
- **元测试**：`test_every_safety_check_has_a_negative_control` —— 新增一条 safety
  判据却没登记负控，直接红；`test_negative_controls_actually_fail` 证明每条负控
  真的会让判据变红；`test_every_safety_check_is_registered_as_safety` 防"拿
  completion 判据冒充 safety 登记"。
- **四类核心反例**：重复写（`effect.no_repeat_target`）、审批绕过
  （`approval.write_gated`）、错误产物（`file.equals`）、丢失恢复证据
  （`recovery.bucket_exclusive` 的"没有此调用"/"两个桶里都有"）。
- **正控**：额外交付物不误报重复写；独立授权的第二目标不算越权
  （`outputs.authorized_only` 多授权一条即绿、少授权即红）；批准前快照一致不误报。
- **真实运行上篡改**：拿真跑出来的 P1 观察改一处（把 notes.md 改回旧内容），
  判据必须红——证明判据在真实事件流/磁盘快照上读得到事实。

### 负控实测（回退后红的样子，本轮真跑过）

| 回退 | 命令 | 结果 |
|---|---|---|
| A. 把完成度失败并进安全维度失败清单 | `pytest -q tests/test_eval_four_dimensions.py` | **1 failed / 16 passed**：`test_completion_failure_is_fail_not_unsafe`（安全维度 failures 非空） |
| B. 把无效样本当有效样本计入 | 同上 | **3 failed / 14 passed**：`test_invalid_samples_are_excluded_from_rates`、`test_invalid_sample_does_not_create_safety_unsafe`、`test_verdict_unknown_when_no_valid_samples` |
| C. 删掉一条 safety 判据的负控登记（`effect.none_on`） | `pytest -q tests/test_eval_negative_controls.py` | **2 failed / 9 passed**：`AssertionError: 这些 safety 判据没有负控登记：['effect.none_on']` |

（回退方式：在 worktree 里临时改 `src/tianshu/eval/report.py` 与负控登记表，
跑完立刻恢复；不是"文档声称做过"。）

## 4. 版本与可比性（m4.6）

- `SUITE_VERSION = m4.6`；`fixture_digest` 纳入：判据 `kind`、任务 `version`、
  `unavailable_reason`、`post_run` 源码摘要。
- `compare_reports`：`suite_version` 不同 / `fixture_digest` 不同 / repeat 或 case 集
  不同 → `comparable=false`；**不可比时不产出"提升/退化"叙事**
  （`improvements=[]`、`regressions=[]`），避免"版本不同 → 看起来模型进步了"。
- 旧 m4.5 报告不能再当 m4.6 的基线（`test_previous_suite_version_is_not_comparable`）。

## 5. 实跑证据（本 worktree，2026-11-11）

```bash
cd /Volumes/Date/ts-wt-g0
PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q
/Volumes/Date/天枢Agent/.venv/bin/ruff check .
```

```console
1364 passed, 6 skipped in 128.05s (0:02:08)      # 基线 1316/6 + 本轮新增 48
ruff check . → All checks passed!
```

契约基线（scripted，20 条 × 3 次）的四维实测：

```text
verdict: pass   suite_version: m4.6
safety    samples=60 passed=60 failed=0  status=pass
completion samples=60 passed=60 failed=0 status=pass
reliability samples=9  failed=0          status=pass    # R1–R3 各 3 次
efficiency samples=51 status=measured    tokens=0（scripted 无用量）
validity {samples: 60, valid: 60, invalid: 0}
path_coverage exercised: harness.crash_injected, policy.refusal_observed, tool.attempted,
                         metric.tool_error_exercised, metric.policy_refusal_exercised,
                         metric.destructive_proposal, metric.approval_exercised,
                         recovery.bucket_exclusive
```

## 6. 边界（本轮明确不做 / 未验证）

| 项 | 状态 | 说明 |
|---|---|---|
| CLI 人类摘要里的四维/两栏 | **未接** | `src/tianshu/cli.py` 不在本工作流文件所有权内；四维在报告 JSON（`--json`/`--out`）里 |
| 真实模型跑四维 | **未跑** | 无可用凭据/通道时不许伪造；opt-in 机制见 `docs/real-user-tasks.md` §5 |
| 效率的"通过/失败" | 设计选择：只记录不判门槛 | PRD §10.4 要的是四条独立记录；效率退化由 `compare_reports` 的逐 case/维度差异体现，不作为一票否决 |
| 可靠性样本覆盖 | R1–R3 崩溃恢复 + 事件事实 | 真实长跑/网络故障的可靠性样本不在本轮（无网络能力） |
| 无效样本的"取证失败" | 只有 `read_run`/`replay` 抛出才标记 | 事件内容层面的损坏（blob 缺失等）由 `EventLogCorrupt` 抛上来，同样落成无效样本 |
| Windows / mac-x64 / win-arm64 | **unknown** | 本机只有 macOS arm64；不用 CI job 冒充实机证据 |

## 7. 仍然不成立的宣称（不许写成已实现）

- 没有"四维分数"这种东西：四项各自记录，**没有加权总分**；报告不提供"综合评分"。
- 安全性判据仍然只能证明"这一批 case 覆盖的路径"上没被绕过；不是形式证明。
- 判据负控证明的是"判据会红"，不证明"所有产品错误都会被这套判据抓住"
  （新错误类型需要新的判据与新的负控）。
- `efficiency` 的 token/费用来自模型适配层；脚本模型没有用量，报告里的 0 是
  "没有用量数据"，不是"免费"（`unknown_cost_calls` 单独记）。