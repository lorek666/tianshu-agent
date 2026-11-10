# F10 记忆 · 独立复审处置账

> **复审对象**：`feat/memory`（PR #64）HEAD `c4216e1`——复审开始时是 `80cb261`，期间作者又提交了
> `dd0fa37` / `fc09c78` 并合入 `main@540c543`（`c4216e1`）。复审证据固定在该 commit，
> 报告见 [REVIEW-F10-memory.md](REVIEW-F10-memory.md)（442 行，含 D1–D15 的 `file:line`、
> 复现命令、关键输出与"没能验证"清单）。
>
> **复审结论**：**不能按"已验收"合并**——安全主路径（授权 / 审批 / 事务 / 遗忘 / 检索 / 去重 /
> 老 file 路径）总体成立，但 1 项高危契约违背 + 5 项中等缺陷。
>
> **本文**：逐条处置。**每条只有三种结局：已修（有可复现证据）、如实改文档（实现不改）、
> 仍未修（写进 §13 缺口，不当已完成）。** 不把"部分修"记成"已解决"。

## 1. 总账

| # | 严重度 | 一句话 | 处置 | 证据 |
|---|---|---|---|---|
| D1 | 高 | 记忆正文进入 append-only 事件日志，F10 §3.1「事件只存引用」不成立 | **改文档（架构裁定）**，另修掉 `memory.get` 正文重复 | §3.1 / §11 改写；`loop._record_result` 行为未改 |
| D2 | 中 | `dispatch_requests.args_payload` 在写入闸门前落明文 | **部分修**：被拒请求已脱敏；被采纳后遭闸门挡掉的仍留原文 | `test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`；§13 #26 |
| D3 | 中 | `supersede` / 编辑无生产入口，§14 却标 ✅ | **改文档**（标未兑现） | §13 #24；§14 该行改"部分覆盖" |
| D4 | 中 | workspace / task 作用域无创建入口 | **改文档**（标未兑现） | §13 #25 |
| D5 | 中 | 「同事务」只在投影构造处强制，Dispatcher 边界不校验 | **已修** | `test_dispatcher_refuses_a_projection_from_another_event_log` |
| D6 | 中 | Worker / daemon 的 `AgentLoop` 没传 `stack.memory`，会开第二个投影 | **已修**（含 eval / 测试替身） | 4 个构造点统一 `memory=`；全量套件绿 |
| D7 | 轻 | CLI 不支持 task 作用域 confirm/forget，文档未写 | **改文档** | §13 #27 |
| D8 | 轻 | `memory://workspace/relative` 被 realpath 到 CWD | **已修** | `test_relative_workspace_scope_is_rejected_before_realpath` |
| D9 | 轻 | `memory.search` schema 无 `limit`，§8 却写"可以要求更少" | **已修** | `test_memory_search_schema_exposes_limit_capped_at_five` |
| D10 | 轻 | forget 后 `memory.get` 仍返回正文 | **已修** | `test_forgotten_memory_cannot_be_revived_by_id` |
| D11 | 轻 | 读路径每次 `ensure_schema()` 开 `BEGIN IMMEDIATE` | **已修** | `MemoryProjection.ensure_schema` 就绪后只做只读版本核对 |
| D12 | 轻 | KNOWLEDGE 仍写"未提交 / 1212" | **已修** | `docs/KNOWLEDGE.md` §2 |
| D13 | 轻 | §14 说"没有断言 origin 的测试"，实际有 | **已修** | 指向 `test_agent_propose_then_confirm_through_dispatcher` |
| D14 | 轻 | §14"新任务"证据只是 loadout；忘了后测试复用同一 Task | **部分修**（如实标注取证口径） | §14 该行改述为"后来的 task 装备" |
| D15 | 轻（流程） | 契约 schema 没跟 `docs(contract)` 一起提交；复审期间 HEAD 移动 | **补契约提交**；HEAD 移动如实记录 | 本批新增 `docs(contract):` 提交（`memory.search` 的 `limit`） |

## 2. 为什么 D1 改文档而不是改实现

复审的建议是"`memory.search`/`memory.get` 的 `tool.result` 只写 `content_ref`，replay 按快照读"。
我没有采纳，理由是**它会把红线 1 换成另一个更弱的性质**：

1. `tool.result` 里的 `output` 就是**模型当时看到的那段文本**。把它拿掉，`replay` 就重建不出
   模型当时的输入，`messages_hash` 立刻对不上——而"日志自足、可离线回放"正是红线 1 的内容。
2. 若改成 replay 时按 `content_ref` 去读快照：快照是可删的（`forget`/将来的擦除都会动它），
   于是**历史可回放性取决于一份允许被删的副本**——这比"日志里留 4 KiB 正文"弱得多。
