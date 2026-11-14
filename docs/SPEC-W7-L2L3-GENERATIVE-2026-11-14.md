# SPEC · W7 记忆 L2/L3 生成式合成 + 冲突消解

> 日期：2026-11-14 · 分支：`feat/memory-l2l3-synthesis` · 基线：`main@88ec0ab`
> 路线图：[ROADMAP-COMPLETE-AGENT-2026-11-14](ROADMAP-COMPLETE-AGENT-2026-11-14.md) W7
> （账本随 PR #151 交付，#151 合并前该链接暂悬空）· 缺口出处：[KNOWLEDGE §7](KNOWLEDGE.md) F10 L2/L3（N3）行
> 与 [F10-memory-design.md](F10-memory-design.md) §13 #19 残留。
>
> **范围判定（先说结论）**：
>
> 1. **生成式合成：本 PR 不实现**。N3 教训已把话写死——「要上 LLM 必须另立契约：正文进
>    `tool.result`、replay 用快照、与确定性路径分两套代码与两个测试文件」（KNOWLEDGE §4
>    F10 N3 决策）；账本 W7 行的「契约影响」列也明写「另立契约（N3 教训）」。外发与接线两
>    个硬约束又都落在被在途 PR 占用的文件上（详见 §2.1）。按任务红线，本 PR 交付 **SPEC +
>    契约增量分析**（§6），不夹带实现，也不夹带契约变更。
> 2. **冲突消解：本 PR 实现**。确定性、可审计的裁决规则（写死，不交给模型裁量），落在
>    `memory_distill.py` 的合成层——这是不需要碰任何禁改文件就能闭环的裁决面（§2.2）。
>
> 冲突面自查（2026-11-14 实测）：在途 PR #140/#141/#146/#150/#151/#152/#154 的改动文件清单
> 与本 PR 改动文件（`src/tianshu/memory_distill.py`、`tests/test_memory_conflict.py`（新）、
> 本 SPEC（新）、`docs/KNOWLEDGE.md`（仅追加自己的条目）、`docs/F10-memory-design.md`
> （仅 §5 三处「代码未落地」假话改正，见 §7））**零交集**。

## 1. 现状（代码事实，2026-11-14 于 `main@88ec0ab`）

- **确定性合成已落地（N3，PR #98）**：`src/tianshu/memory_distill.py`（352 行）——
  L2 = 同作用域 active L1 按 token **重叠系数 0.34**（`|A∩B| / min(|A|,|B|)`）聚簇 →
  引用式拼装（每行 `- <原文一行> [<memory_id>@<revision>]`，块间空行）；L3 = user 作用域
  `kind=preference` 且 `origin ∈ {user_explicit, user_confirmed}` 的 active L1 按 4 KiB
  预算整行打包。常量写死并进测试（`L2_SIMILARITY` / `L2_MIN_GROUP` / `L3_PAGE_BYTES` /
  `QUOTE_LIMIT_BYTES` / `BASIS_SEQ_PREFIX`）。两次合成**逐字节相同**；页 id =
  `mem_<sha256(scope_uri \0 layer \0 content)[:16]>` ⇒ 重复合成幂等。
- **投影/执行链路**：`memory_exec.py` `_consolidate`（两阶段：先预检全部页再写正文，防孤儿
  blob）→ `page.to_output()` 加身份/引用键 → `tool.result` output → `memory.py`
  `_apply_consolidate_page` **只认事件 output、不重算算法**（replay 逐位复现）；校验 layer
  封闭集、`layer != l1`、`proof_count == len(evidence)`、`source_refs` 非空。**output 的键集
  没有字段级 schema 冻结**（F10 §14 / KNOWLEDGE §7「每工具 output 子契约未做」），但事件
  payload 形状按任务红线仍视为契约面——本轮**不增删 output 键**。
- **freshness 已落地**：`source_refs` 带 `seq:<basis_seq>`，作用域内出现更大
  `MAX(last_seq) WHERE layer='l1' AND status='active' AND erased_at IS NULL AND 未过期`
  ⇒ 页 `stale=true` 三档降权（fresh → unknown → stale），三条检索路径共用排序键。
- **测试现状**：`tests/test_memory_distill.py` 22 条（判据 + 负控 10 条真跑红的历史见
  F10 §13 #19）。**本 PR 不修改该文件**——现有判据零改动保持全绿，本身就是「无冲突输入的
  页面字节不变」的回归守卫。
