# PR #64（`feat/memory`）独立复审

## 1. 复审对象

- **PR / 分支**：PR #64，`feat/memory`，worktree `/Volumes/Date/ts-wt-memory`
- **复审时最终 HEAD**：`c4216e1`（`merge: 合入 main（recovery 坏清单修复 / M0–M1 对账 / issue #63）`）
- **复审期间 HEAD 发生了移动**：开始时是 `80cb261`，复审期间新增了
  `dd0fa37`（docs）、`fc09c78`（test）、`c4216e1`（merge main）。本报告的所有证据
  固定在 `c4216e1`；代码快照用 `git archive c4216e1` 落在
  `/tmp/ts-review-c4216e1`，探针脚本在 `/tmp/f10probe/`。
  这不是作者事先声明的“两个 commit”状态；如果作者还要继续改，请以本报告的
  commit 为准重新对一次。
- **复审方式**：不读作者自查结论当事实。PR 描述、`docs/KNOWLEDGE.md` §8 的
  “实测 1215 passed”、代码注释里的“已核验”，全部当**待证伪主张**；自己写探针、
  真跑、贴输出。授权/事务/遗忘/检索/闸门/老路径均用真实 `EventLog` + 真实
  `Dispatcher` + 真 sqlite（FTS5）跑；进程重启探针用**真子进程**；CLI 确认
  用**真子进程**跑 `python -m tianshu`。
- **复审结论**：**不能按“已验收”合并**。授权旁路与事务主路径总体成立（见 §2/§5），
  但存在 **1 项高危契约违背**（记忆正文进入 append-only 事件日志，F10 §3.1 的
  立论不成立）以及 **5 项中等缺陷**（派发请求表落明文 PII/密钥、编辑能力没有
  生产入口、workspace/task 记忆没有生产创建入口、“同事务”只在装配约定里、
  Worker/daemon 会打开第二个投影）。另有若干文档与实现不一致，见 §3。

复现环境：

```bash
cd /Volumes/Date/ts-wt-memory
uv run --frozen pytest -o addopts="" -q
# 1215 passed, 3 skipped in 106.72s
uv run --frozen ruff check .
# All checks passed!
uv lock --check
# Resolved 8 packages
```

收集数：`44` 个 `tests/test_*.py`，`1218` 条 collected。`docs/F10-memory-design.md`
§14 引用的 64 个 `test_*` 函数名全部真实存在（脚本 `probe_docs_current.py`，
`missing: []`）。

## 2. F10 §14 验收项逐条结论

> 说明：这里的“已修”= 该判据已被实现，并且有真实测试或本复审探针打红过；
> “部分修”= 主路径成立但存在未闭合的边界/文档过度陈述；“未修”= 没有实现或
> 没有测试。**单条“已修”不等于整个 PR 可合**。

