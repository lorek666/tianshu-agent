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

## 独立复审后的第二轮修复（B1 / B2 / B3）

第二读者在 `e89a95d4c7e8f7196b38c51148e294d4c72bed9a` 确认原 40 项修复成立，另外找到两项 P1、一项 P2。以下是作者在 `codex/debug-budget-review` 的处理证据，**不是独立批准**；新提交仍须第二读者复跑并绑定完整 SHA。

### B1 · P1：显式金额限额没有接入真实模型调用

原实现普通/摘要预占 `need` 不含 `micros`，结算又把金额从已知维度中排除。两真实 adapter 设置 `budget_limits={"micros": 0}` 后仍发送一次请求，Run 的已知费用与账本不一致；默认未知价调用的账本还错误地全部结清。

修复普通、摘要和每次重试共同使用的金额预占接线。估算模式根据已有版本价格表、输入估算（含余量）及输出预留计算金额；完整实际费用随后进入同一父子账户链，不能二次汇总扣费。账户链任何金额上限耗尽都会在发送前拒绝。

未知金额的表示保持现有持久化格式，**没有把整数 `need_json` 改成 nullable**：

- 缺价或没有可验证金额上界，且自身/任意祖先设金额限额：执行前 `BudgetConfigError`，一次请求也不发。
- 无金额限额时，`need.micros=0` 只表示**没有预留货币额度**，不是零价格或已知零费用。若实际价格也缺失，`CostRecord.cost` 保持 `None`，结算的 `known_dimensions` 不含 `micros`，预占维持 `partial`，`reconcile()` 明确缺少该维度的实际用量；若仅缺金额上界、实际价格已知，则照常结算实际金额。没有 epsilon 价格、假汇率或自动退款。
- 当前价格表只覆盖标准档估价，未声明覆盖所有计费档位的可验证上界。因此 `budget_bound="upper"` 与显式金额限额同时出现时拒发；只设 token 上界而未设金额上限仍可运行，已知实际费用照记，零金额预留不冒充金额上界。

正式回归 `tests/test_budget_review.py` 覆盖两 adapter 的零金额拒发、默认未知金额重开仍未决、发送前确有金额预占、普通/摘要实际结算、祖先金额闸、重试不退款且重新核限、摘要预算不足不发送，以及真实具有输出上界的 Anthropic adapter 在 upper 模式的金额边界。OpenAI-compatible 没有声明输出上界，未假造能力来声称支持该模式。

### B2 · P1：旧负计数在升级后仍可增加可用余额

旧版本公开 API 曾能留下 root `settled.tokens=-7`；根限额 10 时，新兄弟账户仍可预占 17。新写结算校验不能保护已经存在的错误账本。

修复限额、计数器、预占数量与历史结算数量的读取校验。新预占、结算、释放在写入前核验整条账户链；非法数量抛 `TianshuError`，不清零、不截断、不迁移，事务失败不允许部分写入。

历史跨版本证据：用 `4479088ecb2d161755d7ba3a89e46119c94a8b8e` 的公开 API 和冻结脚本种出真实旧账本；新实现 `usage("root")` 与 `reserve(...tokens=17)` 都因负整数域违规拒绝，四张预算表逐行保持相同。原冻结 `inspect` 脚本先要求读到负数，因此现在在第 31 行拒读并 exit 1，**该退出码没有冒充绿**；另用 `check_budget_history_fixed.py` 明确断言两次拒绝和四表不变，exit 0。旧版本误结清的 partial 历史无法仅凭合法正数重建未知信息，此边界保留，未洗白为已恢复。

### B3 · P2：摘要失败没有计入未知预算汇总

摘要协议错误或非法负 usage 被 C1 拒绝后，账本已保留预占，但游标的 `unknown_usage_calls` / `unknown_cost_calls` 未增加，导致 `run.end.budget_enforced=true`。

修复摘要调用异常分支，记录本次尝试的未知用量和未知费用；既有游标与终态汇总据此产生 `tokens_unknown_calls=1`、`budget_enforced=false`。正式回归通过两个真实 adapter 分别触发负 usage 与通用 protocol error，核对调用次数、未决预算、RunResult、终态事件，并执行 replay；没有新增伪 `finish_reason` 或改写历史事件。

### 第二轮验证与旧断言修正

- 首批正式回归在原实现 **16 failed**；扩展到金额发送边界、祖先额度、摘要、重试、历史非法记录后 **32 passed**。最终同一份 32 项又在冻结原实现 `e89a95d4c7e8f7196b38c51148e294d4c72bed9a` 上跑出 **32 failed / 1.96s**：其中 8 项在受控 transport 的发送前观察器中发现缺少 `micros` 预占，因而中止后续流程；其余直接暴露拒发、结算、未知汇总或坏历史数量判据失败。这个结果是断言能区分原实现的证据，不代表 32 个独立缺陷，也不把未到达的后续分支称为已验证。
- 原预算 **40 项通过**；冻结独立候选探针 **8 passed**；十四模块关联 **328 passed / 4.97s**。这些批次存在重叠，不相加充当项目测试总数。
- 原四个部分摘要用量测试的 `unknown_reservations==1` 需要精确修正：其 8 次假模型调用全部没有可核验价格，修复 B1 后应有 8 次金额未决，而缺 token 的调用仍恰好 1 次。现在断言 `unknown_reservations == model.calls == len(reconcile)`、每条 `usage` 缺 `micros`、恰好一条缺 `tokens`；没有放宽为 `>0`。
- 本机 `uv lock --check`、`uv run --frozen ruff check .` 均 exit 0；`uv run --frozen pytest` 为 **2235 passed / 7 skipped / 381.25s**（记录器总耗时 381.784s，exit 0，`budget-review-full`）。全量期间及之后仅澄清注释/证据文字并恢复原有 EOF 格式，没有行为变更；当前提交的独立复审与四项 CI 仍须另行绑定，不借用旧绿色。
- 日志位于 `/tmp/tianshu-debug-20261113/budget-review-*`，同名 JSON 保存完整 HEAD、命令、环境、退出码。测试时 HEAD 为 `e89a95d…` 加未提交修复，不能把这个 HEAD 冒充最终修复 SHA。`budget-review-history-frozen-inspect` 的 exit 1 是上述明确拒读；`budget-review-history-fixed` 为通过的接受性探针。

