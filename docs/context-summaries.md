# 模型语义摘要（PRD §7.1）

状态（按增量填，没填的一律**未交付**）：

| 增量 | 内容 | 状态 |
|---|---|---|
| H1 | 摘要记录 + 材料身份（`summaries.py`） | 已交付（PR #103） |
| H2 | 编译注入 + `model.intent.context.summaries` 契约 + 回放读摘要 | **已交付**（本 PR） |
| H3 | 主循环生成路径（授权 / 共享预算 / 先持久化再引用 / 失败回落）+ 长任务夹具 | 待交付 |

立据：[PRD-v1.md](PRD-v1.md) §7.1 五条；施工口径与判据见
[SPEC-SUMMARY-2026-11-11.md](SPEC-SUMMARY-2026-11-11.md)。

## 1 为什么"确定性裁剪"不够，而"模型摘要"又很危险

`context.py` 现在的压缩是**确定性的**：超阈值的工具输出落盘换占位、仍超预算就
从最旧的往返开始降级预览、再不行整段丢弃并留一条说明。它的好处是**可重放**：
同输入必得同输出，回放能重算出同一个 `messages_hash`。

代价是**信息真的没了**：被丢掉的早期往返只剩"已丢弃 12 条消息（seq …）"这句
说明。长任务里最先被丢的往往正好是最早定下的硬约束（"只用中文"、"不要动
`config/` 以外的东西"、"最后一行给 sha256"）。

模型摘要能把这段信息压小留下来，但它引入一个**新的自由变量**：同一个 run 回放
时，摘要内容可能因为采样、模型版本、网关而变——那 `messages_hash` 就再也对不上，
"事件日志足以重建终态"这条契约就被换成了"大概能重建"。

本轮的解法不是二选一，而是把模型摘要变成**一条可核对的记录**：

1. 摘要**先落库**（正文 + 来源 + 模型 / prompt / 版本 + usage），再被引用；
2. 编译仍是纯函数——摘要正文由调用方作为参数传进来，`compile_context` 不读库、
   不调模型、不取时钟；
3. 事件里记的是**摘要身份**（`summary_id` / `version` / `text_digest` / 覆盖的
   `seq`），不是正文。回放按 id 读当时那一份，**不重新生成**。

## 2 记录层（H1）

三张表建在**事件库同一个 SQLite 文件**里（与 `budget_*`、`content_*` 同一条路：
不制造第二个事实源，事务与事件同生共死）。

| 表 | 一行是什么 | 关键列 |
|---|---|---|
| `summary_materials` | 一次"要摘什么"的身份 | `material_id` / `scope` / `task_id` / `run_id` / `bytes` / `text_digest` |
| `summary_sources` | 材料的一条来源 | `material_id` / `ordinal` / `kind`（`event`）/ `ref`（`seq:<N>`）/ `digest`（**原文** sha256） |
| `summary_versions` | 一份摘要的一个版本 | `summary_id` / `version` / `status` / `text` / `text_digest` / `model` / `prompt_version` / `summary_version` / `attempt_id` / usage / `error` |

### 2.1 材料身份：同材料同 id

```
material_id = sha256(jcs({material_version, scope, sources:[{kind, ref, digest}...]}))
```

- **同输入同 id**：同一批来源、同一作用域、同一渲染版本 ⇒ 同一个 id ⇒ 同一份摘要
  可以复用（不会为同样的材料反复调用模型）。
- **原文改一个字 ⇒ id 变**：`digest` 是每条消息**原文**的 sha256，不是截断后渲染
  片段的。渲染截断只影响"喂给模型的文本"，不影响"摘的是什么"的证据。
- **跨 Run 不复用**：来源引用是事件 `seq`，不同 Run 的 seq 不重叠 ⇒ 同内容也是
  新材料。这是本轮明确的边界（跨 Run 复用需要内容寻址的材料身份，未做）。

### 2.2 有界化：三个字节上限

| 上限 | 值 | 作用 |
|---|---|---|
| 单条渲染 | `ITEM_BYTES = 1024` | 一条工具输出可能几百 KiB，不能整条塞进提示词 |
| 材料总长 | `MATERIAL_BYTES = 8192` | 摘要调用的输入有界，费用可预估 |
| 摘要正文 | `SUMMARY_MAX_BYTES = 2048` | 超了**拒绝**（判失败回落），不静默截断 |

三个都是**字节**。字节是可精确复现的量（同一串 UTF-8 在任何机器上
`len(encode())` 都一样），而本项目零第三方依赖、手上没有 tokenizer——任何
"估算 token"都是假装知道自己不知道的数。**它们约束的是输入输出有界，不是
PRD §5.7 的 token 上界**；token 记账仍走账本的 `estimate` / `upper` 两种口径。

