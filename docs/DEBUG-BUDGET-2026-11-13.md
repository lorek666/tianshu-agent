# 系统 debug：模型用量与预算结算

日期：2026-11-13。受影响基准：`ccff93e79cfeb93e47e5a35012fd92fae366e941`。
本记录是作者的可复现证据，不是独立批准；完整修复 SHA、第二读者亲跑结论和当前四项 CI 绑定在对应 PR 正文。

## 范围与契约

真实两种 adapter → C1 normalize_usage → 普通/摘要调用 → BudgetLedger 预占/结算。保留 PRD §5.1 / §5.6 的既定口径：未知不等于零，账本不自动退款，估算预占不冒充硬上界。没有更改 schema、计费策略、Python版本或依赖。

## DE-01：部分 usage 被错误结算为完整 tokens（P1，已确认缺陷）

- 受影响版本：基准 SHA；位置：`loop.py:_settle_attempt/_settle_summary/_absorb_summary_usage/_model_step`。
- 契约：PRD §5.1 未知用量为 None；§5.6 未知用量保留未决占用，不能按零结算。
- 触发：OpenAI `usage={prompt_tokens: 0}` 或 Anthropic `usage={input_tokens: 0}`；输出缺失。反向只报输出也同样有洞。
- 原始实际：`reserved.tokens=0`，`unknown_usage_calls=0`，缺失部分被 `(value or 0)` 当成零；随后可使用本应保留的预算。
- 期望：只有输入与输出都已知才结清 tokens；任一缺失标为未知调用，保留该次 token 预占；已知 requests 正常结算，已知 token 部分保留为统计下界。
- 最小修复：结算判断 `or`→`and`；未知判断 `and`→`or`；重试投影 `_last_usage` 只用完整 usage。
- 回归：`test_partial_provider_usage_keeps_token_occupation`（两个 adapter × 输入/输出），同时断言后续 5000 token 申请被拒；`test_partial_summary_usage_keeps_token_occupation`（相同四组合）走真实摘要调用入口。
- 红/绿：上述 8 项属于 `de-regression-red` 的 41 failed，修复后属于 `de-regression-green` 的 41 passed。
- 残余风险：估算模式预占本来不是硬上界；未知调用被保守占用，需现有对账流程，不改写成可自动退款。更广泛 provider 非法形状仍未穷举。

## DE-02：负数外部 usage 和非法账本结算可破坏预算（P1，已确认缺陷）

- 受影响版本：基准 SHA；位置：`model.py:normalize_usage`、`budget.py:BudgetLedger.settle`。
- 契约：C1 错误必须是 ModelError；预算计数是非负整数；不能凭负消费恢复额度。
- 触发：OpenAI prompt/completion `-100/-10`，Anthropic input/output 同值。
- 原始实际：真实 adapter → 主循环后 `settled.tokens=-110`，Run tokens 也为负；ledger 入口也接受负数、bool、小数。字符串在后续 overrun 比较抛 TypeError，未按领域错误 fail-closed。
- 期望：识别到的负 token 字段在 C1 抛 `ModelError(kind="protocol", retryable=False)`；账本所有五维结算独立验证非负整数；失败时父/子计数与 pending 预占保持原样。
- 最小修复：normalize_usage 两类已知字段拒绝负数；settle 在事务前拒绝负数/bool/非整数。未改变缺字段/非整数 provider usage 原先映射 None 的口径。
- 回归：`test_negative_usage_is_a_nonretryable_protocol_error`（两 adapter × 输入/输出/缓存读/缓存建）；`test_negative_provider_usage_cannot_refund_run_budget`（两 adapter × 输入/输出，检查 model.error 与预占）；`test_settlement_rejects_invalid_usage_without_changing_accounts`（五维 × 四非法值，检查父子账本与后续合法结算）。
- 红/绿：上述 32 项原实现真实失败，修复后通过；其中字符串测试原来失败于 TypeError，其余为未拒绝或错误账本结果，不把探针崩溃冒充产品断言。
- 残余风险：不会修改既有事件或账本历史。若数据库已有负计数，需要单独取证与人工处置；本修复拒绝新非法输入，没有偷偷洗白旧数据。

## 复现与回归

```sh
uv run --frozen pytest tests/test_model_contract.py tests/test_budget_ledger.py tests/test_summary_loop.py -k 'negative_usage_is or settlement_rejects_invalid or partial_provider_usage or negative_provider_usage or partial_summary_usage' -o addopts= -q
uv run --frozen pytest tests/test_budget_ledger.py tests/test_model_contract.py tests/test_summary_loop.py tests/test_summary_replay.py tests/test_summary_context.py tests/test_pricing.py tests/test_g1_admission.py -o addopts= -q -rs
```

本机 macOS 26.6.2 arm64 / Python3.12.13，临时数据根、隔离provider环境变量，不调用真实模型。与独立网络回归一起运行的正式红绿批次：**41 failed → 41 passed**（预算40项，网络1项）；十模块关联 **285 passed / 26.89s**。原始日志为 `/tmp/tianshu-debug-20261113/de-regression-{red,green}.{log,json}`、`de-focused-full.{log,json}`；JSON记录完整基准SHA与未提交工作区测试口径、命令、退出码与环境。

另外使用真实spawn子进程与Barrier(3)竞争预算10中的6，两个不同PID一成功一拒绝，最终reserved=6；原始探针 `audit-de/budget_process_probe.py` 与 `de-budget-process-final` 日志保留。这是并发证据，不把同进程两连接冒充多进程。

调用两adapter的测试用的是受控响应和真实适配器/主循环，不是实时服务商语义或实际模型效果验证。既有历史负账本未自动迁移、改写或洗白；若发现真实旧数据异常应单独取证。本轮没有修改用户数据。