```sh
uv run --frozen pytest tests/test_budget_review.py -o addopts= -q
uv run --frozen pytest tests/test_budget_review.py tests/test_budget_ledger.py tests/test_model_contract.py tests/test_summary_loop.py tests/test_cost_line.py tests/test_pricing.py tests/test_usage.py tests/test_summary_context.py tests/test_summary_replay.py tests/test_summaries.py tests/test_g2_delegation_spawn.py tests/test_g2_delegation_exec.py tests/test_g2_delegation_recovery.py tests/test_g2_delegation_control.py -o addopts= -q -rs
```

第二轮没有新增生产模块、schema、枚举、价格行或依赖，也没有接触真实用户账本或付费服务。金额估算仍不能承诺绝对不超额；真实 provider 计费、用户设备与云端平台验证不能从本机受控 transport 推出。

## 第三轮：B3 的立即终止路径补修

独立二审对 `51d46e06f230d50b8d7ec27eb5ce3d3c763c2a0a` 的 B3 结论为**部分修**：摘要协议失败后，保留的金额预占使下一次普通调用在发送前被 `micros` 上限拒绝。此时内存 `RunResult.unknown_usage_calls=unknown_cost_calls=1`，但 `run.end` 从旧游标汇总，错误写成 `tokens_unknown_calls=0`、`budget_enforced=true`。因为普通请求未发送，没有新的 `model.intent`，`unbilled_model_intents=0` 也不能揭示摘要的未知用量。这是同一 P2 的未闭合路径，不把上一轮的绿色当成已完整解决。

根因是摘要统计更新后依赖下一次普通模型结果顺带保存游标。最小修复在 `_model_step` 检查摘要前后 `model_attempts`；确有摘要调用就立即通过现有 `TaskStore.save_cursor` 保存，先于正文落盘与下一次普通请求预算准入。摘要关闭、复用或预算提前拒发时没有发生新调用，不新增游标。没有改 `TaskStore`、终态契约、价格、预占或结算语义。

正式回归 `test_failed_summary_unknown_survives_immediate_budget_stop_and_reopen` 使用两个真实 adapter 和受控 transport：先在无金额上限时测得摘要发送前的实际总预占，再用该金额作限额，确保摘要已发送且失败、后续普通调用确实没有发送。断言内存、持久游标、`run.end`、未决账本一致；关闭数据库再重开，逐项核对游标、终态和未决预占，并执行 replay。此处“重开”是数据库关闭/重开，不声称新增了硬崩溃故障注入证据。

- 冻结独立探针 `test_verify_budget_r2_extra.py -k summary_failure_remains_unknown`：**2 failed → 2 passed**；正式新增回归：**2 failed → 2 passed**，原红均准确停在 `tokens_unknown_calls: 0 != 1`。
- 原始日志 `/tmp/tianshu-debug-20261113/budget-r3-{independent,formal}-{red,green}.{log,json}` 记录完整基准 SHA、命令、退出码与隔离环境。首次关联命令误写不存在的 `tests/test_resume.py`，exit 4 且没有测试执行，保存在 `budget-r3-affected`；纠正为实存 `tests/test_run_control.py` 后另存 `budget-r3-affected-corrected`，不把命令错误充当产品红绿。
- 当前修改仍基于 `51d46e0…` 的未提交工作区；受影响九模块 **183 passed / 5.07s**，lock/ruff 均 exit 0；全量 **2237 passed / 7 skipped / 384.75s**（记录器 385.282s、exit 0，`budget-r3-full`）。最终提交独立复审与四项 CI 仍须重新绑定；本轮作者亲跑独立探针不等于第二读者已批准。

```sh
uv run --frozen pytest tests/test_budget_review.py -k failed_summary_unknown_survives -o addopts= -q
```

## 诊断修复整合快照（2026-11-13）

将I/O `7111cbb43cd71db9f25a1348d91e7b39a0d97dee` 合入此前预算整合 `a3ac288c7ce403bbcb020c99057a50e8e224202c`。预算与其他生产src、UI、schema、锁文件完全不变；本批仅继承已独立批准的CI诊断包装器、MCP测试清理与对应文档，知识库保留双方记录。322个跟踪文件在全量前后hash相同，`budget-diagnostic-integrated-*` 原件记录 lock/ruff/full exit0，**2274 passed / 7 skipped / 394.72s**。这是提交前组合快照，日志HEAD仍为a3；后续完整SHA以Git对象继承把此证据绑定，不将旧a3的CI绿色当作新提交门禁。最终current SHA、第二读者继承与四required结果以PR #132正文版本记录为准。