| # | F10 §14 判据 | 结论 | 可复现证据（命令 + 关键输出） |
|---|---|---|---|
| 1 | 记住「报告用中文、先列结论」后，新任务能检索到 | **已修** | 作者测试 `test_user_memory_is_recalled_by_a_later_task` 只是投影/loadout 级；本复审补了真实 AgentLoop 跨 Task 探针：`/tmp/f10probe/probe_cross_task.py` → `A candidates [('mem_c3c...','candidate','inferred',1)]`、`B search candidate output 0`、`C search active output 1 ['mem_c3c...']`。功能成立；作者测试没有真正建“新 Task/Run”，见 §3 D14。 |
| 2 | 不同 workspace 不串记忆；模型读不到另一个 workspace 的私有记忆 | **已修** | `tests/test_memory_store.py::test_cross_scope_isolation_and_loadout`、`tests/test_memory_dispatch.py::test_workspace_scope_is_denied_cross_task_and_rule_is_registered`；复审检索探针 `probe_retrieval.py` → `user-only search cannot see workspace []`、`workspace-a search sees it ['mem_3e00...']`、`workspace-b search does not []`。 |
| 3 | 中文无空格文本与中英混合标识符的词面召回用固定样例量化 | **已修** | `probe_retrieval.py` → `tokenize(doc) ('报告','告用','用中','中文','先列','列结','结论')`、`search 中文结论 [('mem_...','active',30)]`；`memory.search` / `ToolRegistry` 均命中；`tool registry`（中间有空格）不命中——这是词法边界，不是判据失败，但说明“标识符召回”只覆盖精确写法。 |
| 4 | 候选未经确认不激活；摘要 / 网页不能自动成为长期用户事实 | **部分修** | 候选不召回成立（`probe_cross_task.py`: B search 0；`probe_gate.py`: 候选 `search []`）；模型侧 propose 固定 `origin=inferred`（`memory_exec.py:477`），`probe_auth.py` A6 → `production propose origin inferred`、伪造 extra `origin` 被 `ToolArgumentError` 拒绝。**缺口**：“摘要 / 网页不能自动成为事实”只有代码层面的 inferred 约束，没有入口级测试；且 §14 说“没有断言 origin 的测试”是错的（`tests/test_memory_loop.py:349` 就断言了 `origin == "inferred"`），见 §3 D13。 |
| 5 | 编辑保留 revision；忘记后新任务 / 重建索引 / 应用重启均不再召回 | **部分修** | 忘记四条全过：`probe_forget.py` → `search after forget []`、`rebuild hash equal live True`、`DROP+ensure_schema search []`、真子进程重启 `child stdout {"search": [], "get": [... "forgotten", 3] ...}`、`rebuild2 hash equal live True`。**但“编辑保留 revision”没有生产入口**：`supersedes` 只有 `MemoryProjection._apply_propose` 接受，`memory.propose` 的 schema 没有 `supersedes`，`memory_exec._propose` 不读它，CLI 没有 edit；`test_supersede_keeps_old_row_but_removes_it_from_recall` 是**测试直接播种**一个带 `supersedes` 的 output。§14 把这一整行标 ✅ 是过度陈述，见 §3 D3。 |
| 6 | 删除整个检索索引可重建，正文与来源仍在 | **已修** | `probe_forget.py`：`DROP TABLE memory_fts` + `ensure_schema()` 后 `search []`，`rebuild2 applied 3`、`hash equal live True`、`memories row kept 1`。作者测试 `test_projection_hash_covers_the_fts_index`/`test_rebuild_reproduces_projection_hash_and_search` 也覆盖。 |
| 7 | 离线 replay 不读当前记忆库最新版本（用当时快照） | **已修（功能）** | 作者 `test_replay_uses_the_snapshot_not_the_latest_memory`；复审 `probe_event_content.py` 证明 replay 能“用当时快照”，但快照是**完整记忆正文写在 `tool.result` 事件里**——这正触发 §3 D1：功能成立，立论与 §3.1 冲突。 |
| 8 | 记忆激活 / 遗忘有 intent/result + Receipt；审批被拒则不写 | **已修** | `probe_tx.py` T4 → `confirm succeeded applied active/2`、`receipt/verify (['receipt'], [])`；`probe_gate.py` 确认/遗忘 `verify_receipts` 由作者测试覆盖；`test_confirm_requires_approval_and_rejection_changes_no_projection`。事务细节见 §5 B。 |
| 9 | L2 每条信念能指回证据与原文引文；`proof_count` 与实际条数一致 | **部分修** | `MemoryProjection._apply_propose` 机械校验 `proof_count == len(evidence)`（`memory.py:836-842`）；`test_unknown_or_invalid_propose_values_raise` 的 `proof_count=99` 参数会红。**L2 实体未实现**（§13 #19），不能算通过。 |
| 10 | 蒸馏只做增量：未变部分逐字节不变 | **未修** | §13 #19 自述未实现；`grep -rin distill src/tianshu/` 无结果；没有测试。 |
| 11 | 陈旧层先核验再用：L2/L3 不短路检索 | **未修** | 只有 `MemoryProjection.observe` 原语；§13 #23 自述作用域/新鲜度未实现；没有测试。 |
| 12 | 记忆写入过 PII / 密钥闸门：含密钥的候选被红act 或阻断，不入库 | **部分修** | 投影侧成立：`probe_gate.py` → secret `dispatch rejected none_applied`、`counts {'memories':0,'fts':0}`、blob 不存在、event log 无 secret；PII `blob 联系 [REDACTED:email] 或 [REDACTED:phone]`。**但同一份原文在 `dispatch_requests.args_payload` 明文落库**（`probe_persist.py`: `dispatch_requests raw PII: True True`；`probe_gate.py`: secret 所在 request_id 被列出），见 §3 D2。 |
| 13 | 写入前去重：重复事实不重复进索引（进索引前条数已下降） | **已修** | `probe_gate.py` → 第二次同内容 `prepare ('rejected','c4.create_over_existing')`、`dedup count delta (0,0,0)`、`duplicate direct projection True`；`test_find_duplicate_and_deduped_event_do_not_grow_the_index`。 |
| 14 | 资源种类封闭：历史裸绝对路径仍是 `file` 且回写逐字节不变；未知 scheme / 相对路径 fail-closed | **已修** | `tests/test_resources.py` 22 个 def / 42 条；`probe_policy.py` 的固定 file 请求 `digest b38a...` 在 `main` 与 HEAD **逐字节相同**；`test_absolute_path_is_file_and_byte_identical`、`test_absolute_path_with_double_colon_slash_is_still_file`。**但 `memory://workspace/relative` 会被解析成 CWD 下的绝对路径**（见 §3 D8），这是作用域 URI 的 fail-closed 缺口，不属于“裸文件路径”那一行。 |
| 15 | 作用域编码可指回来源：`memory://workspace/<percent-encoded 绝对路径>` 解析回同一路径 | **已修** | `test_workspace_uri_percent_encodes_and_round_trips`、`test_workspace_scope_is_canonicalized`；复审 `probe_forgotten_get.py` 确认 percent-encoded round-trip。**但 relative scope 也被接受**（D8）。 |
| 16 | 工具与资源种类不得互换：文件工具收到 `memory://`、记忆工具收到绝对路径都要拒 | **已修** | `probe_auth.py` A2 → `file-tool+memory-resource ('rejected','dispatch.memory_resource_mismatch')`、`memory-tool+file-resource ('rejected','dispatch.memory_resource_mismatch')`、`approval rows delta 0`；A3 混合资源 → `c4.multi_resource` / `dispatch.memory_resource_mismatch`；A1 零绑定控制 Run 文件动作 → `dispatch.lease_lost`，文件内容 `原始内容` 未变。 |
| 17 | `memory.*` 五个工具真的出现在 `tool_manifest()` 里，schema 就是 `SPECS` 里那份 | **已修** | `tests/test_memory_dispatch.py::test_memory_tools_are_real_specs_without_path_args`（真实 `tool_manifest` + `SPECS`，`version=1.0`、`path_arg==""`、`additionalProperties=False`）；`probe_docs_current.py` 结果 `RULE_IDS 49 == rules.json enum 49`。 |
| 18 | 零目录任务也能用记忆（读与写都不该被“没有目录”挡住） | **已修** | `test_propose_works_for_zero_directory_task`、`test_zero_directory_task_can_recall_memory`；`probe_cross_task.py` 的 Task A/B/C 全是零绑定；`probe_auth.py` A1 零绑定控制 Run 的 **memory propose** 成功、**file** 拒绝。 |
| 19 | 检索文本走 F5 字节预算：超预算时是明确预览 / 丢弃，不是静默塞入 | **已修** | `probe_retrieval.py` → 5 条上限 `cap count 5`；长文 `long count/bytes (1, 4095)` 且 `is prefix True`（截断不是丢弃）；多条预算 `budget bytes 4094`、`per-item [1000,1000,1000,1000,94]`；作者 `test_recall_text_goes_through_compile_context_budget`。 |
| 20 | 实际进入视图的记忆身份落进 `model.intent.payload.context`，供回放核对 | **已修** | `test_zero_directory_task_can_recall_memory` 断言 `context["memories"]` 的 `memory_id/revision/content_ref`；`context.context_payload(..., memories=...)`；复审 `probe_event_content.py` 的 `model.intent` 只带身份，不带正文（正文问题在 `tool.result`，D1）。 |
| 21 | CLI 控制面能列出 / 检索 / 读取 / 确认 / 忘记 | **部分修** | 真子进程 `probe_cli_proc.py`：`CLI rc 0`、`after CLI observe ('active', 2)`、`receipts ['memory.confirm']`、`approval types ['approval.decided','approval.consumed']`；user / workspace 路径成立。**task 作用域的 confirm/forget 在 CLI 被显式拒绝**（`cli.py:1207-1213`），而 §13 #14 / §14 没写这个限制，见 §3 D7。 |

