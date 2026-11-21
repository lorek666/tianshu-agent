# Responses 冻结代码树独立复审

最终结论：**blocker**。对象 `8637a4f5bf7eca31ced99bdce3bb5fdb6007a965`，代码树 `2ddd8ebd35bbc7dbb0b13c738bfbe57cba0028e8`，基线 `71eb57ef1544d126cc929b9172f5463a14999359`。独立第二读者 `responses_frozen_review`，2026-11-21。检查范围为上述基线至冻结 head 的分支改动、相邻生产恢复/计费调用链、C1/C3 机器契约、测试、UI 源码和构建产物及发布状态陈述。使用独立 detached 工作树；未修改跟踪文件、索引、分支或 HEAD。没有读取作者工作树或其 `.omc`。

- **P1，未修**：模型已提交终态且 Run 暂停，删除真实 `run_cursors` / `run_turns` 后直接 `resume_run` 会重新向模型发包。`src/tianshu/loop.py:503` 将投影丢失当成尚未产生模型回合；首轮事件与 `run.model_turn` 仍然完好。独立探针 `test_direct_resume_without_cursor` 实测新 transport 收到 1 次完整首轮请求，契约预期 0。
- **P2，未修**：游标完好时，模型已提交正常终态，暂停后热切为另一模式，再恢复却把 Run 记为 `failed`；实际新模型调用 0。`src/tianshu/loop.py:797` 在读取终态证人之前对下一调用执行 scope 检查。独立探针 `test_committed_end_then_switch_model`。
- **P2，未修**：SSE 函数参数 done 帧与 item/最终快照的函数名不同，仍得到 `tool_use`。同一组探针还验证 added 帧携带未知状态或与 done 冲突的非空 reasoning 密文也被接受。`src/tianshu/openai_responses.py:290,314,335`；`test_sse_observed_lifecycle_conflict`。
- **P2，未修**：中立工具提案参数的数值 `1` 与续接 JSON 布尔 `true` 被 `==` 视为相同。`src/tianshu/continuation.py:203`，独立 `model.result` v2 写入校验返回 `[]`。`test_c3_distinguishes_boolean_from_number`。
- **P2，未修**：显式调用 `rebuild_from_events()` 也不能还原审批中游标。源事件 `run.cursor` 的 `payload_hash` 与原游标相符；重建后提案的 `request_id`、`approval_id` 从非空变为 `None`，完整游标与该 hash 不符，元数据 `claim_epoch` 从 `1` 变 `0`。`src/tianshu/_responses_rebuild.py:62,94,97` 将审批只加到总表，并等待不存在的 `run.cursor_saved`；真实事件名是 `src/tianshu/_tasks_types.py:150` 的 `run.cursor`。因此不只删除时会损失信息，显式重建也可能覆盖本来完好的行。普通批准后续跑用例仍通过；此证据不表示相同副作用已执行两次。
- **P2，部分修**：在 `model-result-before-commit` 经真实子进程 `os._exit`、从上次保存的游标恢复后，两条持久 `model.intent` 的序号是 `[1,2]`，`run.end.unbilled_model_intents=1` 是正确的；但 `RunResult.model_attempts` 和新游标 `model_attempts` 都报 `1`，且标为精确计数。`src/tianshu/_loop_summary.py:605,705` 从事件算下一次序号，却仅从旧游标递增运行计数。CLI 展示该计数（`src/tianshu/cli.py:391,521`）。未观察到重用已结算预占。

## 验收项与边界

