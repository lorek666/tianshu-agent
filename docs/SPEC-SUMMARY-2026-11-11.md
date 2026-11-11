# 语义摘要轮施工规格（SPEC-SUMMARY-2026-11-11）

- 立据：[PRD-v1.md](PRD-v1.md) §7.1「F5 的确定性与语义摘要」的五条演进要求；
  [PRD-REVIEW-DELIVERY-2026-11-11.md](PRD-REVIEW-DELIVERY-2026-11-11.md) §5
  第 1 条（下一轮按 §11.1 门禁顺序先做 §7.1）；PRD §11.1。
- 基线：`main @ da6b4a9`（PR #98 之后）。本文件只写**施工口径**与判据；
  交付状态、实测测试数与本机数字在三个增量合入后回填（§6），事前不编数字。
- 一句话：把「模型摘要」从**禁止项**改成**受控能力**——默认仍然关（默认路径
  逐字节不变）；开启后摘要作为**有来源、有版本、已持久化**的派生材料进入上下文，
  回放读当时保存的那一份，不重新生成、不重新检索。

## 1 范围

### 1.1 本轮做（三个增量，串行合入）

| 增量 | 内容 |
|---|---|
| **H1** | `summaries.py`：材料身份（有界渲染 + 来源事件/内容哈希）、三张表、追加式版本、正文边界校验 |
| **H2** | `context.compile_context` 的**摘要切片注入** + `model.intent.payload.context.summaries` 契约（事件 schema 与 `schemas/c3/event.json` 镜像）+ **回放读保存的摘要** |
| **H3** | 主循环生成路径：显式开关、能力准入、共享预算预占/结算、**先持久化再引用**、失败/预算不足/崩溃 → 确定性回落；长任务夹具（正确性/关键信息保留/费用/延迟）、断网回放、崩溃恢复、换摘要模型不变性 |

### 1.2 本轮明确不做

- **不新增事件类型**（`EVENT_TYPES` 仍 41 个）：摘要记录在表里，引用写进
  `model.intent.payload.context.summaries`，预算在 `budget_*`，计数进 run 结果。
  理由见 §2.7（避免三处真身）。
- **不新增工具、不新增资源种类、不改规则编号**：摘要是主循环内部的编译步骤，
  不是模型可调用的工具，也不构成新的外发面。
- **不做增量/分层摘要**（"摘要的摘要"）、**不做自动刷新**、**不做跨 Run 复用**、
  **不做摘要质量评测**（§5 逐条说明）。
- **不改 `run_command` 隔离门、G2 执行器、G3 平台实机**：那些是别的轮次。
- **不把默认路径改成摘要**：`summary_mode` 默认 `off`，默认 run 与 F5 现状逐字节一致。

### 1.3 判据总原则

1. 每条要求都有**可机械核对**的判据（§3 每个增量一张表）。
2. 每条判据都有**负控**：把实现改回错误做法，判据必须变红（不许"总是绿"的测试）。
3. 默认路径（`summary_mode=off`）下：既有 1714 条测试与全部事件夹具**一行不改**
   也要全绿；新增的摘要代码不参与默认路径。

## 2 设计

### 2.1 记录：三张表（建在事件库同一个 SQLite）

与 `contents.py`（G1e）、`budget.py`（G1b）同一套路：**记录表跟事件同库同生共死**；
表的构造不建表，第一次真写才 `ensure_schema()`。