## 3. 缺陷清单

### D1（高）记忆正文进入 append-only 事件日志，F10 §3.1 的立论不成立

- **位置**：`src/tianshu/loop.py:1366-1463`（`memory.search/get` 输出正文）、
  `src/tianshu/loop.py:1794-1799`（`_record_result` 把 `output` 原样写进
  `tool.result`）、`src/tianshu/memory.py:1269-1349`（search）；
  `docs/F10-memory-design.md:27`；`80cb261` commit message。
- **现象**：F10 §3.1 写“事件日志里**只存引用与审计元数据**，不重复写记忆正文——
  不可删除的日志不该随记忆膨胀”。实现里 `memory.search` / `memory.get` 的
  `ToolResult.output` 带 `content`，主循环 `_record_result` 把它原样写进
  `tool.result` 事件。`memory.get` 甚至把正文放了两遍（顶层 `content` 和
  `memory.content`）。
- **复现**：

  ```bash
  uv run --frozen python /tmp/f10probe/probe_event_content.py
  # [tool.result memory.search] ('succeeded','none_applied')
  # [  has content?] True
  # [  raw body in event?] True
  # [tool.result memory.get] ... [  raw body in event?] True
  # [read result payload bytes] [750, 666]
  ```

  作者自己的测试也承认这件事：`tests/test_memory_loop.py:479`
  “事件里的完整结果仍然在（审计与回放不跟着预算一起缩水）”；`context.py:227`
  “正文与出处本来就在 `tool.result` 里”。而 `tests/test_memory_dispatch.py:343`
  只对 **propose** 检查“事件日志只存引用/审计元数据”，没有覆盖 read 事件——这正是
  “测试通过只说明测试测的那些事成立”的形状。
- **影响**：每次 recall 都把最多 4 KiB 正文写进不可删除的日志；日志随记忆膨胀；
  `memory.forget` 只能清投影，正文永远留在事件日志；F10 §10 的物理擦除和
  “删除正文 blob 不会误删日志材料”的设计前提被自己推翻。
- **建议修法**：`memory.search/get` 的 `tool.result` 只写
  `memory_id/revision/content_ref/content_sha256`（可选 `content_available`），
  不写正文；replay 需要当时正文时按 `content_ref` 从快照读，读不到就明确
  `历史证据不可用`（这与 §10 已有语义一致）。若作者认为正文必须在 `tool.result`
  里才能 replay，那就必须**改 F10 §3.1 与 commit message**，并补“正文如何物理
  擦除 / 日志如何保留”的新契约；不能两边都声称。
