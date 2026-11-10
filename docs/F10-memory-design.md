# F10 · 本地记忆设计（v1.1）

> PRD §7 P1 / F10。承接 [NEXT-CAPABILITIES-PLAN.md](NEXT-CAPABILITIES-PLAN.md) §5（实施拆解：PR08 契约 / PR09 实现）。
> 本文只定**形状与契约**，不写实现代码。任何契约变更仍须走 `docs(contract):` 独立 PR。

## 1. 一句话

**记忆不是第二个事实源，是事件日志的另一个可重建投影。**

## 2. 问题

M0–M5 已完成：事件日志 append-only、可重放、可审计。但 **F10 未实现**——Agent 跨会话什么都不记得。PRD §2.1 承诺的「长任务」与「通用任务执行」缺了记忆，就退化成单会话工具。

四个参照项目——[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)、[OpenViking](https://github.com/volcengine/OpenViking)、[mem0](https://github.com/mem0ai/mem0)、[Hindsight](https://github.com/vectorize-io/hindsight)——前两家把记忆做成**独立服务 / 独立存储**，因此各自背了两套事实、两套一致性；后两家反而把「事实」与「索引 / 合成」明确分开（见 §3.1 外部印证）。它们真正值得学的是**组织方式**（分层、作用域、装备、可检查、带来源、证据化），不是「独立存储」这个形状。

天枢的红线 1（append-only 事件日志是**唯一**事实源）已经把独立存储这条路堵死了。**正确姿势是把记忆建成投影，而不是建第二个库。**

## 3. 核心设计决定

### 3.1 记忆是投影，不是事实源

- 原始会话 = 事件日志（L0，**已有**）。
- L1/L2/L3 由事件日志**确定性蒸馏**得到，可重放重建；删掉投影不丢事实。
- 记忆**激活与遗忘**是一等副作用：与覆盖 / 删除同级，走 intent/result + C4 审批 + Receipt（候选写入只是投影草稿，见 §7）。
- 事件日志里**只存引用与审计元数据**，不重复写记忆正文——不可删除的日志不该随记忆膨胀。

> **外部印证（2026-11-10）**：这个「事实与索引分离、合成层可重建」的形状不是天枢自创的。mem0 把存储拆成 `SQL 事实 = source of truth` / `向量 = embedding` / `实体 = 匹配加权`；Hindsight 的 reflect 检索阶梯是 `Mental Models → Observations → Raw facts（ground truth for verification）`。两家独立收敛到同一架构，且都明确「合成层可由事实层重建」。天枢的红线 1 已经站在这条线上。

### 3.2 记忆操作是 C2 工具，不是新机制

`memory.search` / `memory.get` / `memory.propose` / `memory.confirm` / `memory.forget` 走既有 `ToolRegistry` + `Dispatcher`：

- 写操作（propose / confirm / forget）都走 dispatcher，因此自动获得 intent/result 与完整审计；**但审批按 §7 分级**——propose 不挡，confirm / forget 挡。
- 读操作（search / get）自动获得作用域收窄。

**首版不新增事件族。** 理由：新增 `memory.*` 事件会与 `tool.intent` / `tool.result` 形成同一因果的第二份指针——PRD §5.3.3 已经为 `parent_seq` 立过这条规矩（两份指针不一致时没有裁决者）。记忆生命周期由工具调用 + 审批事件承载，投影可据此重建。

### 3.3 默认私有，共享是显式审批动作

新产生的记忆默认 `scope=user` 且仅 owner 可见；把它放进某个 workspace / task 作用域是**显式动作**，不是默认泄漏。这条与 fail-closed 哲学一致。

## 4. 分层模型

| 层 | 内容 | 来源 | 用途 |
|---|---|---|---|
| **L0 原始会话** | 完整事件日志 | **已有** | 核对原话 / 时间 / 来源；回放 |
| **L1 原子事实** | 单条可执行事实 | 蒸馏 | 精确召回 |
| **L2 场景 / 项目上下文** | **带证据的信念**：围绕项目或任务组织的知识块，每条指回源记忆 + 原文引文 + `proof_count` | 蒸馏（增量） | 快速重建工作上下文；可被证伪 |
| **L3 偏好 / 人格** | 长期用户偏好与稳定模式；**预算好的、可启动的页面** | 蒸馏 + 用户显式 | 启动即得，无需每次现算 |

写入分层，**检索也分层**：L2/L3 用于快速建上下文；需要具体事实时回落 L1/L0。这与 F5 的「字节预算」是同一思路——先给结论与结构，再按需取证据。

L2/L3 的蒸馏有两条硬约束（见 §9）：**只能增量应用新证据隐含的改动，禁止全量重生成**；且**必须保留上一版内容与它当时站的证据**。

> F10 原述的四类存储（事实→SQLite、文件→内容寻址、历史→事件日志、语义→FTS+向量）与本分层是**正交的两个维度**：分层回答「多粗」，存储回答「放哪」。`历史→事件日志` 就是 L0；`事实→SQLite` 承载 L1；`文件→内容寻址` 承载正文与 L2；`语义→FTS+向量` 是覆盖各层的**检索索引**，不是第二份事实。两者组合成一个可重建投影。

## 5. 记忆资产语义

细化 PRD §4.1 的 `Memory` 实体：

```text
Memory {
  memory_id, revision,
  scope: user | workspace | task,
  scope_id,
  kind: preference | fact | decision | procedure,
  content_ref,            # 正文走内容寻址；事件里只存引用
  source_refs[],          # 引用：事件 seq / ArtifactRef / memory_id
  evidence[],             # 支撑证据：源 memory_id + 原文引文（L2 必填）
  proof_count,            # 支撑证据条数（机械可算，L2 必填）
  origin: user_explicit | user_confirmed | inferred,
  status: candidate | active | superseded | forgotten,
  consolidated_at?,       # 上次蒸馏 / 合并时间（freshness 判据）
  created_at, expires_at?, supersedes?
}
```

- **kind**：`preference`（偏好）/ `fact`（事实）/ `decision`（决策）/ `procedure`（可执行流程，对应 Skill）。
- **origin**：`user_explicit`（用户明说「记住」）/ `user_confirmed`（用户确认过候选）/ `inferred`（模型推断）。**只有前两者自动激活**；`inferred` 永远是 `candidate`。
- **status**：`candidate → active → superseded | forgotten`。
- **source_refs**：**每条记忆必须能指回来源**。这是「可检查、可编辑、带来源」的锚点。
- **evidence / proof_count**：L2 每条信念必须指回支撑它的源记忆与原文引文；`proof_count` 是条数，机械可算、可审计。**没有证据的 L2 不成立**——这是「信念」与「断言」的分界。
- **consolidated_at**：用于 freshness awareness——作用域内已有新记忆尚未合并时，该层视为**陈旧**（见 §8）。

## 6. 作用域与装备（loadout）

- 作用域 `scope ∈ {user, workspace, task}`：`user` 全局、`workspace` 绑定目录、`task` 绑定任务。
- **一个 Run 只装备它有权访问的作用域**，由当前身份 + 任务绑定核验。模型不能读另一个 workspace 的私有记忆。
- **不同任务挂不同装备**：调研类任务挂调研记忆，编码类任务挂项目记忆。**不注入全库。**
- 装备 = 作用域过滤 + 状态 / 有效期过滤后的集合；检索在这个集合内做，而不是全局扫。

## 7. 记忆写入：投影更新 + 分级审批

记忆写入**不是**红线意义上的「副作用」（它不改用户文件系统），而是**投影更新**——按 AGENTS.md §5「状态是事件的投影」，同事务写事件即可重建。因此审批的判据不是「写了存储」，而是**「是否改变用户长期可见的行为」**：

| 操作 | 效果 | 审批 |
|---|---|---|
| `memory.search` / `memory.get` | 只读 | 不需要 |
| `memory.propose` | 写**候选**（惰性草稿：未激活不召回，可遗忘 / 编辑 / 重建） | 不需要 |
| `memory.confirm` | **激活**（进入长期召回） | 需要；`origin=user_explicit` 除外——用户本人刚说的话就是授权 |
| `memory.forget` | 写 tombstone | 需要（等同删除） |

- `propose` 只生成 `candidate`，**不自动激活**；候选是惰性草稿、可重建，所以不挡。
- `inferred` 来源的候选**永不自动激活**——摘要或网页内容不能变成长期用户事实（防污染与注入）。
- **激活**与**遗忘**才是一等副作用：走 C4 审批 + 一次性消费 + 执行侧二次核验 digest，与覆盖 / 删除同级。
- 用户明说「记住这件事」可直接确认（`origin=user_explicit`）。
- **任务状态、grant、审批决定不存为可覆盖的普通记忆**：它们是事件投影，不是可被记忆覆盖的事实。
- **写入前去重**：抽取后、进索引（FTS / 向量）**之前**做去重——冗余事实在入索引前剔除（mem0 的做法）。去重只影响投影，不改事件日志。
- **写入闸门（Memory Defense）**：任何记忆写入过一道 PII / 密钥扫描；命中即红act（`[REDACTED:...]`）或**在入库前阻断**。这是写入闸门，不是事后清理；与 F12 脱敏、D7 本地 only 同源。

## 8. 检索与上下文

顺序固定：**先过滤 → 再排序 → 后裁剪**。

1. **过滤**：scope、有效期、status（只 `active`）。
2. **排序**：优先 `user_explicit` > `user_confirmed` > `inferred`；优先当前任务 / 工作区证据。
3. **裁剪**：条数上限 + 字节上限 + 超时。

- 首版默认最多 **5 条 / 合计 4 KiB**（提案值），受总上下文预算限制。
- **不把全库放进 system prompt**。检索文本是带出处的上下文资料，由 F5 的 `compile_context`（纯函数）统一预算。
- 实际选中的 `memory_id` / `revision` + 文本快照引用要落进事件，供回放核对。
- **冲突事实并列呈现并保留来源**，不能由新抓取的网页静默覆盖用户偏好。
- **freshness awareness**：L2/L3 带 `consolidated_at`；若其作用域内已有新记忆尚未合并，该层视为**陈旧**——不盲信，回落到 L1/L0 原始事实核验后再用。
- 检索信号四路：**语义 / 关键词（BM25）/ 实体 / 时序**。**首版只做前两路**；实体与时序列为后续，须用跨实体 / 跨时间问题评测证明增益后再做（见 §12）。

## 9. 可检查、可编辑、带来源

- 用户能打开「Agent 记住了什么」，**直接编辑** L1–L3——不只是查看 / 删除。
- 理由：记忆的价值取决于准确性；给人纠正的办法，比指望蒸馏永远正确更现实。
- 编辑保留 `revision`，用 `supersede` 而非覆盖（旧版可查）。
- 每条记忆显示来源（事件 seq / 来源引用）。
- **增量 delta 刷新，禁止全量重生成**：L2/L3 的蒸馏只能**应用新证据隐含的改动**，其余物理不动。全量重生成会让模型悄悄改写未变的部分（bullet 变数字、大小写漂移、同义改写），长文档会缓慢变成「说法差不多的另一份文档」。这是硬约束，不是优化。
- **保留上一版 + 证据**：每次变化保留上一版内容与它当时站的证据，能回答「上个月它说了什么、依据是什么」。

## 10. 忘记 vs 物理擦除

- **忘记（本阶段交付）**：追加 tombstone，同事务从活动投影移除；失效 FTS / 向量 / 缓存；**新请求与重建索引都不能复活它**。
- **物理擦除（单开契约工单）**：需要追踪历史模型消息、来源事件、备份、共享 blob 引用与保留策略。**不能因为删了 FTS 行就声称历史内容已擦除。**
- 历史 replay 用当时的快照；若用户将来彻底清除导致快照缺失，应明确报「历史证据不可用」，**而不是重取最新记忆**。

## 11. 与现行契约 / 红线的关系

| 红线 / 契约 | 关系 |
|---|---|
| 红线 1（append-only，唯一事实源） | 记忆是投影；事件只存引用 |
| 红线 2（副作用先 intent 后 result + Receipt） | 记忆写入是投影更新（同事务写事件）；**激活 / 遗忘**作为一等副作用走 intent/result + Receipt |
| 红线 4（授权只在执行侧） | 记忆激活 / 遗忘的审批在 dispatcher 执行侧判定 |
| 红线 6（覆盖 / 删除需审批） | agent 发起的 `memory.confirm` 与 `memory.forget` 等同覆盖 / 删除 |
| 红线 8（工具 schema 变化即新工具） | 记忆工具版本化，`schema_hash` 绑定审批 |
| 红线 9（能力声明只用于路由） | 记忆检索能力不构成授权 |
| C2 Tool | 记忆操作是工具 |
| C3 Event | 首版**不新增**事件族（§3.2）；若新增须三处同改 |
| C4 Permission | 记忆作用域复用现有权限收窄 |
| F12 脱敏 / D7 本地 only | 记忆写入过 PII / 密钥闸门，命中红act 或阻断 |

## 12. 明确不做（不抄参照项目的部分）

- **不建独立记忆服务 / 独立事实库**（红线 1）。
- **不引入外部 embedding / VLM 作为运行时硬依赖**（零依赖约束）。向量召回是**可选投影**，缺失时降级 FTS-only 并如实标注，不静默假装有语义。
- **不做多租户 / 团队 / owner / ACL**（D2 单用户；作用域只有 user / workspace / task）。
- **不做云托管 / 自动上报**（D7 诊断本地 only）。
- **不照抄 AGPL 项目的代码**（OpenViking 为 AGPLv3；只取设计，不取实现）。
- **不把会话全文无差别抽成记忆**（首版只做显式记忆 + 有界蒸馏，避免记忆膨胀淹没上下文）。
- **不首版做实体图谱 / 时序检索**：实体抽取与实体作用域检索、时序信号都是有效方向（mem0 / Hindsight 都在用），但须用**跨实体 / 跨时间问题评测**证明增益后再做，不凭「听起来该有」就上。

## 13. 契约增量清单

文档先行，代码随后：

1. PRD §4.1 `Memory` 实体字段（本文 §5）。
2. PRD §7 F10 需求细化（本文 §4 / §6 / §8）。
3. C2：新增记忆工具定义（`memory.*`，版本化 + schema）。
4. C4：记忆作用域与审批规则编号（写入 / 忘记是否需审批）。
5. `schemas/`：首版预期**不新增**事件 schema；若新增事件族则补 `schemas/c3/event.json`。
6. KNOWLEDGE / README：文档索引与状态对账。

## 14. 出口验收（判据）

- 记住「报告用中文、先列结论」后，新任务能检索到。
- 不同 workspace 不串记忆；模型读不到另一个 workspace 的私有记忆。
- 中文无空格文本与中英混合标识符的词面召回用固定样例量化。
- 候选未经确认不激活；摘要 / 网页不能自动成为长期用户事实。
- 编辑保留 revision；忘记后新任务、重建索引、应用重启均不再召回。
- 删除整个检索索引可重建，正文与来源仍在。
- 离线 replay 不读当前记忆库最新版本（用当时快照）。
- 记忆**激活 / 遗忘**有 intent/result + Receipt；审批被拒则不写。
- L2 每条信念能指回证据与原文引文；`proof_count` 与实际证据条数一致（机械核对，不靠人眼）。
- 蒸馏只做增量：未变部分**逐字节不变**（用固定夹具比对蒸馏前后）。
- 陈旧层先核验再用：作用域内有新记忆未合并时，L2/L3 **不短路**检索，回落 L1/L0 核验。
- 记忆写入过 PII / 密钥闸门：含密钥的候选被红act 或阻断，**不入库**。
- 写入前去重：重复事实不重复进索引（进索引前条数已下降）。

## 15. 与实施计划的关系

[NEXT-CAPABILITIES-PLAN.md](NEXT-CAPABILITIES-PLAN.md) §5 是实施拆解（PR08 `docs(contract)` / PR09 `feat(memory)`）；本文是该 PR 的设计输入。两者冲突时**以本文为准（设计）**，但任何契约变更仍需走 `docs(contract):` 独立 PR。

## 16. 参考

- [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) — 分层蒸馏（L0–L3）、记忆资产化、装备（loadout）、默认私有、可编辑记忆、冷启动 save file。
- [OpenViking](https://github.com/volcengine/OpenViking) — 上下文即虚拟文件系统、三层读取（abstract/overview/detail）、目录作用域检索、会话成文件 + diff、记忆带引用。
- [OpenViking 设计自述](https://blog.openviking.ai/post/openviking-context-database/) — 五条件公式与「把复杂度从用户移到系统」。
- [mem0](https://github.com/mem0ai/mem0) — ADD-only 加性抽取（不静默改写）、写入前去重、实体抽取、语义+BM25+实体+时序四路检索、SQL 事实为源 + 向量/实体为投影。
- [Hindsight](https://github.com/vectorize-io/hindsight) — Observations（带证据与 proof count、refine 而非覆盖）、Mental Models（预算好的可启动页面、读取为 DB read）、增量 delta 刷新防漂移、freshness awareness、Decision Models、Memory Defense。
- 天枢既有地基：[F5-context-compile.md](F5-context-compile.md)（纯函数预算编译）、[M5-desktop.md](M5-desktop.md)（作用域与审批语义）。