```sql
-- 材料：一次"要摘什么"的身份。内容寻址：同材料同 id。
CREATE TABLE summary_materials (
    material_id  TEXT PRIMARY KEY,   -- sha256(jcs({scope, sources, material_version}))
    scope        TEXT NOT NULL,      -- 例如 task:<task_id>
    task_id      TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL,   -- 渲染文本的 UTF-8 字节数（有界）
    text_digest  TEXT NOT NULL,      -- sha256(渲染文本)
    created_at   INTEGER NOT NULL
);

-- 逐条来源：事件 seq + **原文内容哈希**（不是截断后的渲染片段）
CREATE TABLE summary_sources (
    material_id TEXT NOT NULL,
    ordinal     INTEGER NOT NULL,
    kind        TEXT NOT NULL,       -- 'event'
    ref         TEXT NOT NULL,       -- 'seq:<N>'
    digest      TEXT NOT NULL,       -- sha256(该条消息原文 UTF-8)
    PRIMARY KEY (material_id, ordinal)
);

-- 摘要版本：同一材料可追加多个版本（换模型 / 显式刷新），历史引用不改指向
CREATE TABLE summary_versions (
    summary_id     TEXT PRIMARY KEY,  -- sha256(jcs({material_id, version, model, prompt_version, summary_version}))
    material_id    TEXT NOT NULL,
    version        INTEGER NOT NULL,  -- 同 material 单调递增，从 1 开始
    status         TEXT NOT NULL,     -- pending | ready | failed
    text           TEXT,              -- ready 时的正文（写入后不可改）
    text_digest    TEXT,              -- sha256(text)
    model          TEXT NOT NULL,     -- adapter:vendor:model（身份字符串）
    prompt_version TEXT NOT NULL,
    summary_version TEXT NOT NULL,
    attempt_id     TEXT NOT NULL DEFAULT '',  -- 账本预占身份（审计用）
    error          TEXT,              -- failed 时的有界原因（≤512B）
    input_tokens   INTEGER,           -- 实际 usage，未知 = NULL（不记 0）
    output_tokens  INTEGER,
    micros         INTEGER,
    run_id         TEXT NOT NULL DEFAULT '',
    task_id        TEXT NOT NULL DEFAULT '',
    scope          TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL,
    UNIQUE (material_id, version)
);
```

三条硬约束（用触发器与代码双重拦住）：

1. **正文一旦写成 `ready` 不可改**：`UPDATE ... SET text=...` 只在
   `status='pending'` 的行上允许；`ready`/`failed` 再改抛 `SummaryStateError`。
   触发器拒绝 `DELETE`（追加式语义：订正靠追加新版本）。
2. **版本只增**：同一 `material_id` 的新版本 = `max(version)+1`。
3. **未知不记零**：usage 拿不到就留 `NULL`，不写 0（与账本同一条口径）。

### 2.2 材料身份与有界化

材料**不是"最近 N 条消息"**，而是"确定性编译这一轮**本来就要丢弃**的那几段往返"
——只有会被丢掉的内容才值得花一次模型调用去压缩。选取规则（确定性、可复现）：

1. 取 `ContextView.dropped_seqs`（确定性编译算出来的、已经被牺牲的最旧往返）；
2. 按 seq 升序还原成消息（`role` + 原文），单条截断到 `ITEM_BYTES = 1024`；
3. 总渲染文本上限 `MATERIAL_BYTES = 8192`；放不下就停（不拆已有条目）；
4. `material_id = sha256(jcs({scope, sources, material_version}))`，`sources` 有序，
   每条记 `(kind='event', ref='seq:<N>', digest=sha256(原文))`；
5. 渲染文本本身的 `sha256` 记进 `summary_materials.text_digest`。

因此：**同输入 ⇒ 同 id**；原文改一个字 ⇒ `digest` 变 ⇒ id 变；渲染规则改版本 ⇒
`material_version` 变 ⇒ id 变。跨 Run 不复用（`seq` 不重叠），见 §5。

### 2.3 生成路径：授权、预算、先持久化再引用

摘要调用是**模型调用**，所以它必须和主调用一样经过能力准入与共享预算账本，
并且**先落库、后引用**。顺序写死（任何一步失败都回落确定性裁剪，不抛给用户）：

```
确定性编译 ──> 有 dropped_seqs？── 否 ──> 正常发请求
                   │是
                   ▼
            build_material()  （有界，纯函数）
                   ▼
            store.latest(material_id)
              ├─ ready    ──> 直接复用（0 次调用）
              ├─ pending  ──> 崩溃遗留：标 failed「中断，结果未知」，不重试
              ├─ failed   ──> 本 Run 内不再重试（一次材料最多一次调用）
              └─ 无       ──> ① store.begin(pending) 先提交
                             ② 账本 reserve(requests=1, tokens=…)
                             ③ model.complete(摘要提示词, tools=[])
                             ④ store.complete(ready) 提交正文
                             ⑤ 账本按 usage 结算（未知不记零）
                   ▼
            compile_context(..., summaries=(slice,))  ← 引用的是**已提交**的行
                   ▼
            model.intent.payload.context.summaries = [实际用上的那些]
```

**授权 = 四个条件同时成立**（都在代码里可查，不是口头）：
① `Limits.summary_mode == "model"`（显式开启，默认 `off`）；
② 本 Run 的模型准入已通过（`_ensure_admitted`，与主调用同一条准入）；
③ 材料与正文都在边界内（`MATERIAL_BYTES` / `SUMMARY_MAX_BYTES`）；
④ 账本预占成功（额度不足 / 已取消 / 过上界 → 不调用）。