- **回归测试**：加一条“memory.search/get 的 `tool.result` payload 不含记忆正文”
  的断言；`test_propose_really...` 只测 propose 不够。

### D2（中）派发请求表先落明文，写入闸门在后：PII / 密钥留在 `dispatch_requests`

- **位置**：`src/tianshu/dispatcher.py:1577-1596`（`_store_request` 把完整
  `args` 明文写 `args_payload`）；`src/tianshu/memory_exec.py:419-434`（闸门在
  executor 才执行）。
- **现象**：`memory.propose` 的 `prepare` 会先把 `content` 原文写进
  `dispatch_requests.args_payload`，之后 executor 才做 PII/密钥扫描。
  - **密钥候选被阻断**：`memories=0`、`fts=0`、blob 不存在、event log 无 secret，
    但 `dispatch_requests` 里仍是原文（`probe_gate.py`：
    `dispatch_requests args contain secret ['req_...']`）。
  - **PII 成功候选**：blob 是 `[REDACTED:email]`，event 无原始 PII，但
    `dispatch_requests` 里原文仍在（`probe_persist.py`:
    `dispatch_requests raw PII: True True`）。
  - 该表没有清理/擦除路径，`memory.forget` 也不碰它。
- **复现**：

  ```bash
  uv run --frozen python /tmp/f10probe/probe_gate.py
  uv run --frozen python /tmp/f10probe/probe_persist.py
  # dispatch_requests raw PII: True True
  # blob PII: False
  # events raw PII: False False
  ```

- **影响**：F10 §7 的“写入闸门是写入前扫描，命中即红act 或阻断”在投影/blob
  上成立，但在**同一 sqlite 的审计表**上不成立；F12 的“全链路脱敏”有缺口。
  本地单用户、无网络，危害等级不是高危，但“密钥 at rest”不应被测试写成已解决。
- **建议修法**：在 `prepare()` 调 `_store_request` **之前**对 memory 写工具做
  gate：被阻断的候选直接拒绝、不落 `dispatch_requests`；PII 先红act 再落
  `args_payload`（或只落 `content_sha256` + 长度，不落正文）。如果为了审计必须
  保留原文，必须新增保留策略并明确“这里不是物理擦除边界”，同时改 §7/§14 的
  ✅。

### D3（中）`supersede` / 编辑没有生产入口，§14 却标“编辑保留 revision”已覆盖

- **位置**：`src/tianshu/memory.py:848-901`（`supersedes` 只有事件输出能带）、
  `src/tianshu/memory.py:953-973`（`_supersede`）；`src/tianshu/tools.py:789-800`
  （propose schema 无 `supersedes`，`additionalProperties:false`）；
  `src/tianshu/memory_exec.py:407-488`（`_propose` 不读 `supersedes`）；
  `docs/F10-memory-design.md:219-221`、`:307`。
- **现象**：F10 §9 要求“用户能直接编辑 L1–L3”“编辑保留 revision，用 supersede
  而非覆盖”。实现只有投影层接受 `output.supersedes`；**没有任何工具、CLI 或
  可信入口产生 `supersedes`**。`tests/test_memory_store.py::test_supersede...`
  是测试手工 `_propose_payload(..., supersedes="old")`，不是生产路径。
- **复现**：

  ```bash
  grep -rn "supersedes" src/tianshu
  # 只出现在 memory.py（以及 cli.py 的只读打印）
  grep -n "def test_supersede" tests/test_memory_store.py
  # 1066: 测试播种
  ```

- **影响**：§14 的“编辑保留 revision”✅ 是**未兑现的验收项**；用户/模型都没有
  编辑入口；旧版可查只在事件/投影里有行，但没有一次真实编辑事件。
- **建议修法**：补 `memory.edit`（或 `memory.supersede`）工具/CLI，走 C4
  overwrite 审批 + Receipt，参数绑 `memory_id/revision` 与旧版；或者把 §14 该行
  改成“部分修/未覆盖”，并在 §13 加一行“编辑未实现”。不能既无入口又标 ✅。

### D4（中）workspace / task 作用域记忆没有生产创建入口

- **位置**：`src/tianshu/loop.py:1603-1632`（`_memory_target` 注释明确
  “propose 首版只有一个作用域：user/local（共享是另一个显式动作，本单不做）”）、
  `src/tianshu/tools.py:789-800`（propose schema 无 `scope/scope_id/target`）、
  `src/tianshu/cli.py:177-179`（CLI 没有 propose）。
- **现象**：模型只能 `memory.propose` 到 user；`memory.confirm/forget` 只能操作
  **已经存在**的记忆。CLI 也没有创建命令。于是 workspace / task 作用域的记忆
  只能由测试播种或库调用者直接造事件，**没有生产路径**。§6 的“把它放进某个
  workspace / task 作用域是显式动作”没有落地；`tests/test_memory_loop.py:383`
  的 `test_loadout_equips_only_authorized_scopes` 是 `env.seed` 播种。