- **缺口（KNOWLEDGE §7 N3 行 + F10 §13 #19 残留原文）**：① LLM 合成未做；② **冲突消解
  未做——两条矛盾的 L1 在页里并列，不裁决**；③ L3 仅 user 作用域；④ 没有 L2/L3 vs L1 的
  检索质量评测，谁都不能宣称「读页比直接读 L1 更好用」。
- **W6 外发 gate 语义先例（PR #154，在途）**：`MemoryProjection.retrieve(...,
  allow_embedding_outbound=False)` **默认 False**——模型工具的检索永不触发端点请求（零网络
  调用被测试钉死），自动降级 FTS-only 并如实标注；网络调用只允许发生在**人显式发起的控制面
  动作**（rebuild / CLI search / 面板 RPC）；provider 用 `sends_data_off_device` 自报能力、
  **缺省按会外发处理**。这是本轮生成式合成契约分析（§6）的对齐基准。
- **模型调用的产品接线点**：`loop.py` 的 `build_stack(...)`（provider 装配）与
  `_maybe_summarize` / `_generate_summary`（§7.1 语义摘要：`begin(pending)` 先提交 → 账本
  预占 → 模型调用 → `complete(ready)`；崩溃见 pending 只标 failed **不重试**）。
  **`memory_exec.MemoryExecutor` 不持有任何模型 adapter**——执行器路径今天没有产生模型
  调用的通道。

## 2. 范围判定（为什么一半停、一半做）

### 2.1 生成式合成为什么停在契约分析（§6）

三个相互独立的硬阻塞，任意一个都足以停：

1. **契约阻塞（N3 教训，权威）**：生成式合成的「两次合成是否一致」不可机械核对，会破坏
   确定性合成赖以成立的「可重建、可回放、与证据逐条对账」立论。所以 N3 落地时就把结论
   写进 KNOWLEDGE §4：**LLM 合成必须另立契约**——正文进 `tool.result`（replay 用快照，
   不重算）、与确定性路径**分两套代码与两个测试文件**。触发面无论走
   「`memory.consolidate` args 加 `mode`」还是新工具，都是 C2 schema 变化——红线 8
   （工具 schema 变化即新工具）⇒ 必须 `docs(contract)` 独立 PR 先行。账本 W7 行的
   「契约影响：另立契约（N3 教训）」与本判定一致。
2. **外发阻塞（数据出设备）**：生成式合成把**记忆正文**送外部模型 API = 数据外发。按 W6
   刚建立的 gate 语义，模型触发的路径必须默认拒绝；「允许」只能发生在人显式发起的控制面
   动作。gate 的判定点在 `memory.py`（PR #154 在途占用，**禁改**）。
3. **接线阻塞（产品路径）**：模型调用在产品里的产生路径在 `loop.py`（`build_stack` /
   `_generate_summary`，PR #154 在途占用，**禁改**），且模型调用必须接预算账本
   （预占 → 结算，计费属高危面，须独立复审）。`memory_exec` 执行器今天没有 model
   adapter 访问通道；任何接线都跨禁改文件。

⇒ 按任务红线：「若实现必须动 C1–C5 契约 / schemas/ / 事件 payload / 新增事件族 → 不要在
实现 PR 里夹带契约变更，停下，把 SPEC + 契约增量分析写成文档交付」。§6 就是这份分析——
它是**输入**，不是决定；拍板发生在 `docs(contract)` PR。

### 2.2 冲突消解为什么落在合成层、且本轮可做

- **缺口原文**（KNOWLEDGE §7 N3 行）：「**冲突消解**（两条矛盾的 L1 在页里并列、不裁决）
  都没做」。即：页把互斥陈述平铺给读者，没有任何「以谁为准」的结论。
- **落点约束**：能落裁决的地方只有三处——① L1 行本身（supersede/退役）：需要审批，模型
  触发的免审批合成不能改 L1（N3 已按此收口「active 页不退役」，同一逻辑更强地适用于
  L1）；② 召回排序层：在 `memory.py`（禁改 + 在途 PR 占用）；③ **合成层（页内容）**：
  `memory_distill.py`，冷区、纯函数、可逐字节复现。本轮落 ③。
- **「不许交给模型裁量」**：检测判据与裁决规则全部写死为常量 + 纯函数（§3），同一批输入
  逐字节得到同一份裁决；没有任何模型调用参与。
- **诚实边界（先承认再设计）**：确定性文本度量**分不开**「矛盾陈述」与「近重复改写/
  补充」。所以本实现只产出**「冲突候选 + 按规则裁决以谁为准」**的标注，**不断言**两条
  记忆逻辑上矛盾，**不删行、不改 L1、不改召回集合**——误报的代价被压到「页上多一行
  可核查的标注」，而不是「一条仍有效的记忆从页里消失」。这是把「并列不裁决」推进到
  「并列 + 已裁决（标注）」的最小诚实步子；更强裁决（淘汰败者、自动 edit）必须走审批
  契约，留 §6。

## 3. 冲突消解设计（本 PR 实现）

### 3.1 检测判据（写死，全部可机械复现）

同一页（L2）的**被采用成员**里，两条记忆 `a`、`b`（按 `memory_id` 定序，`a.memory_id <
b.memory_id`）构成**冲突候选**，当且仅当**全部**成立：

1. `a.kind == b.kind`——事实和偏好同框不是冲突（"部署在周五" 与 "喜欢周五部署" 可并存）；
2. `_one_line(a.content) != _one_line(b.content)`——压成一行后仍不同（逐字节相同的重复
   是 dedup 的事，不是消解的事）；
3. **互不为子集**：`¬(T_a ⊆ T_b) ∧ ¬(T_b ⊆ T_a)`，`T` 为既有 `_tokens()` 的稳定 token
   集合——子集关系是**信息增量**（"报告用中文写" ⊂ "报告用中文写并且先列结论"），把
   补充标注成冲突是误报；这一条是检测判据里干活最多的一条；
4. **token 规模相等**：`|T_a| == |T_b|`——替换型差异的形状特征：等量词汇、共享骨架、
   互斥值（"报告用中文写"/"报告用英文写" 各 11 个 token）。互补的流程事实
   （"数据库迁移分三步：备份、演练、割接" 24 个 token vs "数据库迁移前先做备份演练"
   23 个）规模不等，即使重叠 0.70 也不标注——它们共享步骤词汇但各自成立，标成冲突
   是误报；改写/扩写规模必变，同样排除；
5. **重叠系数 ≥ `CONFLICT_SIMILARITY`（写死 0.6）**：同主题的不同陈述（"报告用中文写"
   vs "报告用英文写"：共享 "报告/用/写" 骨架、各持互斥值，系数 ≈ 0.73）——阈值**高于**
   聚类阈值 0.34：进簇只要求"同场景"，标注冲突要求"几乎是同一句话换了值"。

检测跑在**整页被采用成员**上（不限于同簇）：聚类的贪心实现只对簇代表比较相似度，两条
高重叠成员可能落进同页的不同簇（B~C 0.8 但 C 与簇代表 A 只有 0.2）——"同页并列"才是
缺口的原文，按页检测不留这个盲区。

### 3.2 裁决规则（写死，优先级从高到低，无模型参与）

对每个冲突候选对 `(a, b)`，产出 `(winner, rule)`：

| 优先级 | 规则名 | 判定 | 依据 |
|---|---|---|---|
| 1 | `origin` | `_ORIGIN_RANK` 高者胜：`user_explicit > user_confirmed > inferred` | 与召回排序的 `_ORIGIN_WEIGHTS`（1.0 / 0.6 / 0.3，`memory.py`）同一口径：人给的比人确认的可信、人确认的比模型猜的可信。**这条先于时间**：人早就说过的结论，不因模型后来猜了新的而被推翻 |
| 2 | `seq` | `last_seq` 大者胜 | 逻辑时钟（与 freshness 同基准，不用钟表时间）：同 origin 时，后写入的推翻先写入的 |
| 3 | `memory_id` | 字典序小者胜 | 全序 tie-break。同 origin 且同 `last_seq` 在单事件逐条写入的投影里几乎不可达，但规则必须全序，否则两次合成可能差一个字 |

**作用域优先级在本轮不可用**（如实写）：`consolidatable_sources(scope)` 按单一作用域取
输入，一页之内没有第二个作用域可比较；跨作用域的冲突（user 记忆 vs task 记忆）属于
跨作用域合成，N3 起就明确不做。

### 3.3 页内呈现（确定性文本，不改既有版式）

- **成员行序不变**：簇内仍按 `memory_id` 排序、块间空行——既有版式一字节不动；
- 页尾追加**裁决标注节**（与正文块空一行分隔），每个冲突候选一行：

  ```
  [冲突裁决] <winner_id>@<winner_rev> 与 <loser_id>@<loser_rev> 按 <rule> 规则以
  <winner_id>@<winner_rev> 为准（两条 L1 均仍有效）
  ```

  多对时按 `(winner_id, loser_id)` 字典序排；标注行**不以 `- ` 开头**——既有判据
  「每条 `- ` 行带 `[memory_id@revision]`」的作用域是引用拼装行，裁决行不是引用行，
  不冒充证据；
- **上限**：`CONFLICT_ANNOTATION_LIMIT = 8`（写死并进测试）。超过则截断到 8 行并追加
  `[冲突裁决] 其余 N 对省略（标注上限 8）`——防病态簇（N 条近重复事实产生 N(N-1)/2 对）
  把页正文撑爆，且**截断必须如实报数**，不许静默吞；
- **不变量全部保持**：逐字节确定性；`proof_count == len(evidence)`；evidence 每项形状
  `{memory_id, revision, quote}` 不变（两条 L1 都还在页里，引文都还是证据）；
  `source_refs` / `basis_seq` 不变；`to_output()` 键集不变（**事件 payload 契约面零
  改动**）；页 id 仍由 `(scope, layer, content)` 派生 ⇒ 幂等成立。
- **行为演化（如实声明）**：存在冲突候选的页，正文多出标注节 ⇒ 页 id 变化 ⇒ 下一次
  `consolidate` 会把旧的 candidate 页按既有机制 superseded（行保留、旧正文与当时的
  evidence 仍可 `get`）；无冲突候选的页**逐字节不变**（现有 22 条判据零改动全绿即证）。
  active 页不退役的既有边界不受影响。

### 3.4 消解不碰什么（与红线对齐）

- **不改 L1**：两条源记忆仍 `active`、仍可单独检索、仍是下一轮合成的输入——淘汰式裁决
  （覆盖/删除语义）需要审批（红线 6），模型可见且免审批的 `memory.consolidate` 不能
  顺手做；
- **不改召回集合与排序**：`search()` / `retrieve()` / `_order_by_priority` 一行不动
  （`memory.py` 禁改，且排序语义属既有判据面）；
- **不新增事件类型 / payload 键 / schema**：标注全部在页正文（内容寻址 blob）里，
  事件面形状与今天逐字节同构。

### 3.5 L3 为什么本轮不做冲突裁决（如实留账）

L3 成员全部是**人给的偏好**（`origin ∈ {user_explicit, user_confirmed}`）。机器按 seq
裁决「人后说的偏好作废人先说的」，语义上越权——偏好冲突该由人自己 edit/forget 收口；
且 L3 的 4 KiB 预算与标注行有计费交互（标注吃预算、截断语义要在预算循环里重定义）。
留给契约 PR 与实现轮（§6 / §9）。

## 4. 判据表（冲突消解，逐条对上真实测试）

> 测试文件：`tests/test_memory_conflict.py`（**新**）。`tests/test_memory_distill.py`
> 现有 22 条**零改动**——无冲突输入的页面逐字节不变由它们继续钉住。
> 实测数字见 §8（以实跑回填为准，不预估）。

| # | 判据 | 测试 |
|---|---|---|
| J1 | 常量写死并进测试：`CONFLICT_SIMILARITY == 0.6`、`CONFLICT_ANNOTATION_LIMIT == 8` | `test_conflict_constants_are_pinned` |
| J2 | 互斥改写对（"报告用中文写" vs "报告用英文写"，同 kind、origin 同级）被标注为冲突候选，裁决规则 `seq`、胜者是 `last_seq` 大者；标注行逐字符断言 | `test_mutually_exclusive_restatement_is_annotated_with_seq_rule` |
| J3 | **origin 规则先于时间**：`user_confirmed` 的旧记忆（seq 小）压过 `inferred` 的新记忆（seq 大），规则名 `origin`——证明裁决不是"谁新谁赢" | `test_origin_rule_beats_recency` |
| J4 | 子集/补充关系**不**标注（"报告用中文写" vs "报告用中文写并且先列结论"）：页里没有 `[冲突裁决]` | `test_subset_extension_is_not_a_conflict` |
| J4b | 同 token 集合的**语序重排**不标注（"deploy on friday" vs "on friday deploy"：集合相等、规模相等、重叠 1.0）——子集排除判据的存在理由，没有它等规模 + 高重叠会误标同义重排 | `test_word_order_reordering_is_not_a_conflict` |
| J5 | 重叠系数在 `[0.34, 0.6)` 的同簇等规模近邻（"部署窗口是周一清晨六点" vs "部署窗口是周二深夜十点"，≈0.57）**不**标注；既有八夹具（含 m4/m5 互补流程事实）整页无标注 | `test_weakly_overlapping_cluster_mates_are_not_flagged` |
| J6 | 跨 kind 不标注（同正文对、kind 不同→无 `[冲突裁决]`） | `test_cross_kind_pairs_are_not_flagged` |
| J7 | 带冲突的页仍逐字节确定：同输入两次合成 content 逐字节相同、`to_output()` 相等 | `test_conflict_page_is_still_byte_deterministic` |
| J8 | 标注上限：10 条互为候选的事实产生 45 对 → 恰好 8 行标注 + 1 行如实截断（报 37 对省略） | `test_annotation_limit_truncates_and_reports_the_omitted_count` |
| J9 | 引用式拼装版式不被破坏：冲突页的 `- ` 行全部带 `[id@rev]` 且以 `]` 收尾；`proof_count == len(evidence)`；evidence 项形状恰为三键 | `test_conflict_page_keeps_reference_assembly_invariants` |
| J10 | **消解不改 L1**（投影 e2e）：播种互斥对 → 真 `consolidate` → 按事件形状应用 → 两条 L1 仍 `active`、页行存在且正文含裁决、`rebuild()` 后 `projection_hash` 与正文逐位复现 | `test_resolution_leaves_both_l1_rows_active_and_replays` |
| J11 | 冲突候选随内容变化出现/消失时，只有相关页受影响（增量 delta 在冲突场景仍成立） | `test_delta_still_holds_when_a_conflict_appears` |
| J12 | `_resolve_conflict_pair` 的三级规则单测：origin 分歧 / 同级比 seq / 同 seq 比 id，全序可复现 | `test_resolution_rules_are_total_and_pinned` |

## 5. 负控清单（变异 → 实跑红 → 还原 → 复跑绿）

> 纪律：每条负控**先确认拆的是最后一层保护**（KNOWLEDGE §5「冗余保护要证明不是声明」）；
> 红的测试名与失败形状如实回填本节，不许编造。

| # | 变异点（拆什么） | 预期红的判据 | 实测结果 |
|---|---|---|---|
| NC1 | 删掉检测判据 #3 的**子集排除**（`ta <= tb or tb <= ta` 分支移除）→ 同词重排被误标为冲突（J4 的子集对由等规模判据兜住，仍绿——子集规则独有的覆盖面就是同集合重排，见 J4b） | J4b | **红**：`test_word_order_reordering_is_not_a_conflict`（1 failed / 12 passed）；还原后 13 passed |
| NC2 | 裁决规则**换序**（先比 `seq` 再比 `origin`）→ "人确认的旧结论"被"模型的新猜测"压过 | J3 | **红**：`test_origin_rule_beats_recency` + `test_resolution_rules_are_total_and_pinned`（2 failed / 11 passed）；还原后 13 passed |
| NC3 | 标注上限改 `10**9`（截断永不触发）→ J8 的截断行消失、页被 45 行标注撑爆 | J8 | **红**：`test_annotation_limit_truncates_and_reports_the_omitted_count`（1 failed / 12 passed）；还原后 13 passed |
| NC4 | `CONFLICT_SIMILARITY` 判定改 `0.0` → 弱重叠近邻（0.571）与八夹具弱重叠对全被误标（跨 kind 对不受影响——kind 判据独立于阈值） | J5 | **红**：`test_weakly_overlapping_cluster_mates_are_not_flagged` + `test_delta_still_holds_when_a_conflict_appears`（其 before 夹具含 0.467 的弱重叠对，同样不该被标）（2 failed / 11 passed）；还原后 13 passed |

> 四条负控均已实跑：每轮清 `__pycache__`（同秒改写文件时 mtime 粒度会让 pytest 复用旧
> 字节码），变异 → 红 → 还原 → 复跑绿；还原后 `git diff` 确认实现文件只剩本功能的
> 113 行新增。

## 6. 生成式合成 · 契约增量分析（交付给 `docs(contract)` PR 的输入，非决定）

> 本节是分析，不是实现，也不在本 PR 落任何契约改动。逐项列「原方案 → 需要什么 →
> 为什么 → 影响哪些代码」，供契约 PR 拍板。

### 6.1 语义与触发面

- **方案 A（改既有工具）**：`memory.consolidate` args 增 `mode: "deterministic" |
  "generative"`（缺省 deterministic）。
  影响：C2 schema 变化 ⇒ 按红线 8 视为**新工具**，版本必须升级（`1.0` → `2.0`），三处同改
  （`tools.SPECS` / `schemas/` / PRD §5.2.2 表）；派发层要按**调用者身份** gate——模型路径
  恒拒 `generative`（W6 语义：模型触发的外发默认拒绝）。缺点：模型可见面变大，gate 依赖
  身份判定多一层，"模型点名 generative 被拒"要多一条负控。
- **方案 B（新控制面专属工具，倾向）**：新增 `memory.synthesize`（`1.0`、`ACTION_CREATE`、
  **`exposed=False`**）。模型工具面**零接触**，外发面天然最小（对齐 `memory.remember` /
  `memory.promote` / `memory.erase` 的先例：控制面专属不进 `tool_manifest()`，两道边界——
  manifest 不投影 + `tool.not_dispatched`）。人显式发起（CLI / 面板 RPC）才触发；外发
  审批走既有 `c4.outbound` 同级语义（恒需人工批准，公开与否不是本地能判定的事）。
  影响：新工具 + `tool_manifest()` 计数 + 绊线清单（`test_veto_scope`）显式过一次；
  `memory_control.py` / `cli.py` 各加一个入口。
- 两个方案都要在 PRD §5.2.2 表与 C2/C4 schema 落字段级形状——属 `docs(contract)` PR。

### 6.2 外发 gate（对齐 W6 先例，逐条）

1. provider 协议 `SynthesisProvider` 对齐 `EmbeddingProvider` 模式：`synthesize(sources,
   budget) -> GeneratedPage` + **`sends_data_off_device` 自报，缺省按会外发处理**（安全
   默认）；本机测试 provider 显式报 False。
2. **模型触发路径零外发**：即便将来有人把生成式合成挂到模型可见面（方案 A），gate 判定
   必须使模型路径的调用**零网络请求**并如实降级/拒绝——与
   `retrieve(allow_embedding_outbound=False)` 同构，测试钉死请求计数为 0。
3. **人显式动作 + 一次性审批**：外发的是**记忆正文**（私有内容出设备），按 G1d 口径
   恒需人工批准（`c4.outbound` 同级），不能走"免审批 create"——`memory.consolidate`
   免审批的理由是"只写可重建投影、零外发"，生成式路径不满足这个前提，**不得继承免审批**。
4. provider 未配置 / 半套配置 → 如实报 `unsupported`，不回落确定性合成假装成功。

### 6.3 事件面与回放

- 生成正文必须进 `tool.result` output（replay 用快照，不重算）——投影侧
  `_apply_consolidate_page` 今天已经"只认事件 output"，形状上兼容；但**证据语义不同**：
  确定性页的 `evidence.quote` 是逐字节引文（可机械核对），生成式页的 quote 是模型改写，
  逐字核对不成立。契约增量：生成式页必须显式带 `synthesis: "generative"` 标记，其
  evidence 校验降级为**引用存在性**（`memory_id` 必须在投影中存在且当时 active；引文不
  保证逐字）——"没有证据的 L2 不成立"这条底线不变，只是"证据"的含义分两档，必须可区分、
  不可混淆。
- 页 id 派生不变（`scope + layer + content`）；生成页与确定性页撞正文时的行为要在契约里
  写死（倾向：fail-closed，同 §3.3 的页 id 冲突语义）。

### 6.4 计费与崩溃（对齐 §7.1 摘要轮先例）

- 预算：`begin(pending)` 先提交 → 账本预占 → 模型调用 → `complete(ready)`；`upper` 模式
  拿不到输出上界 ⇒ **拒绝进入生成式合成**（如实报错，不用默认预留冒充上界）；失败一律
  回落确定性合成，不弄死 Run。
- 崩溃：见 pending 只标 failed **不重试**（"不偷偷增加一次模型调用"）；注入点与判据抄
  摘要轮的 `summary-after-call` 模式。

### 6.5 接线点与冲突面

- provider 装配：`loop.build_stack(synthesis_provider=...)`（对齐 `embedding_provider=`）；
  环境变量四件套模式（key 只从环境读，永不落盘）。
- 触发执行：控制面动作经 `memory_control.py` → dispatcher → `memory_exec`；**执行器需要
  拿到 provider**——这是 `memory_exec.py` 的形状变化，必须在契约 PR 里写清依赖注入路径。
- **在途冲突**：`memory.py` / `loop.py` / `memory_control.py` / `cli.py` 均被 PR #154
  占用；契约 PR 与实现 PR 都要等 #154 落地后 rebase 再动。

### 6.6 判据 / 负控草案（供契约 PR 与实现 PR 引用）

判据：① 模型路径零网络请求（计数钉死为 0）；② 控制面动作无外发审批不发包、拒绝后
零字节出设备；③ `sends_data_off_device` 缺省按外发处理，本机 provider 不弹审批；
④ 生成页 evidence 引用不存在 ⇒ 页被拒、一个字节不进投影；⑤ 生成页显式 `generative`
标记，与确定性页不可混淆；⑥ `upper` 无输出上界 ⇒ 拒绝；预占 → 调用 → 结算链路完整；
⑦ 崩溃 pending ⇒ failed 不重试；⑧ replay 按事件快照重建生成页，不重算。
负控：拆 gate → ①② 红；拆引用存在性校验 → ④ 红；拆 upper 拒绝 → ⑥ 红；拆 pending
标 failed → ⑦ 红。

## 7. 文档对账

- **`docs/PRD-v1.md` 第 170 行（漂移，不改——PRD 属 #141/#146 冲突面，留给契约 PR）**：
  「`layer` 仍是**契约、未落库**（N3）」与代码事实相反——N3（PR #98）已把 `layer` 落库
  （投影 schema v2→v3，`memories.layer TEXT NOT NULL DEFAULT 'l1'` 就地 `ALTER`，封闭集
  `MEMORY_LAYERS = {l1, l2, l3}` 见 `memory.py`，迁移测试
  `test_schema_v2_database_gains_the_layer_column_in_place`）。同一行里 `erased_at`
  的「已落库（N4）」是对的。**建议改法**：「`layer` 已落库（N3）」。F10 自己的 §13 #1
  也早已按「17 个字段已全部落地」记账——PRD 落后于 F10。
- **`docs/PRD-v1.md` 第 168 行（口径张力，留给契约 PR 一并核）**：把 `consolidated_at`
  称为「freshness 判据」；§5.2.2 第 463 行已按 N3 实际改口（「freshness 的基准不是
  `consolidated_at`……判据落在 `source_refs` 里的 `seq:<basis_seq>`」）。168 行建议补
  「freshness 实际基准见 §5.2.2（`seq:<basis_seq>`，N3）」。
- **`docs/PRD-v1.md` 第 463 / 494 行（正确，无需改）**：`memory.consolidate` 规格（args
  仅 `{scope, scope_id}`、确定性合成、candidate + 人 confirm、workspace 拒）与
  「确定性 L2/L3 合成与 freshness 已落地」的落地状态，与代码一致。**冲突消解不在 PRD
  的工具规格里**（它是合成算法内部行为，不改工具契约）——若将来做「淘汰式裁决/生成式
  合成」，才需要动 §5.2.2（见 §6）。
- **`docs/F10-memory-design.md`（本 PR 修三处假话，均为括注级）**：§5 两处
  「layer（v1.2 契约；**代码未落地**）」与一处「迁移规则（v1.2 契约，**代码未落地**）」
  ——三处都与该文 §13 #1 / #22 的「已落地」记账自相矛盾，且与代码相反（layer 落于
  N3 schema v3；origin 迁移规则落于 N1
  `test_human_approval_upgrades_origin_to_user_confirmed`）。改为「已落库（N3/N1）」。
- **`docs/KNOWLEDGE.md`**：§7 的 F10 L2/L3（N3）行更新冲突消解状态（确定性裁决已落地 +
  残留边界）；§8 追加本会话日志条目。其余条目不动。

## 8. 实测回填（2026-11-14 实跑，macOS arm64 本机）

- 新增判据：`tests/test_memory_conflict.py` **13 个 `def test_`**（J1–J12 + J4b，无参数化，
  13 条），实跑 **13 passed**。
- 既有判据回归：`tests/test_memory_distill.py` 22 条**零改动**，实跑 22 passed
  （两文件合计 35 passed / 0.15s）。记忆面全文件回归（store / dispatch / loop / rank /
  vectors / host / resources）**254 passed**。
- 负控：NC1–NC4 逐条变异 → 红（测试名与红绿比数见 §5 回填）→ 还原 → 绿（13 passed）。
- 门禁：见 §8 末尾「全量门禁」小节（收尾全量实跑后回填）。

### 全量门禁

- `uv lock --check`：exit 0（`Resolved 8 packages`，锁文件无漂移）。
- `uv run --frozen ruff check .`：`All checks passed!`（全仓）。
- `uv run --frozen pytest -o addopts="" -q`：**3212 passed / 8 skipped（543.58s，
  macOS arm64 本机，2026-11-14 实跑）**——基线 `main@88ec0ab` 为 3199 passed / 8 skipped
  （KNOWLEDGE §2 的 2026-11-14 条目实测数字），本 PR **+13** = `tests/test_memory_conflict.py`
  全部新条目；skip 数不变、无既有用例改动。CI 为最终口径，本机数字是收尾记录。

## 9. 边界与未决（不许写成完成）

1. **检测是"召回导向"的，不是完备的**：确定性度量抓得住"同主题换值"（部署日改了、
   汇报语言改了），抓不住低重叠的语义矛盾（"deploy on fridays" vs "deploys happen on
   friday evenings" 重叠 0.33，连簇都进不了）；也**分不开**"矛盾"与"近重复改写"——
   后者会被如实标为**候选**并按规则给出裁决，不声称已证明逻辑矛盾。任何"裁决 =
   正确消解了矛盾"的解读都不许写。
2. **标注不改变任何检索行为**：`search()` / `retrieve()` 的排序、召回集合、stale 三态
   一概不动；页内裁决的消费者是读页面的人与模型。召回层的冲突处置（如"互斥对只召回
   胜者"）需要动 `memory.py`（禁改 + 在途占用），未做。
3. **L3 冲突裁决未做**（§3.5 的理由）；跨作用域合成 / 对话抽取维持 N3 的"不做"。
4. **生成式合成未实现**（§6 是契约分析）；在其落地前，任何文档不得把「记忆合成」写成
   "能生成摘要"。
5. **无 L2/L3 vs L1 的检索质量评测**（N3 残留维持）：本轮同样不许宣称"带裁决的页比
   不带的更好用"。
6. **结构化冲突输出字段未做**：`to_output()` 不加 `conflicts` 键（事件 payload 契约面
   冻结）；裁决的结构化消费留给契约 PR（可与 §6 的 `synthesis` 标记一起定）。

## 10. 高危判定

- 本轮改动面：`memory_distill.py`（纯函数合成器）+ 新测试文件 + 文档。**不碰**授权 /
  租约 / 并发写 / 事件日志契约 / 计费；不新增事件类型、不改 `schemas/`、不改工具
  schema、不改任何派发/审批路径（合成页仍走既有 `memory.consolidate` 免审批 create
  写候选的语义，零变化）。
- 页正文会进 `tool.result` output 与内容寻址 blob——**形状不变、内容更长**，事件契约
  面不变；正文仍过既有 `redact_or_block` 闸门（标注行只含 memory_id 与规则名，无正文
  汇聚效应：标注引用的是 id，不是原文）。
- **判定：非高危改动**，不触发 CONTRIBUTING §7 的强制独立复审；仍走标准 PR + 四平台
  CI 门禁。若复审人认为「页内容算法口径变更」值得一读，§3 是完整依据。