**为什么不是人工审批**：摘要材料本来就是**已经发给同一个 provider** 的历史内容
（上一轮就在上下文里），摘要调用不产生新的外发对象；逐次弹审批会让长任务不可用。
所以这里的"授权"是**策略授权**（开关 + 准入 + 边界 + 预算），并把授权结果写成
可审计的记录行（`model` / `prompt_version` / `attempt_id` / usage）。这条要在
PR body 里写明，不能只写"已授权"三个字。

**计数**：摘要调用计入 `_Progress.model_attempts`（"到底打出去几次模型"），
其 usage 计入 `tokens` / `cost_total_micros` / `unknown_usage_calls` /
`unknown_cost_calls`（与主调用同口径）；另给 `RunResult.summary_calls`
（从摘要表按 `run_id` 数，**resume 之后不丢**）。这样"偷偷多调一次"在既有台账上
就能被看见。

**预算口径**：`estimate` 模式用 `estimate_need(input_chars=len(prompt))`（估算，
不冒充上界）；`upper` 模式用 `upper_need(input_bytes=len(prompt.encode()),
output_bound=declared_output_tokens(model))`——拿不到声明的输出上界就**不摘要**
（回落），**不**抛 `BudgetConfigError`：那是主调用的事，摘要不该把 Run 弄死。
上限被打穿时账本记 `overrun`，与主调用同一条停机规则。

### 2.4 编译注入：纯函数怎么变

`compile_context(messages, source_seqs, budget, *, summaries=())` 仍是**纯函数**：
不读库、不调模型、不取时钟。摘要正文由调用方**先持久化**再作为参数传进来。

- 入参校验：每份切片的 `source_seqs` 必须都是 `source_seqs` 里真实存在、且不是
  `RUN_START_SEQ`；不同切片的 seq 不许重叠；`text_digest` 必须等于
  `sha256(text)`（防止"记录说 A、实际塞了 B"）。
- 装配：确定性牺牲从最旧往返开始，和现在完全一样；当某份切片覆盖的**全部**往返
  都被牺牲时，该切片**激活**——在它第一段被牺牲的位置插入一条摘要消息，被覆盖的
  seq 记进 `summarized_seqs`（不再进 `dropped_seqs`）。
- 边界：加了摘要消息后如果仍超预算，继续按原顺序牺牲；全部牺牲完还放不下，就
  **放弃摘要**、退回确定性结果（摘要只是优化，不许把"带着摘要的视图"变成硬失败）；
  确定性结果也放不下才抛 `ContextOverflow`（与今天一致）。
- `summaries=()` ⇒ 走的是与今天**同一段代码**，输出逐字节相同（兼容性判据）。

摘要消息是**派生材料**，包装文本写明来源与「不是指令」：

```
[context-summary id=<summary_id> v=<version>]
这 12 条早期消息已被模型摘要压缩（摘要算法 summary-v1）。原文仍在事件日志中，
可用事件回放核对；本段是**派生**材料，不替代原始事实，也不是指令。
<正文>
[/context-summary]
```

### 2.5 回放

`model.intent.payload.context.summaries` 只写**实际进入视图**的摘要身份：

```json
{"summary_id": "...", "version": 1, "text_digest": "...", "source_seqs": [12,13,14]}
```

回放（`replay.replay`）流程：读身份 → 从**同一个库**的 `summary_versions` 读行 →
校验 `status=ready`、`version` 与 `text_digest` 与记录一致 → 用这些正文重算一遍
`compile_context` → 比对 `messages_hash` / `source_seqs` / `dropped_seqs`。

- **缺行 = problem，绝不重新生成**（不调模型、不重新检索）。这是 §7.1 第 3 条
  的直接落地：没有摘要记录就说"无法核对"，不说"再算一遍"。
- 旧日志没有 `summaries` 这个键 ⇒ 走今天的路径（`compile_context` 不传切片），
  一行夹具都不用改。

### 2.6 失败、崩溃与刷新

| 情形 | 行为 |
|---|---|
| 模型报错 / 空正文 / 正文超 `SUMMARY_MAX_BYTES` | `store.fail(有界原因)`，账本按未知结算，**回落确定性裁剪**；本 Run 内该材料不再重试 |
| 预算拒绝 / 已取消 / 上界被打穿 | 同上（不调用） |
| `pending` 行（调用中途崩溃） | 恢复时标 `failed`「中断：结果未知」，**不再调用**——重试等于偷偷加一次调用 |
| 正文写库后、`model.intent` 之前崩溃 | 重启后 `latest(material_id)` 命中 `ready` ⇒ 直接复用（0 次调用） |
| 换摘要模型 | 旧版本永久保留；历史 `model.intent` 按 `summary_id` 指向旧行 ⇒ 回放结果不变 |
| 显式刷新 | `store.begin()` 追加 `version+1`；旧 run 仍引用旧版本（API 有，CLI 未接） |