- **影响**：§14 的“不同 workspace 不串记忆”是**读侧隔离**成立，不是“能创建并
  使用 workspace 记忆”；README/§13 没有披露这个缺口。
- **建议修法**：给 `memory.propose` 加受控的 `scope/scope_id`（或单独
  `memory.promote`/`memory.share` 动作），执行侧按绑定/任务校验；或明确写
  “首版只能创建 user 记忆，workspace/task 仅支持已存在记忆”，并修正 §6/§13/§14。

### D5（中）“同事务”只在 `MemoryProjection` 构造处强制，Dispatcher 边界可错装配成两库分叉

- **位置**：`src/tianshu/memory.py:585-597`（`conn is log.conn` 检查）、
  `src/tianshu/dispatcher.py:339-381`（`memory` 参数直接赋值，不校验）、
  `src/tianshu/dispatcher.py:404-443`、`:1920-1934`（`self._log.transaction()`
  + `projection.apply_event`）。
- **现象**：默认装配（`build_stack`/`run`/CLI）确实同库同连接，`probe_tx.py`
  也证明 T2 “apply_event 已改投影再抛异常 → 投影回滚、只留 unknown 事件”。
  **但如果调用方给 Dispatcher 传一个别的 EventLog 的 MemoryProjection**
  （`Dispatcher(log1, ..., memory=projection(log2))`），`_commit_result` 会在
  log1 的事务里 append 事件、在 log2 的独立事务里 apply 投影；log1 提交失败时
  log2 已提交 → 事件不存在、投影存在。
- **复现**：

  ```bash
  uv run --frozen python /tmp/f10probe/probe_mismatch.py
  # dispatch failed unknown RuntimeError: after apply
  # log1 events memory tool.result succeeded: 0
  # log2 memories rows: 1 [('mem_...', 'candidate')]
  # log1 dispatches state: [('error',)]
  # log1 in_tx False log2 in_tx False
  ```

- **影响**：当前生产装配不触发，属于“硬约束没有在边界上强制”的结构性缺陷；
  一旦未来装配代码复制错，事件/投影会静默分叉，且 unknown 结论无法裁决。
- **建议修法**：`Dispatcher.__init__` 校验 `memory` 的 log/conn 是 `self._log`/
  `self._conn`（给 `MemoryProjection` 加只读 `log`/`conn` 属性，别摸私有
  `_conn`）；`AgentLoop`/`build_stack` 同样校验；不匹配直接抛
  `MemoryStoreError`，不提供“先跑通”的后路。

### D6（中）Worker / daemon 的 AgentLoop 没传 `stack.memory`，会打开第二个投影；自定义 blob 目录时读不到正文

- **位置**：`src/tianshu/worker.py:323-332`、`src/tianshu/daemon.py:798-807`
  构造 `AgentLoop` 时没传 `memory`；`src/tianshu/loop.py:1349-1350`
  `self._memory = open_memory(self._log)`（默认 blob 目录 DB 旁）。
  `src/tianshu/cli.py:330-341` 与 `loop.run()` 是传了的，二者不一致。
- **现象**：`build_stack(memory=custom)` 时，Dispatcher 用 custom 的
  `blob_dir`，Worker/daemon 的 AgentLoop 却新建默认 `memory_blobs` 投影；
  写入成功、recall 时 `search` 读不到正文，count=0。
- **复现**：

  ```bash
  uv run --frozen python /tmp/f10probe/probe_assembly.py
  # stack.memory is custom: True stack.memory.blob_dir= .../custom
  # propose succeeded blob exists custom: True db-parent exists: False
  # confirm succeeded
  # engine memory is custom? False
  # engine projection blob_dir .../memory_blobs ... is custom? False
  # search through second projection count 0
  ```

- **影响**：默认装配（两者都用 DB 旁）不会触发；但注释和 §13 说“所有入口都走
  同一套 blob 目录 / 同一个实例”，实际 Worker/daemon 没有。测试若给 stack 注入
  自定义时钟/blob dir，主循环读写可能看到两套时间/目录。
- **建议修法**：Worker/daemon（以及 `eval/runner.py`、`tests/support.engine_for`
  等所有 AgentLoop 构造点）统一传 `memory=stack.memory`；或在 `AgentLoop`
  构造里若 `tasks.log` 已有 `Stack` 的记忆就强制复用（更稳的是把 `memory` 设为
  `build_stack` 的必传字段，避免默认分叉）。

### D7（轻）CLI 不支持 task 作用域的 confirm/forget，§13 #14 / §14 未写这个限制

- **位置**：`src/tianshu/cli.py:1207-1213`（`scope == "task"` 直接
  `TianshuError`）；`docs/F10-memory-design.md:278`、`:323`。
- **现象**：CLI 能 `list/search/get` task 作用域（带 `--task-id`），但
  `confirm/forget` 一律拒绝；`§13 #14` 只写“list|search|get|confirm|forget ✅”，
  `§14` 只用一个 user 作用域用例。§14 的“CLI 控制面能…确认/忘记”应加
  “user/workspace 才支持”的限定。