- **输入前缀与策略：已修。** 独立实际 loop 开启 `summary_mode="model"`，第一次 `read_text` 返回 12,000 字符，下一次 `input` 的既有前缀、原始 reasoning / 函数顺序、完整工具结果逐项相等，没有 spill/摘要，两个 intent 都是 `ctx-responses-v1`，离线 replay 通过。仓库定向用例覆盖超预算只发一次请求便停止，旧 ctx-v1 仍 spill。
- **密文隐私：已修（约定的展示路径）。** 独立探针检查默认诊断压缩包的所有成员。UI 的时间线渲染、流式预览不包含 reasoning，普通协议错误不回显密文，分别由相关定向测试验证。刻意关闭脱敏的导出、直接读事件库或内存中的原始续接值不是默认展示，本轮未扩大保证。
- **中立投影、函数身份与顺序：部分修。** 完整历史、`store:false`、不使用 `previous_response_id`、真实 `tool_manifest()`、有序 item、按 `call_id` 而非 `id` 对应工具结果、原始参数字符串保留均有证据；`true` / `1` 混同使 C3 对相互矛盾的双重提案没有拒绝。畸形单个函数字符串的执行侧拒绝通过。
- **HTTP/SSE 收束：部分修。** 正常文本、并发函数、refusal、最终用量、断流、终态与增量文本冲突有正负控；另有三个已复现的冲突未拒绝（上文）。未将受控传输判据说成线上提供商兼容证明。
- **scope、热配置与恢复：部分修。** 真要继续生成时，改变 adapter/endpoint/model 会在传输前拒绝；普通运行的上下文、请求、预算与成本身份被同一步快照固定。终态恢复却误用下一次生成的 scope 守卫。
- **事务及审批：部分修。** 真实提交前/后进程硬退、第二事件/版本行失败的回滚、正常审批后一次工具 intent、同一配置终态恢复零生成，以及显式重建后普通批准继续，均经定向验证。缺游标直接恢复会再生成；即使显式重建也丢失原审批绑定和游标元数据，两项不通过。不以仓库中只检查 `approval_ids` 总表的断言替代逐字段核对。
- **旧库版本和 hash：已修（已测范围）。** 与 Git 基线机器 schema 直接对照：整个 `legacyPayload` 不变，`model.result` / `run.model_turn` 的历史 v1 均等于基线 current，只有这两个 current 节点改变。历史摘要、无 continuation 的旧规范化消息、旧回放定向判据通过；真实 `a0732d9881247998df24db7af9f1c7989c1ae7a4` 读取器在 `model.result` v2 新库上抛 `AdmissionRefused [incompatible]`。未声称任意旧版都在零接触磁盘前拒绝。
- **计费与新 attempt：部分修。** 缓存缺席、显式 `null`、零和正值分别测试；正常缓存/网关不猜价格，reasoning tokens 不重复累加。请求额度为 1 和 2 的提交前崩溃负控通过，尝试编号不复用；返回及游标仍漏报一次真实 attempt。
- **设置及 UI：已修（离线范围）。** CLI 与设置/快速切模要求显式 ID，`openai` 凭据域复用受控 env，旧模式测试通过；React 定向 4/4，独立离线 Vite 构建与提交的 `ui/dist` 完全一致。

## 可复现证据

以下命令的工作目录均为 `/Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent`。第一条用真实 `tests/conftest.py` 作为 pytest 插件运行：探针内断言用户级权威根、维护锁根都已隔离，子进程经真实测试启动钩子继承隔离设置；全部用合成凭据，未读取真实钥匙串。正式模型 HTTP 未发包，仓库 HTTP 用例仅访问 loopback。

```sh
env UV_OFFLINE=1 PYTHONPATH=tests TIANSHU_CREDENTIAL_BACKEND=env TIANSHU_API_KEY=review-only uv run --frozen python -m pytest -p conftest -o addopts= -s -q --tb=no .omc/artifacts/independent-responses-review/test_independent.py
env UV_OFFLINE=1 TIANSHU_CREDENTIAL_BACKEND=env TIANSHU_API_KEY=review-only uv run --frozen pytest -q tests/test_responses_{contract,conformance,usage,http,integration,model,settings,preflight}.py
pnpm --dir ui install --offline --frozen-lockfile
pnpm --dir ui exec vitest run src/components/__tests__/responses.test.tsx
pnpm --dir ui exec vite build --outDir ../.omc/artifacts/independent-responses-review/ui-dist
diff -qr ui/dist .omc/artifacts/independent-responses-review/ui-dist
```