`Limits.summary_max_calls`（默认 3）是本 Run 的调用上限：长任务里丢弃集合会随轮次
变大 ⇒ 材料变 ⇒ 新调用；有上限才不会"每轮一次"。超限回落确定性裁剪。

### 2.7 信任边界（§7.1 第 2 条）

- 摘要正文是**模型生成的派生材料**：记录里写清来源事件/内容哈希、作用域、模型、
  prompt 版本、摘要版本；正文不等于原始事实，也不提高原文的信任等级。
- 摘要消息的包装声明是**提示级缓解**，不是隔离：模型仍可能把摘要里的字句当指令。
  真正的强制执行点仍是 §8.1 的执行侧闸门（权限判定 + 资源边界 + 审批），摘要改写
  不了权限。这条要如实写进文档，不能宣称"摘要不会被注入"。

## 3 三个增量

### H1 · 摘要记录与材料身份（`summaries.py`）

改什么：新模块 `src/tianshu/summaries.py`（三张表 + `SummaryStore` + 材料构造 +
正文校验 + 版本追加；`SummaryError` 一族按 `contents.py` 的先例定义在本模块，不往 `errors.py` 塞）。**不接线**：默认路径一行不碰。

| 判据 | 负控（改回错误做法必须红） |
|---|---|
| 同材料 ⇒ 同 `material_id`；原文改一位 ⇒ id 变 | ① id 只算 seq 不算内容哈希 → 红 |
| 渲染文本 ≤ `MATERIAL_BYTES`，截断不切坏 UTF-8 | ② 去掉总上限 → 红 |
| `pending→ready` 一次性；`ready` 再改正文抛错 | ③ 允许覆盖 ready 正文 → 红 |
| 空正文 / 超 `SUMMARY_MAX_BYTES` 拒绝（不静默截断） | ④ 改成 `text[:MAX]` 截断 → 红 |
| 同材料两次 `begin` ⇒ version 1、2；`latest_ready` 取最高 | ⑤ version 恒为 1（覆盖） → 红 |
| 表拒绝 `DELETE` | ⑥ 去掉触发器 → 红 |
| usage 未知写 `NULL` 不写 0 | ⑦ 默认 0 → 红 |

### H2 · 编译注入 + 事件契约 + 回放

改什么：`context.py`（`SummarySlice` / `compile_context` 可选参数 / `ContextView`
新字段 / `context_payload` 写 `summaries`）、`event_schema.py` + `schemas/c3/event.json`
（`model.intent.context.summaries` 可选数组）、`replay.py`（读行、校验、重算）。

| 判据 | 负控 |
|---|---|
| `summaries=()` 时输出与旧实现逐字节相同（含 `messages_hash`） | ① 装配里无条件改消息 → 红 |
| 切片只在"覆盖的往返全被牺牲"时激活；`dropped_seqs` 不含被覆盖 seq | ② 激活条件放宽成"部分牺牲" → 红 |
| 摘要消息在第一条被牺牲往返的位置（顺序稳定） | ③ 固定插到最前 → 红 |
| `text_digest` 不符 / seq 重叠 / 未知 seq ⇒ `ContextCompileError` | ④ 跳过校验 → 红 |
| 摘要放不下 ⇒ 放弃摘要、回落确定性（不抛 `ContextOverflow`） | ⑤ 直接抛错 → 红 |
| payload 只写实际用上的摘要；缺省不写键（旧夹具不变） | ⑥ 写"传入的全部" → 红 |
| 回放：正文被改 / 行被删 / 版本不符 ⇒ problem；旧日志路径不变 | ⑦ 缺行时"跳过校验" → 红 |
| `event_schema` 与 `schemas/c3/event.json` 两处一致；坏形状写入前拒绝 | ⑧ 只改 py 不改 json → 红（既有镜像测试） |

### H3 · 生成路径 + 长任务验收

改什么：`loop.py`（`Limits.summary_mode/summary_max_calls`、`SummaryStore` 接线、
`_maybe_summarize`、`RunResult.summary_calls`）、`build_stack`/`run()`/CLI 装配、
验收夹具测试。