- **复现**：`probe_cli_proc.py` 证明 user 真子进程 OK；task 路径由
  `cli.py` 代码直接可见（拒绝文案），没有黑盒用例。
- **建议修法**：补 task 作用域 CLI 路径（需要绑定到该 task 的 Run/控制 Run），
  或在文档与 §13 #14 明写“CLI 暂不支持 task confirm/forget”。

### D8（轻）`memory://workspace/relative` 被接受并解析到 CWD；fail-closed 契约不一致

- **位置**：`src/tianshu/resources.py:123-130`（`MemoryScope.workspace` 先
  `realpath`）、`:180-197`（`parse_memory_uri` 对 workspace 先 `realpath`）。
- **现象**：文档说 workspace `scope_id` 必须是规范化后的绝对路径；实际
  `parse_resource("memory://workspace/relative")` → `MemoryScope('workspace',
  '/Volumes/Date/ts-wt-memory/relative')`（CWD 下），`MemoryScope.workspace("relative")`
  也接受。`tests/test_resources.py::test_workspace_scope_rejects_relative_path`
  只测了直接构造 `MemoryScope("workspace","relative/dir")`，没测这两条入口。
- **复现**：

  ```bash
  uv run --frozen python /tmp/f10probe/probe_forgotten_get.py
  # parse_resource(relative workspace URI) -> MemoryScope(scope='workspace',
  #   scope_id='/Volumes/Date/ts-wt-memory/relative')
  # MemoryScope.workspace(relative) -> 同一个 scope
  ```

- **影响**：低。最终仍要求 `scope_id` 精确等于任务绑定根，不能跨作用域；但同一
  URI 字符串在不同 CWD 进程里含义不同，审计/重放需要 canonical 化；违反
  “相对路径 fail-closed”的契约。
- **建议修法**：`parse_memory_uri` 对 workspace 先要求 `os.path.isabs(unquote(...))`，
  再 realpath；`MemoryScope.workspace` 只在明确接受“相对 CWD”的地方调用，或
  加 `require_absolute` 参数。补 URI 级测试。

### D9（轻）`memory.search` 模型不能“要求更少”——schema 没有 `limit`，§8 却写“可以要求更少”

- **位置**：`src/tianshu/tools.py:760-764`（search schema 只有 `query`，
  `additionalProperties:false`）；`src/tianshu/loop.py:1409-1414`
  （`args.get("limit", ...)` 是死分支）；`docs/F10-memory-design.md:210`。
- **现象**：`validate_args({"query":"x","limit":2}, SPECS["memory.search"].input_schema)`
  → `ToolArgumentError: 不允许的参数：['limit']`；模型只能拿默认 5 条。
- **复现**：`probe_retrieval.py` 最后两行。
- **建议修法**：schema 加 `"limit": {"type":"integer","minimum":1,"maximum":5}`，
  loop 保留 `min(limit,5)`；或把 §8 的“模型可以要求更少”删掉并说明由执行侧
  固定。

### D10（轻）`memory.get` 在 forget 之后仍返回正文；“新请求不能复活”只覆盖 search

- **位置**：`src/tianshu/memory.py:1228-1232`（`read_content` 直接按 ref 读）、
  `:1351-1364`（`get` 含 forgotten）、`src/tianshu/loop.py:1432-1462`。
- **现象**：`probe_forgotten_get.py` → 忘记后 `memory.get` `found=True,
  available=True, content='忘记后还能按 id 读到吗'`。作者测试
  `test_forget_is_not_recalled...` 也**明确**断言 `read_content(forgotten) == 原文`。
- **影响**：设计上 tombstone 可审计，可能是有意的；但 F10 §10 说“新请求与重建
  索引都不能复活它”，而 `memory.get` 是一个新请求。模型只要记得旧 id 就能在
  forget 后继续读到正文，语义边界未定义、没有测试钉。按用户“忘记不可复活”的
  验收口径，这至少需要明确写“recall 不返回 ≠ 不可读”，否则容易被读成漏洞。
- **建议修法**：明确 `get` 的语义：模型侧 `memory.get` 对 forgotten 返回
  `found=False`（人 CLI `--show-forgotten` 才可审计），或保留可读但在 §10/§14
  写清楚；补模型侧 forget 后 get 的用例。

### D11（轻）读路径每次 `ensure_schema()` 都开 `BEGIN IMMEDIATE`，读操作也要写锁

- **位置**：`src/tianshu/memory.py:626-634`；`search/get/list/observe` 都先
  `ensure_schema()`，内部 `with self._log.transaction()` → `BEGIN IMMEDIATE`。
- **现象**：`memory.search`/`get` 在 SQLite 上拿写锁（即使没有写入）；多进程
  CLI/daemon 同时读时可能拿到 `database is locked`（busy_timeout 5s），而不是
  纯读。不是功能错误，但“读是 pure”的性能/并发语义不一致。