关键探针原始输出（`failed` 是产品未满足断言，不是门禁通过）：

```text
terminal: failed 模型续接作用域不兼容，请创建新 Run new model calls 0
approval proposal bindings before/after: [{'approval_id': (True, False)}, {'request_id': (True, False)}] cursor digest matches: False claim epochs: 1 0
resume without derived rows: 1 noop None
C3 conflicting argument boolean/numeric: []
accepted conflicting SSE: added_status tool_use
accepted conflicting SSE: added_cipher tool_use
accepted conflicting SSE: done_tool_name tool_use
crash accounting: sent attempts [1, 2] returned 1 saved 1 unbilled 1
old reader: AdmissionRefused [incompatible] model.result已登记payload版本不受支持：2。
```

仓库八个定向文件全部通过，无故意保留的失败；UI `4 passed`；构建比对无差异。独立探针套使用八个失败断言验证上述六项发现，另有按名称字符集清点与完整前缀/隐私/旧读取器两个对照。没有重复跑全量、远端 CI 或发行流程，也没有对这些门禁作通过背书。

## 归档校验

完整 Git 差异的 SHA-256 是 `d87041045dd368ea21838093f5358619cd725f7d07935b208e57ff402b84c85c`，复核命令为 `git -c core.quotePath=false diff --no-ext-diff --no-textconv --binary 71eb57ef1544d126cc929b9172f5463a14999359..8637a4f5bf7eca31ced99bdce3bb5fdb6007a965 | shasum -a 256`。这是整份 diff 的摘要，包含文档和打包产物；阅读按源文件、契约、测试和 UI 分组，构建产物按字节比对。

- `test_independent.py`：`5d46fd8bcd3f231668512b43c474a26003100e6f141f41fda47e01b207e24f93`
- `probes.log`：`95775189a2e8a04b74b2e2a01fa2080b4a2d5c94c6b5a198235da3b445185d28`
- `ui-dist/index.html`：`f4a9af15248083a875f8b92a8b27430c4f0d26eb9647b99a48b6377716b6fa0b`
- `ui-dist/assets/index-DpCQcvpl.js`：`6a48e3ae41bb82091bf26ebf926816b1fe9ba1899f898bb34e59a29875d3436f`
- `ui-dist/assets/index-DWb5DCdH.css`：`0b257825b1c7ca4d8cd0974d1d4707417ccecf2d8d7c5df15f29612fba13a4ea`

`REPORT.md` 本身的摘要存放在同目录的 `SHA256SUMS`，避免在报告正文中自引用。校验：在本报告目录运行 `shasum -a 256 -c SHA256SUMS`。

## 已考虑但不作结论

- `tool_manifest()` 实发的名称有 12 个带点（如 `memory.search`）。离线清点只证明请求形状；仓库测试未验证真实 Responses 服务的名称字符约束。本轮不接外网，不据此断言官方或网关会返回 400。建议在各自显式 smoke 中保留真实工具清单。
- Responses 以“显式配置、已规范化的请求端点 + 模型 ID”绑定，官方可能返回带日期模型快照；没有把响应 `model` 与请求别名逐字不相等判为 scope 缺陷。未单独验证非默认收费层或官方线上价格。
- 非流式公开函数 item 的 `id`、`status` 可选时，只要顶层终态与完整函数提案成立，不因可选字段未出现判错；没有把未知枚举与合法的可选缺席混为一谈。
- 原始事件供持久恢复、UI 客户端拿到但未渲染原始 payload、用户显式关闭诊断脱敏等，与“默认诊断/可见文本不展示密文”不同。本轮只判受约束路径，没有验证本机用户直接读取数据库、第三方网关行为或跨平台用户设备。
- 真实付费官方/网关 smoke、Windows 真机、跨进程多用户、真实硬件断电、托管 required CI：本次独立复审未运行。仅 local child crashpoint 定向通过，不把它等同断电。旧计价器是否应识别所有 Chat 网关的原生嵌套缓存字段、未完成/未支持的多模态/内置工具等，也未据此另开本功能 blocker。