| 判据 | 负控 |
|---|---|
| 默认 `off` ⇒ 摘要表 0 行、`summary_calls=0`、模型调用次数不变 | ① 默认改成 on → 红 |
| 开启且首次出现丢弃 ⇒ 恰好 1 次调用；同材料再来 ⇒ 复用（0 次） | ② 每次都调用（不复用） → 红 |
| 账本拒绝 ⇒ 不调用、写 failed 行、回落确定性、Run 继续 | ③ 跳过预占直接调用 → 红 |
| 先 `begin(pending)` 再调用（顺序）：崩溃夹具在调用后、`complete` 前退出 ⇒ 重启不重试 | ④ 先调用后建行 → 红 |
| 崩溃遗留 `pending` ⇒ 标 failed、不再调用 | ⑤ 重试 pending → 红 |
| 摘要调用计入 `model_attempts` / `tokens` / `summary_calls` | ⑥ 不计数 → 红 |
| upper 模式拿不到输出上界 ⇒ 不摘要（不抛错、主调用照旧） | ⑦ 抛 `BudgetConfigError` 弄死 Run → 红 |
| 换模型再 replay ⇒ 结果不变；追加 v2 后旧 run 仍引用 v1 | ⑧ 回放改用"当前最新版本" → 红 |
| 长任务：关键约束被摘要遗漏 ⇒ 探测器报缺（失败） | ⑨ 探测器恒真 → 红 |
| 断网回放：`replay` 不构造模型、不触网 | ⑩ replay 里读最新模型配置 → 红 |

## 4 与既有口径的冲突（必须写进 PR body）

1. **`context.py` 模块注释的措辞**：原文写「压缩是确定性摘要而不是让模型总结：模型
   总结不可重放」。这句话的**理由仍然成立**，但结论要改口径：默认路径仍然不引入
   模型摘要；**开启时**摘要作为**已持久化的引用**进入，可重放性来自"读当时保存的
   那一份"，不来自"禁止模型"。这条改动会在 PR body 里给旧/新口径对照。
2. **`model.intent.payload.context` 增加可选键 `summaries`**：封闭 schema 里加
   可选字段（与既有 `memories` 同形），缺省不写 ⇒ 旧日志、旧夹具、老 digest 不变。
3. **`RunResult` 增加 `summary_calls`**：默认 0；序列化/展示层按可选字段处理。
4. **不新增事件类型**：`EVENT_TYPES` 仍 41 个。审计面 = 摘要表行 + 账本行 +
   `model.intent` 的引用 + run 计数；四处各有单一职责，不重复存正文。

## 5 诚实边界（本轮写下来，不许以后悄悄改成"已完成"）

- **跨 Run 不复用**：材料身份含事件 `seq`，不同 Run 的 seq 不重叠 ⇒ 同内容也是新
  材料。跨 Run 复用需要内容寻址的材料身份，未做。
- **不做增量/分层摘要**：丢弃集合长大就是新材料（受 `summary_max_calls` 限），
  不会"在旧摘要上续写"。
- **不做自动刷新**：换模型后旧 Run 永远读旧版本（这是要求，不是缺陷）；显式追加
  新版本的 API 有，CLI 未接。
- **不做摘要质量评测**：只保证"有来源、有界、可回放、遗漏可发现"；"摘要比确定性
  裁剪更好"没有评测就**不许宣称**。长任务夹具只证明该夹具上的关键约束不丢。
- **回放依赖同一数据根**：纯事件导出不足以回放摘要轮次（显式 problem）。备份含
  这三张表（同一 SQLite 文件），恢复后回放可用。
- **提示注入没有解决**：摘要正文是派生材料，包装声明是提示级缓解；执行侧闸门才是
  强制执行点。
- **费用与延迟数字**只来自本机夹具，不当 SLO，不编 provider 账单。
- **Windows 仍不是 required check**，本轮若 Windows job 红，照实写，不当绿。

## 6 交付状态（合入后回填）

| PR | 内容 | 判据 | 负控 | 状态 |
|---|---|---|---|---|
| #103 | H1 记录层 + 材料身份 | `tests/test_summaries.py` 22 条 | **7/7 红** | 已合入 |
| #103 | H2 编译注入 + 事件契约 + 回放 | `test_summary_context.py` 17 + `test_summary_replay.py` 7 | **7/7 红** | 已合入 |
| （本 PR） | H3 生成路径 + 长任务夹具 | `tests/test_summary_loop.py` 14 | **10/10 红** | 待合入 |