- **建议修法**：schema 迁移/版本检查只在构造或显式 `ensure_schema` 时做一次；
  读路径缓存 `_ready`，或用 `BEGIN`（deferred）读事务。

### D12（轻）`docs/KNOWLEDGE.md` 的 F10 条目仍写“工作区状态，未提交 / 1212”，与当前 HEAD 不符

- **位置**：`docs/KNOWLEDGE.md:31`（“工作区状态，未提交”）、`:273`
  （“1212 passed… 未提交”）、`:34`（“1215 passed”）。
- **现象**：当前 HEAD `c4216e1` 是已提交分支，`git status` 干净；全量实测
  `1215 passed, 3 skipped`。§8 同一份文档里 1212 与 1215 并存，且“未提交”是
  过期状态。
- **建议修法**：把 `:31`/`:273` 改为“已提交 `feat/memory`，合入 main 后 1215
  passed”；保留历史数字时明确标“合入前”。

### D13（轻）§14 说“没有断言 origin 的测试”，但已有断言

- **位置**：`docs/F10-memory-design.md:306`；`tests/test_memory_loop.py:349`
  `assert memory.origin == "inferred"`。
- **现象**：文档的“候选未确认不激活”行说“没有断言 origin 的测试”，实际
  `test_agent_propose_then_confirm_through_dispatcher` 已断言；这是**低报**，
  不是安全缺陷，但说明 §14 的自述也在漂。
- **建议修法**：把该句改成“已有模型侧 origin=inferred 断言；缺的是‘摘要/网页
  入口’本身”。

### D14（轻）§14 的“新任务”证据不是真正的新 Task/Run；“忘记后新任务”测试复用了同一 Task

- **位置**：`docs/F10-memory-design.md:301`；`tests/test_memory_store.py:734`
  （`test_user_memory_is_recalled_by_a_later_task` 只做 `loadout()` 后直接
  `projection.search`）；`tests/test_memory_loop.py:508`
  （`test_agent_forget_then_new_task_cannot_recall` 的 before/after 是**同一个
  `task.task_id`**）。
- **现象**：作者的“新任务”是投影层 loadout，不是新 Task/Run；本复审另跑了
  `probe_cross_task.py` 才证明真实跨 Task 行为成立。行为本身是好的，但 §14 的
  “✅ 已覆盖”证据不够。
- **建议修法**：把 `probe_cross_task` 形状固化成测试（Task A 写、Task B 读、
  Task C 读），或把 §14 文案改成“存储层 + 主循环分别覆盖，跨 Task 端到端未单独
  钉”。

### D15（轻，流程）契约 schema 没有跟 `docs(contract)` 一起提交；复审期间 HEAD 连续移动

- **位置**：`b9d519f` 只含 `docs/F10-memory-design.md` 与 `docs/PRD-v1.md`；
  `schemas/c4/rules.json` 在 `80cb261`（feat 实现）里。
- **现象**：AGENTS §三要求“契约变更单独提交，不夹带代码”；`rules.json` 是
  机器可校验契约，却进了实现 commit。另：复审开始时 HEAD 是 `80cb261`，期间
  新增 `dd0fa37`/`fc09c78`/`c4216e1`，作者在复审期间继续改分支。
- **影响**：不影响运行正确性；影响可审计性与“PR 审的是哪一版”的确定性。
- **建议**：契约 schema 单独 `docs(contract)` commit；复审期间冻结分支或明确
  标注“复审对象 commit”，避免报告与 HEAD 漂移。

## 4. 明确没能验证的部分

以下不是“应该没问题”，是**没有条件/没有做**：

1. **Windows / mac-x64 / win-arm64**：本机只有 macOS arm64；FTS5、`realpath`、
   大小写折叠、路径分隔符在 Windows 上没有实机证据。F10 文档没有宣称三平台，
   但 `MemoryProjection` 的 sqlite/FTS5 行为不能从 macOS 外推。
2. **真实断电 / fsync / WAL 恢复**：没有做真断电；探针只做“异常回滚 + 进程
   重启”。记忆写没有 failpoint，`TIANSHU_FAILPOINT` 没有覆盖记忆事务。
3. **Worker / daemon 端到端跑记忆工具**：复审用直接 AgentLoop + CLI 子进程；
   Worker/daemon 的 `AgentLoop` 没有传 `memory=stack.memory`（D6），也没有一条
   daemon 驱动的 memory.search/propose 黑盒用例。UI/Host 路径没有记忆。
4. **两个真子进程并发写/确认同一条记忆**：没有写这样的并发探针；D5 的
   “apply_event 校验 revision 会拒绝陈旧写入”是代码推理，不是压测证据。
   跨进程的真实 approval+resume（CLI 批、另一个进程 resume）也没有跑；本复审
   只跑了“父进程 propose、子进程 CLI confirm”和“子进程重启后 search/get”。
5. **Windows 原生 sqlite/FTS5 行为**：同上 1。
6. **重放独立探针**：`test_replay_uses_the_snapshot_not_the_latest_memory` 通过，
   但我没有另写 replay 探针；D1 的“事件正文”事实已经由 `probe_event_content`
   证明，不依赖 replay 实现。
