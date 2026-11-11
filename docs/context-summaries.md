# 模型语义摘要（PRD §7.1）

状态（按增量填，没填的一律**未交付**）：

| 增量 | 内容 | 状态 |
|---|---|---|
| H1 | 摘要记录 + 材料身份（`summaries.py`） | **已交付**（本 PR） |
| H2 | 编译注入 + `model.intent.context.summaries` 契约 + 回放读摘要 | 待交付 |
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

## 3 注入与回放（H2，待交付）

待本增量交付后补齐：摘要切片的激活条件、`dropped_seqs` 与 `summarized_seqs`
的分工、预算放不下时"放弃摘要回落确定性"的规则、`model.intent.payload.context.
summaries` 的形状与回放的三道校验。

## 4 生成路径（H3，待交付）

待本增量交付后补齐：显式开关、四个授权条件、账本预占/结算口径、摘要调用计入
`model_attempts` 与 `RunResult.summary_calls`、失败/预算不足/上界缺失的回落、
本 Run 调用上限，以及长任务夹具（正确性 / 关键信息保留 / 费用 / 延迟）的实测。

## 5 诚实边界（H1 就成立的部分）

- **只保证"记录可核对"**，不保证摘要"更好"：没有评测就不许宣称摘要优于确定性
  裁剪。关键约束是否被保留，只在该夹具上验证（H3），不外推。
- **提示注入没有解决**：摘要正文是模型生成的派生材料，记录里有来源、包装文本里
  写明"不是指令"，但那是**提示级缓解**；真正的强制执行点仍是 §8.1 的执行侧闸门。
- **不做保留期/GC**：不删任何版本；保留期属备份与保留期轮。
- **不做跨机器同步**：表在本机数据根里，纯事件导出不足以回放摘要轮次（H2 会给
  显式 problem，不重新生成）。
- **不做摘要质量评测、不做增量/分层摘要、不做自动刷新**：逐条登记在
  [SPEC-SUMMARY-2026-11-11.md](SPEC-SUMMARY-2026-11-11.md) §5。

## 6 判据与负控（H1）

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