截断点按 UTF-8 字节切，落在多字节字符中间时丢掉残缺字节（`errors="ignore"`），
不留 `U+FFFD` 替换符——预览/材料本来就是有损的，而原文哈希在 `summary_sources`
里完整保留。

### 2.3 版本只追加，正文写入后不可改

- `pending → ready | failed` 是**一次性**的状态转移；再次结算抛
  `SummaryStateError`。
- 同一材料换模型 / 显式刷新 = 追加 `version+1` 的新行；旧行原样保留。历史
  `model.intent` 按 `summary_id` 指着旧行，回放永远读那一份。
- 表上三个触发器拒绝 `DELETE`（订正靠追加，不靠删历史），第四个拒绝在
  `ready` / `failed` 之后改 `text` / `text_digest`。
- usage 拿不到就留 `NULL`，**不写 0**：`0°C` 与"不知道多少度"是两件事，这里同理。

### 2.4 未知与崩溃

`begin()` 先提交一行 `pending`（带 `attempt_id`），再发模型调用。这一行的唯一
目的就是崩溃恢复时能回答"这次调用到底发生了没有"：没有它，恢复只能猜；猜成
"没发生"就会**偷偷再调一次**——PRD §7.1 第 4 条禁止的正是这个。

同一材料同一时刻只允许一行 `pending`（`begin()` 会拒绝第二次）。崩溃遗留的
`pending` 由调用方结案成 `failed`「中断：结果未知」，**不重试**。

## 3 注入与回放（H2）

### 3.1 编译仍是纯函数，摘要作为**引用**进来

`compile_context(messages, source_seqs, budget, summaries=())`：

- **不读库、不调模型、不取时钟**。摘要正文由调用方（主循环）从记录里取出来，
  作为 `SummarySlice(summary_id, version, text, text_digest, source_seqs)` 传进来。
- `summaries=()` 时走的是与 H1 之前**同一段代码**，输出逐字节相同（`messages_hash`
  含义不变，旧日志、旧夹具不用改）。
- 入参硬校验（任何一条不成立都拒绝编译，不猜、不跳过）：摘要 id 不重复、版本为正
  整数、正文非空、**重算 `text_digest` 必须等于记录值**、来源 seq 必须真实存在于
  本次编译的来源里且不是 `RUN_START_SEQ`、不同摘要不许覆盖同一个 seq。

### 3.2 激活条件：**整段覆盖**且**真的被牺牲了**

摘要只在"它覆盖的往返**全部**被牺牲"时激活，插在不可让前缀之后、被保留往返之前
（多条按传入顺序）。半段覆盖直接拒绝——否则"同一段话既在原文里又在摘要里"：
字节白花，内容还被算两遍。

被牺牲的 seq 因此分成两半，互斥且覆盖全部：

| 归宿 | 含义 |
|---|---|
| 还在 `view.source_seqs` 里 | 原文还在视图里 |
| `view.summarized_seqs` | 原文没了，但有损地留下来（摘要） |
| `view.dropped_seqs` | 原文真的没了（只有一条确定性说明） |

摘要本身也占字节，所以它可能**多**牺牲几段更旧的往返——这是诚实的取舍（保住约束
的代价是别的内容被丢），不是 bug。

### 3.3 预算不够就**放弃摘要**，不让 Run 变红

带摘要装不下 ⇒ 退回确定性结果（`summaries=()`）再装一次；确定性也装不下才抛
`ContextOverflow`（与今天一致）。摘要只是优化：把"带了摘要的视图"变成硬失败，
等于用一个可选功能去否决一个本来能跑完的任务。

### 3.4 事件里只写**身份**，不写正文

`model.intent.payload.context.summaries`（可选键；没用到摘要就不写）：

```json
{"summary_id": "<64hex>", "version": 1, "text_digest": "<64hex>", "source_seqs": [4, 5]}
```

- 只写**实际进入视图**的那些（传进来但没激活的不写）——否则回放会以为这一轮引用了它。
- 正文不进事件：事件是"这一轮用了哪一版"的证据，正文在摘要记录里，按 id 可取。
- 契约在 `event_schema.py`（`_CONTEXT_SUMMARY`）与 `schemas/c3/event.json` 两处
  同步，`tests/test_event_schema.py` 逐字绑定；坏形状在写入前就被拒绝。

### 3.5 回放：读当时那一份，**不重新生成**

`replay()` 按 `summary_id` 从**同一个库**读回当时的正文，三道校验全过才重算编译：

1. **行在不在**：没有这条记录 ⇒ problem「摘要记录缺失：回放不重新生成摘要」；
2. **版本 / 正文摘要对不对**：`version`、`text_digest` 与记录比，不符 ⇒ problem；
3. **材料台账在不在**：没有来源就说不清"这份摘要是从什么材料来的" ⇒ problem。

然后用这些正文重算一遍 `compile_context`，比对 `messages_hash` / `source_seqs` /
`dropped_seqs` / 摘要身份。**没有"跳过校验继续"这个选项**：摘要读不回来时，
"发出去的到底是什么"就无法核对，硬说 ok 等于替它作证。