7. **物理擦除/保留策略**：本阶段明确不做；但 D1/D2 让“将来能擦除”更难，属于
   契约问题，不是本报告替作者验收的部分。
8. **大规模检索/超长 query/DoS**：没有测 FTS5 表达式深度、超长 content、超大
   `source_refs`、并发读 `ensure_schema` 的锁竞争（D11）。
9. **`memory://` 在所有平台的 realpath 一致性**：macOS 上相对 URI 解析到 CWD
   已复现（D8）；Windows 未验。
10. **L2/L3 蒸馏、向量召回、freshness awareness**：未实现，按文档不算本 PR
    验收项；没有测试。

## 5. 总体结论

- **授权旁路（重点 A）**：**未发现高危绕过**。
  - 记忆分支提前 return 跳过的三条，逐条论证：
    - `assert_covers`：记忆资源没有目录，`memory_scope_allowed()` 用 canonical
      根/任务 id 做了更严格的精确匹配；文件资源仍在同一分支外走原租约。
    - `held_paths` 非空：记忆没有目录租约；文件动作在同一 Run 没有租约时仍被
      `dispatch.lease_lost` 拒绝（`probe_auth.py` A4）。
    - `ensure_not_blocked`：记忆不落在目录里，当前路径没有文件副作用；但**阻断的
      workspace 里仍能 propose/confirm 记忆**（A5 实测 admitted/applied），设计上
      应明确这是“不适用”还是“应阻断”，目前无测试、文档也没说。不是文件旁路。
  - `memory://` 与 file 资源双向冒充、混合资源、零绑定控制 Run 文件写入、模型
    伪造 `origin=user_explicit` 都被拒绝；`file` 路径的授权判定与 `main` 一致。
- **同事务（重点 B）**：**默认装配成立，但不是结构性强制的**。
  - `probe_tx.py` T2 在 `apply_event` 已改投影后再抛异常：投影回到
    candidate/1，事件只剩 `failed/unknown`，无 succeeded `tool.result`、无
    receipt、dispatch `error`；`T3` 直接事件+投影一起回滚；`T4` `verify_receipts` 空。
  - 但 D5 的错装配探针能让“事件库回滚、投影库已提交”分叉；D6 的 Worker/daemon
    会开第二个投影。也就是说，**“同事务”现在是装配约定，不是 Dispatcher/
    AgentLoop 边界上的硬校验**。
- **忘记**：search / rebuild / 进程重启 / 删索引重建 四条都过（`probe_forget.py`）；
  但 `memory.get` 仍按 id 返回 forgotten 正文（D10），语义需明确。
- **检索**：过滤→排序→裁剪成立；FTS bm25 不参与最终排序；5 条 / 4 KiB 截断
  实测 4095/4094 字节；CJK 与精确标识符召回可用；作用域隔离成立。模型不能
  要求更少（D9）。
- **写入闸门 / 去重**：投影/blob/事件无密钥/PII；去重不增长索引。但
  `dispatch_requests` 明文落原文（D2），不能按“已入库前阻断”写 ✅。
- **老路径**：`ActionRequest.to_canonical_dict()` 无新字段；固定 file 请求的
  `digest/action_digest` 在 `main` 与 HEAD 逐字节相同；13 个 policy 判定逐项
  相同；真实 file dispatch 的事件序列/结果/凭据/verify 除临时路径与随机 id 外
  一致。**F 项成立。**
- **文档真实性（重点 G）**：§13 的“未落地”清单基本诚实；但 §14 的“编辑保留
  revision”是过度陈述（D3），PII 行把投影级通过写成全链路通过（D2），
  `memory://workspace/relative` 与 `memory.search limit` 两处与 §6.1/§8 不一致
  （D8/D9），KNOWLEDGE 仍写“未提交/1212”（D12），§14 “没有 origin 测试”是
  错的（D13）。§13/§14 引用的测试函数名全部存在，这一点已核对。

**能不能合**：

- 作为“F10 L1 显式记忆”的**安全主路径**，授权、审批、事务、遗忘、检索、去重
  基本成立；但作为 PR #64 的**验收**，**不能合**。
- 阻断项：D1（高危契约违背，事件日志正文）必须改代码或改 §3.1 契约；D2（明文
  PII/密钥进 `dispatch_requests`）必须在落库前处理，不能记成“已覆盖”。
- 同批建议：D3（编辑至少改成未覆盖，最好补工具）、D4（workspace/task 创建入口
  如实登记）、D5/D6（在 Dispatcher/AgentLoop 边界强制同一投影，Worker/daemon
  传 `stack.memory`），D7-D14 文档/测试收口。
- 最小可合条件：D1 + D2 有代码/契约结论；D3/D4 在 §13/§14 如实降级；D5/D6
  加边界校验与回归；D7-D14 修文档/测试。若作者只修 D1/D2 就合，仍需在 PR 描述
  里保留 D3/D4 的未实现声明，不能写“F10 L1 已交付”。