3. 更根本的是：正文**必然**进日志的写入侧本来就有（模型是 `propose` 的 `content` 作者）——
   只不过已经被 `Dispatcher._intent_args()` 脱敏成 `sha256`+长度。读侧留着是想清楚过的代价，
   不是漏删。

代码实测：`tool.intent` 里**没有**正文（`_intent_args` 生效），只有 `tool.result` 的读输出有。
所以准确表述是「**写**事件只存引用；**读**事件保留模型看到的正文快照，日志随检索增长（每次
≤ `MAX_RECALL_BYTES` = 4 KiB）」。§3.1 已按此重写，并把「`forget` ≠ 物理擦除；正文在盘上的
副本 = 事件日志读快照 + 正文 blob」写进契约——与 §10 原本就有的"物理擦除单开工单"对齐，
不再自相矛盾。

顺手修掉了复审指出的**重复写**：`memory.get` 原先把正文放两遍（顶层 `content` 与
`memory.content`）。

## 3. D2 为什么只修一半

`args_payload` **会被读回来**做 `args_hash` 绑定校验，执行器也**从这张表取参数**——所以
"落库前统一脱敏"直接把派发打红（我先试过，11 条派发测试立刻红："票据参数与请求 args_hash
不一致"）。能安全脱敏的只有**永远不会执行的行**：

- **被拒绝**（`_reject`）的记忆写请求 → 已脱敏成 `content_sha256` + `content_bytes` + `content_redacted`。
  拒绝的行不会重派、不会执行，脱敏不破坏任何绑定；审计仍能核对"要写的是同一段正文"。
- **被采纳后**又被内容闸门（PII / 密钥）挡掉的请求 → **仍保留原文**。闸门在
  `MemoryExecutor` 里跑，而请求行在 `prepare()` 就落库了。彻底修需要把内容扫描挪到落库之前，
  或给审计表定保留 / 擦除策略——这是契约级改动，单开工单，见 §13 #26。

所以 §14 那条判据现在是**部分覆盖**：投影与正文 blob 上的闸门成立，派发审计表不在覆盖内。

## 4. 本轮代码改动清单

- `src/tianshu/dispatcher.py`：`__init__` 校验 `memory.conn is self._conn`（否则 `TianshuError`）；
  `_audit_args()` 只用于 `_reject`。
- `src/tianshu/memory.py`：暴露只读 `log` / `conn`；`ensure_schema()` 就绪后短路，只做只读
  分词器版本核对（`_verify_tokenizer_version`，meta 缺失时退回完整建表，不在空库上假装就绪）。
- `src/tianshu/resources.py`：`parse_memory_uri` / `MemoryScope.workspace` **先判绝对路径再
  realpath**。
- `src/tianshu/loop.py`：`memory.get` 对 `forgotten` 不读正文（存在性仍如实报告）；错误文案按
  原因区分。
- `src/tianshu/tools.py`：`memory.search` schema 增加 `limit`（1..5）。
- `src/tianshu/worker.py` / `daemon.py` / `eval/runner.py` / `tests/support.py`：统一
  `memory=stack.memory`。

新增回归 5 条：`test_relative_workspace_scope_is_rejected_before_realpath`、
`test_dispatcher_refuses_a_projection_from_another_event_log`、
`test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`、
`test_memory_search_schema_exposes_limit_capped_at_five`、
`test_forgotten_memory_cannot_be_revived_by_id`。

## 5. 复审明确说"没能验证"的，我不代它背书

Windows / mac-x64 / win-arm64 实机、真实断电与 fsync、daemon/worker 端到端驱动记忆工具、
两个真子进程并发写/确认同一记忆、跨进程"CLI approve + 另一进程 resume"、UI/Host 记忆路径、
recovery 的记忆崩溃分支、L2/L3/向量/freshness、大规模检索与锁竞争——**以上一律仍属未验证**。
本 PR 的 CI 只在 ubuntu / macOS 两个 required check 上跑过；Windows 是跑不挡。

## 6. 仍留的缺口（不要读成已完成）

- 编辑（`supersede`）与 workspace / task 作用域**创建**没有生产入口（§13 #24 / #25）。
- 被采纳的记忆写请求仍可能把正文原文留在 `dispatch_requests`（§13 #26）。
- **物理擦除未实现**：`forget` 只改状态，不删正文 blob、不动事件日志（§13 / §10）。
- L2/L3 蒸馏、向量召回、`origin` 的两档迁移、§8 作用域优先级、recovery 的记忆分支：均未做
  （§13 #18–#23）。