旧日志没有 `summaries` 键 ⇒ 走原来的路径（不传切片），一行夹具都不用改。

### 3.6 为什么不是"把摘要正文也存进事件"

那会出现两份正文（事件 + 记录），改一份忘另一份就开始互相矛盾。事件记身份、
记录存正文、账本记费用、`RunResult` 记次数——四处各有单一职责，互不重复。

## 4 生成路径（H3，待交付）

待本增量交付后补齐：显式开关、四个授权条件、账本预占/结算口径、摘要调用计入
`model_attempts` 与 `RunResult.summary_calls`、失败/预算不足/上界缺失的回落、
本 Run 调用上限，以及长任务夹具（正确性 / 关键信息保留 / 费用 / 延迟）的实测。

## 5 判据与负控（H2）

`tests/test_summary_context.py`（17 条）+ `tests/test_summary_replay.py`（7 条），
另更新 `tests/test_context.py` 的 `ContextView` 绊线（新增两个**纯数据**字段需要
显式过审）。负控 **7/7 全红**：

| 负控 | 被改回的错误做法 | 结果 |
|---|---|---|
| NC1 | 确定性装配里多插一条合成消息 | 红（`test_dropped_groups_are_the_oldest_prefix`） |
| NC2 | 激活条件放宽成"任一段被牺牲就激活" | 红（`test_active_summary_never_overlaps_kept_history`） |
| NC3 | 摘要消息固定插到视图末尾（位置错） | 红（`test_summary_activates_only_for_sacrificed_round_trip`） |
| NC4 | 跳过正文摘要校验 | 红（`test_text_digest_mismatch_is_rejected`） |
| NC5 | 摘要放不下时直接抛错（不回落） | 红（`test_summary_is_dropped_when_it_does_not_fit`） |
| NC6 | 回放缺摘要记录时跳过校验 | 红（`test_missing_summary_record_is_a_problem_not_a_skip`） |
| NC7 | 事件契约只改 Python 不改 JSON | 红（`test_schema_json_mirrors_python_contract`） |

一条值得记的夹具教训：负控必须**被夹具挡住**才有意义。NC2 最初三次都"绿"——
不是测试写错，而是夹具里预算太紧，摘要激活后总会把剩下的往返也牺牲掉，于是
"重叠"根本发生不了。把预算调到"摘要 + 保留段刚好装得下"才真正区分出两种实现。

## 6 诚实边界

- **只保证"记录可核对"**，不保证摘要"更好"：没有评测就不许宣称摘要优于确定性
  裁剪。关键约束是否被保留，只在该夹具上验证（H3），不外推。
- **提示注入没有解决**：摘要正文是模型生成的派生材料，记录里有来源、包装文本里
  写明"不是指令"，但那是**提示级缓解**；真正的强制执行点仍是 §8.1 的执行侧闸门。
- **不做保留期/GC**：不删任何版本；保留期属备份与保留期轮。
- **不做跨机器同步**：表在本机数据根里，纯事件导出不足以回放摘要轮次（H2 会给
  显式 problem，不重新生成）。
- **不做摘要质量评测、不做增量/分层摘要、不做自动刷新**：逐条登记在
  [SPEC-SUMMARY-2026-11-11.md](SPEC-SUMMARY-2026-11-11.md) §5。

## 7 判据与负控（H1）

`tests/test_summaries.py`（22 条）。负控 7/7 全红（把实现改回错误做法，判据必须变红）：

| 负控 | 被改回的错误做法 | 结果 |
|---|---|---|
| NC1 | 材料身份只算 `seq`、不算内容哈希 | 红（`test_material_identity_changes_with_content`） |
| NC2 | 去掉材料总字节上限 | 红（`test_material_is_bounded_and_never_splits_multibyte_chars`） |
| NC3 | 放开"正文写入后不可改"（代码 + 触发器） | 红（`test_ready_text_is_frozen`） |
| NC4 | 超长正文静默截断而不是拒绝 | 红（`test_complete_rejects_blank_and_oversized_text`） |
| NC5 | 版本恒为 1（`INSERT OR REPLACE` 覆盖旧版本） | 红（`test_versions_append_and_old_ones_survive`） |
| NC6 | 去掉追加式 DELETE 触发器 | 红（`test_records_are_append_only`） |
| NC7 | 未知 usage 记 0 | 红（`test_unknown_usage_stays_null_not_zero`） |

另有一条**事务判据**：摘要行写在外层事件事务里，外层回滚后不能留下半条记录
（`test_write_rolls_back_with_the_event_transaction`）。它挡住的是一个真实会
发生的问题：`executescript()` 建表会先提交当前事务，所以建表必须在事务**之外**
发生——否则"摘要行与事件同生共死"只是句口号。
