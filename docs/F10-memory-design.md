# F10 · 本地记忆设计（v1.1）

> PRD §7 P1 / F10。承接 [NEXT-CAPABILITIES-PLAN.md](NEXT-CAPABILITIES-PLAN.md) §5（实施拆解：PR08 契约 / PR09 实现）。
> 本文只定**形状与契约**，不写实现代码。任何契约变更仍须走 `docs(contract):` 独立 PR。
>
> **落地状态（2026-11-11）**：L1 显式记忆已落地，并在 N1（控制面标记 / `memory.remember` / `memory.edit` / `memory.promote` / `origin` 升级）、N2（作用域优先级 + 向量索引/RRF，**受能力边界约束**）、N4（物理擦除 `memory.erase` + 投影 schema v2）、N5（崩溃恢复的记忆分支 + 派发审计表终态脱敏）、**N3（确定性 L2/L3 蒸馏 + freshness，投影 schema v3，`memory.consolidate`）** 之后收口——逐条见 §13，判据↔测试见 §14。**L2/L3 只做了确定性合成，LLM 合成明确没做**（§13 #19 与 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) §4.4 的"偏差 / 没做到"）；§14 表里明确写"部分覆盖 / 未覆盖"的判据照旧按表读，别把"有页"当成"页比 L1 更好用"——那个评测不存在。

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
- **写入**路径只存引用与审计元数据：`Dispatcher._intent_args()` 在**带正文的记忆写**
  （`memory.propose` / `memory.remember` / `memory.edit`）进 `tool.intent` 前拿掉
  `content`，只留 `content_sha256` / `content_bytes` / `content_omitted`；判断口径是
  "请求资源是记忆资源 + 参数里有 content"，不是列工具名（N5 复审发现 N1 之后
  remember / edit 的正文原样进了 append-only 事件日志）。正文走内容寻址
  （`memory_blobs/`）。
- **读取**路径必然带正文：`memory.search` / `memory.get` 的 `ToolResult.output` 就是
  模型当时看到的那段文本，`loop._record_result()` 按事件溯源的原样写进 `tool.result`。
  **不留它，replay 就重建不出模型当时的输入，`messages_hash` 立刻对不上。**
- 所以准确的说法是：**`tool.intent` 不存正文；`tool.result`（读快照）与模型提议快照会存**
  （读事件每次最多 `MAX_RECALL_BYTES` = 4 KiB）。这是红线 1（日志自足、可离线回放）换来
  的代价，不是疏忽；早先"事件日志只存引用、不重复写正文"的说法**只对 `tool.intent`
  成立**，已按实测改正。
  **还得再补一句（N5 复审指出）**：模型给出的工具调用参数（例如模型自己提议的
  propose / edit 正文）会进 `model.result`，`run_turns` 也保留整轮 payload——replay 需要
  它们重建模型当时的输入，所以这部分正文**同样不是"没写"**。因此"日志只随读增长"这句
  话只在"**受控写入路径**（`tool.intent`）不膨胀"的意义上成立；跨会话的正文副本总量仍会
  随模型提议增长，物理擦除的范围见 §10。
- 由此 **`memory.forget` 不等于物理擦除**：它只改投影状态，不动事件日志，也**不删**
  正文 blob（§10）。正文在盘上的副本 = 事件日志里的读快照 + 正文 blob。物理擦除是
  §10 单开的契约工单，**不得**用"删了 FTS 行"或"删了投影"替代声明。

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
  layer: l1 | l2 | l3,    # 资产层（v1.2 契约；代码未落地）
  content_ref,            # 正文走内容寻址；事件里只存引用
  source_refs[],          # 引用：事件 seq / ArtifactRef / memory_id
  evidence[],             # 支撑证据：源 memory_id + 原文引文（L2 必填）
  proof_count,            # 支撑证据条数（机械可算，L2 必填）
  origin: user_explicit | user_confirmed | inferred,
  status: candidate | active | superseded | forgotten,
  consolidated_at?,       # 上次蒸馏 / 合并时间（freshness 判据）
  created_at, expires_at?, supersedes?,
  erased_at?              # 物理擦除时间（v1.2 契约；与 forgotten 分开）
}
```

- **kind**：`preference`（偏好）/ `fact`（事实）/ `decision`（决策）/ `procedure`（可执行流程，对应 Skill）。
- **origin**：`user_explicit`（用户明说「记住」）/ `user_confirmed`（用户确认过候选）/ `inferred`（模型推断）。**只有前两者自动激活**；`inferred` 永远是 `candidate`。
  **迁移规则（v1.2 契约，代码未落地）**：`memory.confirm` 被人批准后必须把 `inferred`
  **升级**为 `user_confirmed`（只升级、不降级）；出处是审批记录，不是模型参数。
  `user_explicit` 只能由控制面 `memory.remember` 创建产生。**origin 描述的是
  "这一版正文"的来源，不是记忆的终身属性**：控制面 `memory.edit`（人自己写的新正文）
  → `user_explicit`；模型写、人批准 → `user_confirmed`；`memory.promote` 搬同一段正文，
  来源**随行**（不降档）。旧版 origin 留在旧行。
- **status**：`candidate → active → superseded | forgotten`。
- **layer**（v1.2 契约，代码未落地）：资产层 `l1` 原子事实 / `l2` 带证据的信念 / `l3`
  偏好页面；L2/L3 是可重建的合成层（§4）。`layer` 与 `kind` 正交：`kind` 说"是什么"，
  `layer` 说"多粗"。
- **erased_at**（N4 已落库）：`status=forgotten` 只停召回、正文仍在本机；
  `erased_at` 非空表示正文副本与检索索引已删。两者分开，是为了不把"不再用"谎称成
  "已擦除"（§10）。
- **source_refs**：**每条记忆必须能指回来源**。这是「可检查、可编辑、带来源」的锚点。
- **evidence / proof_count**：L2 每条信念必须指回支撑它的源记忆与原文引文；`proof_count` 是条数，机械可算、可审计。**没有证据的 L2 不成立**——这是「信念」与「断言」的分界。
- **consolidated_at**：用于 freshness awareness——作用域内已有新记忆尚未合并时，该层视为**陈旧**（见 §8）。

## 6. 作用域与装备（loadout）

- 作用域 `scope ∈ {user, workspace, task}`：`user` 全局、`workspace` 绑定目录、`task` 绑定任务。
- **一个 Run 只装备它有权访问的作用域**，由当前身份 + 任务绑定核验。模型不能读另一个 workspace 的私有记忆。
- **不同任务挂不同装备**：调研类任务挂调研记忆，编码类任务挂项目记忆。**不注入全库。**
- 装备 = 作用域过滤 + 状态 / 有效期过滤后的集合；检索在这个集合内做，而不是全局扫。

### 6.1 资源种类（`memory://`）：记忆怎么进入 C4 的 `resources`

C4 的 `resources` 原本是一条**隐含**约定：里面每一个字符串都是绝对文件路径。记忆没有
文件路径，所以必须给"资源"补一个显式的种类维度——否则只剩两条坏路：给记忆编一个假
路径，或者把记忆排除在派发 / 审批之外（等于没有授权与审计）。

#### 为什么不能把记忆伪装成一个目录

把记忆作用域映射成 `.tianshu/memory/workspace/<hash>/` 这类真实目录，是最省事也最
危险的形状：**"删掉投影"就变成"删掉用户文件"**。F10 的整个立论是「记忆是投影，删掉
可重建」（§3.1 / §10）；投影一旦落在真实文件系统的可写目录里，`memory.forget`、重建
索引、清缓存都会变成对用户文件的删除，C4 的覆盖 / 删除审批、写前快照、undo 也会被
拖进一个本来不需要它们的域。所以**拒绝这个形状**。

#### 为什么不给 `ActionRequest` 加 `resource_kind` 字段

加字段就要进 `ActionRequest.to_canonical_dict()`（`src/tianshu/permissions.py`），而
`digest()` / `action_digest()` / `subject_digest()` 都是对该字典的 JCS 摘要
（`permissions.ActionRequest` 的同名方法）。**字段一变，所有 file 请求的摘要全变**：
审批按 `action_digest` 绑定、grant 与派发记录按 `digest` 绑定，历史审批、grant、
`dispatch.requested` 记录与重放比对会**集体失效**。为了一个新能力作废全部既有授权链，
不值得。

> 行号不写在这里：这一层正在被并行改动，`file:line` 会立刻过期；函数 / 类名可以直接
> `grep` 到，是稳定的锚点。

#### 采用 scheme 前缀（已落地：`src/tianshu/resources.py`）

`resources` 里的字符串自己带种类，封闭集 `RESOURCE_KINDS = {file, memory}`
（`resources.RESOURCE_KINDS`）：

| 字符串形状 | 种类 | 判据 / 位置 |
|---|---|---|
| 裸绝对路径（`/Volumes/Date/proj/a.md`） | `file` | `os.path.isabs()`，**第一个判**（`resources.parse_resource`） |
| `memory://<scope>/<scope_id>` | `memory` | 前缀匹配（`resources.parse_resource`） |
| 其他任何 `xxx://`（含 `file://`） | **拒绝** | `ResourceError`（`resources.parse_resource`） |
| 相对路径 | **拒绝** | `resources.parse_resource` |
| 空串 / 非字符串 | **拒绝** | `resources.parse_resource` |

三条要点：

1. **绝对路径必须优先判**，不能"含 `://` 就当 scheme"：POSIX 上 `/Users/x/a://b`
   是合法文件名，历史 `resources` 里真可能出现；判序错了，历史请求会被读成未知
   scheme 而拒绝。测试钉住：
   `tests/test_resources.py::test_absolute_path_with_double_colon_slash_is_still_file`。
   结论：**历史上每一条 `resources` 都是裸绝对路径，回写逐字节不变**
   （`ResourceRef.uri`），既有摘要一个字节都没动。
2. **fail-closed**：不认得的 scheme 一律拒绝，不设"未知 scheme 当文件"的后路——那条
   后路会让 `memory://../../etc/passwd` 变成一次真实文件访问。测试钉住：
   `test_memory_uri_never_becomes_a_file_path` / `test_unknown_scheme_rejected`。
3. **向前兼容**：将来接 web / mcp / child 只需往 `RESOURCE_KINDS` 里加一种，并在
   `parse_resource()` 里实现它的解析，不必再动文件路径那条线。

#### 作用域编码：不哈希

`memory://<scope>/<scope_id>`，`scope_id` 用 percent-encoding 转义
（`resources.MemoryScope.to_uri`）：

- `user`：`scope_id` 固定 `local`（`resources.USER_SCOPE_ID`，校验在
  `MemoryScope.__post_init__`）。D2 单用户，没有第二个用户可言；多用户是另一张契约
  工单，不在字符串里留个位置假装支持。
- `workspace`：`scope_id` 必须是**规范化后**的绝对路径（`MemoryScope.workspace` 走
  `os.path.realpath`）。`/a/b`、`/a/./b`、`/a/../a/b` 是同一个目录；算成三个作用域，
  用户会在三个"不同"的 workspace 记忆里找不到十分钟前存的东西。URI 解析侧也
  canonicalize（`resources.parse_memory_uri`）。
- `task`：`scope_id` 就是 `task_id`，形状校验 `^[A-Za-z0-9_.:-]{1,128}$`
  （`MemoryScope.__post_init__`）。

示例：`memory://workspace/%2FVolumes%2FDate%2Fproj`。

**不哈希**：事件日志里的引用是给人读的。哈希之后 `memory://workspace/9f2c...` 只能
靠猜是哪个目录，而「每条记忆必须能指回来源」是 F10 的硬要求（§5 `source_refs`）。
编码后的样子不好看，但一眼能看出是哪个目录。

#### 派发与执行现在怎么处理 `memory://`

原先把 `resources` 当路径的四条线都改成了**按种类分流**，不再把 URI 送进路径解析：

| 位置 | 记忆资源怎么走 |
|---|---|
| `dispatcher.Dispatcher._authority` | 先按 scheme 分流：解析出 `memory` 后**跳过租约**（租约是给目录的），改由 `memory_exec.memory_scope_allowed()` 核验作用域；越权即 `c4.memory_scope_denied`。文件资源仍走原来的 `ResourceIdentity` + 租约判定，逐字节不变。 |
| `dispatcher.Dispatcher._resolve_target` | `memory://` → 返回 `dispatcher._MemoryTarget`，**不做 `Path.resolve()`**；文件工具收到记忆资源 → `dispatch.memory_resource_mismatch`；不认得的 scheme → `c4.unsupported`。 |
| `permissions.PolicyEngine.classify` → `_classify_memory` | 只有带 `://` 的资源才进记忆分支；裸绝对路径继续走原来的文件判定（历史摘要不变）。`ACTION_CREATE` 查 kind 封闭集，`ACTION_OVERWRITE` 查 `memory_origin`，`ACTION_DELETE` 按删除审批。 |
| `memory_exec.MemoryExecutor._verify` | 执行侧二次核验：资源必须是单个 `memory://`、`memory_id` 与请求声明一致、status/revision 与投影一致——**不接受"prepare 说批过了"**。 |

明文保留的两个边界：**记忆没有目录租约**（不落在目录里，租约无从谈起；作用域授权替代它），**记忆也没有写前快照**（`FileExecutor` 的 preimage 语义不适用于投影——`memory.forget` 是 tombstone，可逆性由事件重放提供，不由快照提供）。这两条是设计选择，不是漏项。

## 7. 记忆写入：投影更新 + 分级审批

记忆写入**不是**红线意义上的「副作用」（它不改用户文件系统），而是**投影更新**——按 AGENTS.md §5「状态是事件的投影」，同事务写事件即可重建。因此审批的判据不是「写了存储」，而是**「是否改变用户长期可见的行为」**：

| 操作 | 效果 | 审批 |
|---|---|---|
| `memory.search` / `memory.get` | 只读 | 不需要 |
| `memory.propose` | 写**候选**（惰性草稿：未激活不召回，可遗忘 / 编辑 / 重建） | 不需要 |
| `memory.confirm` | **激活**（进入长期召回） | 需要；`origin=user_explicit` 除外——用户本人刚说的话就是授权 |
| `memory.forget` | 写 tombstone | 需要（等同删除） |
| `memory.edit`（N1 已落地） | 改一条记忆：旧版保留可查、新版生效 | **需要**（覆盖类） |
| `memory.promote`（N1 已落地） | 把一条记忆**移动**到另一个已声明作用域（按需共享进 workspace / task，也可收回到 user）；原行退役、目标行是候选 | **需要**（换作用域是显式动作） |
| `memory.remember`（N1 已落地） | 人直接记住（`origin=user_explicit`） | 不需要——人本人就是签发者；模型清单里没有这个工具 |
| `memory.consolidate`（N3 已落地） | 合成 L2/L3（只写可重建投影；**确定性合成**，LLM 合成未做） | 不需要；结果 `candidate`，召回仍需 `confirm`；只退役同层的 **candidate** 旧页，已 `confirm` 的 active 页不因合成退役 |
| `memory.erase`（N4 已落地） | 删正文副本与检索索引 | **需要**（等同删除 + Receipt） |

> `memory.consolidate` 已在 N3 落地（PR #98，**只做确定性合成**；LLM 合成明确延后）。
> `memory.remember` / `memory.promote` / `memory.erase` 是控制面专属（`exposed=False`），
> 模型清单里没有它们，但 CLI / daemon RPC 已经能调。别把整张表读成"模型全能调"。三条
> "仅控制面"的判据是 `Task.is_control`（`task.created` payload 的 `control: true`，执行侧
> 再核一次）；`memory.erase` 的回执**不是规则编号**——Receipt 在结果侧产生，回执对不上时
> 结论是 `effect_state=unknown` 并阻断后续擦除。

- `propose` 只生成 `candidate`，**不自动激活**；候选是惰性草稿、可重建，所以不挡。
- `inferred` 来源的候选**永不自动激活**——摘要或网页内容不能变成长期用户事实（防污染与注入）。
- **激活**与**遗忘**才是一等副作用：走 C4 审批 + 一次性消费 + 执行侧二次核验 digest，与覆盖 / 删除同级。
- 用户明说「记住这件事」可直接确认（`origin=user_explicit`）。
- **任务状态、grant、审批决定不存为可覆盖的普通记忆**：它们是事件投影，不是可被记忆覆盖的事实。
- **写入前去重**：抽取后、进索引（FTS / 向量）**之前**做去重——冗余事实在入索引前剔除（mem0 的做法）。去重只影响投影，不改事件日志。
- **写入闸门（Memory Defense）**：任何记忆写入过一道 PII / 密钥扫描；命中即红act（`[REDACTED:...]`）或**在入库前阻断**。这是写入闸门，不是事后清理；与 F12 脱敏、D7 本地 only 同源。

> **落地注记（2026-11-10）**：`origin=user_explicit` 的"免审批确认"目前**没有产生入口**——`memory.exec` 的 `_propose` 固定写 `origin="inferred"`（模型不能自称"用户明示"来绕过审批，这是防绕过），策略层那条 `memory_origin == "user_explicit"` 分支因此暂时走不到。用户明说"记住"的入口要另开（见 §13 #18）。

## 8. 检索与上下文

顺序固定：**先过滤 → 再排序 → 后裁剪**。

1. **过滤**：scope、有效期、status（只 `active`）。
2. **排序**：优先 `user_explicit` > `user_confirmed` > `inferred`；优先当前任务 / 工作区证据。
3. **裁剪**：条数上限 + 字节上限 + 超时。

- 首版默认最多 **5 条 / 合计 4 KiB**（已落地为 `memory.MAX_RECALL_ITEMS` / `MAX_RECALL_BYTES`）。模型可以要求更少、不能要求更多：`memory.search` 的 schema 暴露 `limit`（`minimum=1 / maximum=5`），执行侧 `_memory_search_output` 再夹一次到 `MAX_RECALL_ITEMS`——schema 只是把已有能力显式化，上限不由模型决定。受总上下文预算限制。
- **不把全库放进 system prompt**。检索文本是带出处的上下文资料，由 F5 的 `compile_context`（纯函数）统一预算。
- 实际选中的 `memory_id` / `revision` + 文本快照引用要落进事件，供回放核对（已落地：`context.context_payload(memories=...)` 写进 `model.intent.payload.context`）。
- **冲突事实并列呈现并保留来源**，不能由新抓取的网页静默覆盖用户偏好。
- **freshness awareness**：L2/L3 带 `consolidated_at`；若其作用域内已有新记忆尚未合并，该层视为**陈旧**——不盲信，回落到 L1/L0 原始事实核验后再用。
- 检索信号四路：**语义 / 关键词（BM25）/ 实体 / 时序**。**首版只做前两路**；实体与时序列为后续，须用跨实体 / 跨时间问题评测证明增益后再做（见 §12）。

## 9. 可检查、可编辑、带来源

- 用户能打开「Agent 记住了什么」，**直接编辑** L1–L3——不只是查看 / 删除。
- 理由：记忆的价值取决于准确性；给人纠正的办法，比指望蒸馏永远正确更现实。
- **能编辑（N1 已落地）**：编辑保留 `revision`，用 `supersede` 而非覆盖（旧版可查）。
  `memory.edit`（一次审批产出"旧行 `superseded` + 新行 `active`"两行，PRD §5.2.2）——
  新行是**新 `memory_id` + `revision=1` + `supersedes=旧 id`**，旧行 `revision` 不变、
  仍可按 id 查到；**编辑不允许改 `kind`**。入口：模型经人批准、或人经 CLI / 控制面 RPC。
- **看得见（N6 已落地 Host/IPC 一侧）**：`memory.list` / `memory.get` 给出六项判据
  （`scope` / `origin` / `revision` / `status` / `source_refs` / `proof_count`）＋正文；
  CLI 的 `list|search|get` 与 daemon 的 RPC 是同一套控制面实现（`memory_control.py`）。
  真 Host 的 `POST /rpc` 会把它们代理给 daemon（有测试）。**React 面板还没接**：UI 目前
  **没有**记忆列表 / 编辑入口，端点只是'具备被调用的能力'——别把'端点通了'写成'UI 上
  已经能看见'。
- **⚠ 这些端点不是授权边界**：v1 没有进程级沙箱，`run_command` 能以**同一个 uid** 跑
  子进程；同 uid 进程本来就能读 `run/authkey`、连 IPC、跑 `tianshu memory`。也就是说
  **模型若被授予 `run_command` 且策略允许，它的子进程可以像人一样调控制面（并自己签
  审批）**。N6 交付的是'人机界面与 CLI 共用一套实现、写操作都走 dispatcher'，**不是**
  '模型不可能写记忆'。这条边界见 §13 #30 与 [KNOWLEDGE.md](KNOWLEDGE.md) §7。
- 每条记忆显示来源（事件 seq / 来源引用）：`source_refs` + `evidence`（`proof_count`
  与实际证据条数一致），CLI 与控制面 RPC 都带。
- **增量 delta 刷新，禁止全量重生成**：L2/L3 的蒸馏只能**应用新证据隐含的改动**，其余物理不动。全量重生成会让模型悄悄改写未变的部分（bullet 变数字、大小写漂移、同义改写），长文档会缓慢变成「说法差不多的另一份文档」。这是硬约束，不是优化。
- **保留上一版 + 证据**：每次变化保留上一版内容与它当时站的证据，能回答「上个月它说了什么、依据是什么」。

## 10. 忘记 vs 物理擦除

- **忘记（本阶段交付）**：追加 tombstone，同事务从活动投影移除；失效 FTS / 向量 / 缓存；**新请求与重建索引都不能复活它**。
- **物理擦除（单开契约工单）**：需要追踪历史模型消息、来源事件、备份、共享 blob 引用与保留策略。**不能因为删了 FTS 行就声称历史内容已擦除。**
- 历史 replay 用当时的快照；若用户将来彻底清除导致快照缺失，应明确报「历史证据不可用」，**而不是重取最新记忆**。

**擦除已落地（N4，契约 v1.2）**：`memory.erase`（控制面 only，`delete` + 一次性审批 + Receipt）删除该记忆的**正文副本**（`memory_blobs/` 下按 `content_ref`）与**检索索引**，投影行置 `forgotten` + `erased_at`，`content_state=unavailable`；重建时不得复活（`read_content` 见到 `erased_at` 一律 None，哪怕文件还在）。

**删除顺序写死**：**先删文件 → 再让结果与事件同事务提交**。崩在两者之间只会留下"文件没了、行还没标擦除"——重建时正文如实不可用（`content_state=unavailable` 本来就允许）；反过来（先标擦除、文件还在）才是隐私泄漏。删文件失败则整次擦除如实失败，**不写** `erased_at`。

**这条边界必须写在界面上**：事件日志是 append-only，里面的**读快照删不掉**（§3.1 的实测结论：`memory.search` / `memory.get` 的 `tool.result` 保留模型当时看到的正文）。所以"擦除"= 删副本与索引，**不等于**"历史日志里没有这段文字了"。彻底清日志需要日志压缩 / 重写，与红线 1 冲突，单开契约工单，本轮不做；UI 也不许写"已彻底删除"。

**引用计数是必须的，不是优化**：正文是 `sha256` 内容寻址，不同记忆（不同作用域 / kind）可能共享同一个 blob。擦除只能删**没有任何其它投影行引用**的 `content_ref`；仍被引用的保留文件、只把本行标 `erased_at` + `content_state=unavailable`。否则"擦一条"会让另一条记忆的正文凭空消失——那不是擦除，是数据损坏。回执里要列出"删了哪些 / 哪些被谁引用而未删"。**`memory.promote` 会与它退役的旧行共享同一个
`content_ref`**（搬的是同一段正文，不复制字节）——引用计数把退役行算进去；但**已擦除的行不算**（它自己都声明副本删了，不能拦住别人的擦除）。

## 11. 与现行契约 / 红线的关系

| 红线 / 契约 | 关系 |
|---|---|
| 红线 1（append-only，唯一事实源） | 记忆是投影；**写**事件的参数已脱敏（只留正文哈希/长度），**读**事件的 `tool.result` 保留模型当时看到的正文快照——replay 需要它才能重建输入（见 §3.1） |
| 红线 2（副作用先 intent 后 result + Receipt） | 记忆写入是投影更新（同事务写事件）；**激活 / 遗忘**作为一等副作用走 intent/result + Receipt |
| 红线 4（授权只在执行侧） | 记忆激活 / 遗忘的审批在 dispatcher 执行侧判定 |
| 红线 6（覆盖 / 删除需审批） | agent 发起的 `memory.confirm` 与 `memory.forget` 等同覆盖 / 删除 |
| 红线 8（工具 schema 变化即新工具） | 记忆工具版本化，`schema_hash` 绑定审批 |
| 红线 9（能力声明只用于路由） | 记忆检索能力不构成授权 |
| C2 Tool | 记忆操作是工具 |
| C3 Event | 首版**不新增**事件族（§3.2）；若新增须三处同改 |
| C4 Permission | 记忆作用域复用现有权限收窄；记忆资源用 `memory://<scope>/<scope_id>` 表达（§6.1），不新增 `ActionRequest` 字段 |
| C4 资源种类 | 裸绝对路径 = `file`（历史 `resources` 逐字节不变），`memory://` = `memory`，其他 scheme fail-closed（§6.1） |
| F12 脱敏 / D7 本地 only | 记忆写入过 PII / 密钥闸门，命中红act 或阻断 |

## 12. 明确不做（不抄参照项目的部分）

- **不建独立记忆服务 / 独立事实库**（红线 1）。
- **不引入外部 embedding / VLM 作为运行时硬依赖**（零依赖约束）。向量召回是**可选投影**，缺失时降级 FTS-only 并如实标注，不静默假装有语义。
- **不做多租户 / 团队 / owner / ACL**（D2 单用户；作用域只有 user / workspace / task）。
- **不做云托管 / 自动上报**（D7 诊断本地 only）。
- **不照抄 AGPL 项目的代码**（OpenViking 为 AGPLv3；只取设计，不取实现）。
- **不把会话全文无差别抽成记忆**（首版只做显式记忆 + 有界蒸馏，避免记忆膨胀淹没上下文）。
- **不首版做实体图谱 / 时序检索**：实体抽取与实体作用域检索、时序信号都是有效方向（mem0 / Hindsight 都在用），但须用**跨实体 / 跨时间问题评测**证明增益后再做，不凭「听起来该有」就上。

## 13. 契约增量清单（实际状态）

> 状态是**按代码读出来的**，不是按计划写的。写"未落地 / 未覆盖"的条目就是**没有**
> 对应实现或测试；别把它当已完成。位置给到文件 + 函数名（行号会漂，函数名不会）。

> **v1.2 契约增量的落地口径**：PRD §5.2.2 / §5.4.2 与本文 §5 / §7 / §9 / §10 新增的形状
> （`layer` / `erased_at` / `memory.edit|promote|remember|consolidate|erase` / 规则编号）
> 已由 N1 / N3 / N4 按"代码注册与产生路径同 PR 落地"（SPEC §0.2）全部实现并补上测试函数名
> ——**逐条在本节写成 ✅ 或如实写残留**。本节只按代码事实写；写"未落地 / 未覆盖"的就是真的
> 没有实现或没有测试。

| # | 增量 | 状态 | 位置 / 缺口 |
|---|---|---|---|
| 1 | PRD §4.1 `Memory` 实体字段（本文 §5） | ✅ **17 个字段已全部落地** | 文档 `docs/PRD-v1.md` §4.1；代码 `memory.Memory`（`memory.py`）：`erased_at`（N4，`memories.erased_at`，schema v1→v2 只加可空列）+ `layer`（N3，`memories.layer TEXT NOT NULL DEFAULT 'l1'`，schema v2→v3 就地 `ALTER`，旧行一律 l1）；封闭集 `MEMORY_LAYERS = {l1,l2,l3}` |
| 2 | PRD §7 F10 需求细化（本文 §4 / §6 / §8） | ✅ 文档已写 | `docs/PRD-v1.md` §7 P1 `F10` 行（指向本文） |
| 3 | C2：`memory.*` 工具（版本化 + schema） | ✅ 已落地（N3 后 **10 个**） | `tools.SPECS` 记忆段：**7 个模型可见工具**（`memory.propose` 升 **2.0**；`memory.consolidate` 为 N3 新增 `1.0`/`ACTION_CREATE`；其余 `1.0`）+ 3 个控制面专属 `memory.remember` / `memory.promote` / `memory.erase`（`1.0`、`exposed=False`，不进 `tool_manifest()`）；均无 `path_arg`。`tool_manifest()` 总数 12 → **13** |
| 4 | C2：只读工具执行路径 | ✅ 已落地 | `loop.AgentLoop._exec_read` 在目录解析**之前**分流 → `_exec_memory_read` / `_memory_search_output` / `_memory_get_output`；结果记 `none_applied` |
| 5 | C2：写工具目标解析（`memory://`） | ✅ 已落地 | `loop.AgentLoop._target_for` → `_memory_target`：`confirm` / `forget` 的作用域由记忆自身决定，模型参数改不了 |
| 6 | C4：资源种类解析（scheme 前缀） | ✅ 已落地 | `resources.parse_resource` / `parse_memory_uri` / `MemoryScope` / `ResourceRef`；封闭集 `RESOURCE_KINDS` |
| 7 | C4：派发按资源种类分流 | ✅ 已落地 | `Dispatcher._authority`（记忆跳过目录租约、改判作用域）/ `_resolve_target`（`_MemoryTarget`）/ `_policy_preconditions` / `_check_memory_preconditions` |
| 8 | C4：执行侧二次核验 + Receipt | ✅ 已落地 | `memory_exec.MemoryExecutor._verify` / `execute` / `_propose` / `_transition`；`receipt.build_memory_receipt` |
| 9 | C4：策略判定（作用域 / kind / 审批分级） | ✅ 已落地 | `permissions.PolicyEngine.classify` → `_classify_memory`；执行侧 `memory_exec.memory_scope_allowed` 再判一次 |
| 10 | C4：规则编号（6 条新增） | ✅ 已落地 | `rules.RULE_IDS`（43 → 49）/ `RULE_GROUPS["记忆"]` / `DECISION_RULE_IDS`；`schemas/c4/rules.json` 的 `rule_id.enum`、`x-rule-descriptions`、`x-rule-groups` 同步；给人的说明在 PRD §5.4.1 |
| 11 | C3：**不新增事件族** | ✅ 未新增 | `schemas/c3/event.json` 未改；`MemoryProjection.apply_event` 只消费 `tool.result`，`memory.search` / `memory.get` 的 result 被忽略 |
| 12 | 记忆投影（表 + FTS + 正文内容寻址） | ✅ 已落地 | `memory.MemoryProjection`：`apply_event` / `rebuild` / `projection_hash` / `loadout` / `search` / `get` / `observe` / `list_memories` / `redact_or_block` / `put_content` / `find_duplicate` |
| 13 | F5：选中的记忆身份落进 `model.intent.payload.context` | ✅ 已落地 | `context.context_payload(memories=...)` |
| 14 | CLI 控制面：`tianshu memory list\|search\|get\|confirm\|forget\|remember` | ✅ 已落地 | `cli._cmd_memory` / `_cli_memory_scopes` / `_dispatch_memory_decision` / `_remember_memory`（人敲命令 = 批准，审批仍落库、一次性消费、执行侧二次核验；task 作用域走控制 Run 的 `memory_scopes` 声明） |
| 15 | 测试：资源种类 | ✅ 已覆盖 | `tests/test_resources.py`：23 个 `def test_`，参数化后 44 条 |
| 16 | 测试：投影 / 检索 / 闸门 / 去重 | ✅ 已覆盖 | `tests/test_memory_store.py`：53 个 `def test_`，参数化后 66 条 |
| 17 | 测试：派发 / 审批 / 主循环 / CLI | ✅ 已覆盖（N5 + N2 + N3 后 67 + 26 条） | `tests/test_memory_dispatch.py`（67 个 `def test_`：真派发、intent→result 顺序、审批消费、拒绝不改投影、事务内更新、中途失败 unknown、作用域越权、零目录、工具↔资源种类、未知 kind、`memory://` 下的重复候选、文件路径仍走原路、伪造票据被拒、task 作用域只对本任务可用、零绑定控制 Run 不能写文件、**错装配投影被拒**、**拒绝行不含正文原文**、remember/edit/promote/erase 控制面、终态脱敏、崩溃恢复）与 `tests/test_memory_loop.py`（26 个：工具封闭集与注册表一致、零目录召回、模型侧 propose→confirm 走派发、装备、5 条 / 4 KiB 上限、F5 预算、忘记后新 Run 不召回、replay 用快照、CLI 控制面与 workspace 作用域、**`limit` 在 schema 上存在**、**遗忘后不可按 id 复活**、**`origin` 断言**、能力标注与 score_kind、生产装配与构建入口、CLI 失败路径） |
| 18 | `origin=user_explicit` 的**候选**确认免审批路径 | 策略已测 / **候选路径仍无生产入口** | 策略分支有测试：`test_user_explicit_confirm_needs_no_approval`（测试播种 `user_explicit` 候选走真实 prepare/dispatch）。N1 之后：`user_explicit` 有了产生入口 `memory.remember`（直接写 active，不产生候选），`user_confirmed` 由人批准 confirm 升级（#22）；**免审批确认候选**这一条仍无生产路径——人直接用 remember，模型仍只能写 inferred 候选 |
| 19 | L2/L3 蒸馏（增量 delta、保留上一版 + 证据） | ✅ 已落地（N3，**只做确定性合成**） | 新模块 `memory_distill.py`（`L2_SIMILARITY = 0.34` 重叠系数 / `L2_MIN_GROUP = 2` / `L3_PAGE_BYTES = 4 KiB` / `QUOTE_LIMIT_BYTES = 200` / `BASIS_SEQ_PREFIX = "seq:"`，全部写死并进测试）：**L2** = 同作用域 `active` L1（`consolidatable_sources`：只取 `layer='l1'`、未擦除、未过期、正文在盘上）按 token 交叠聚簇 → 引用式拼装，每条一行 `- <原文一行> [<memory_id>@<revision>]` + 空行分块，`evidence` 每条 `{memory_id, revision, quote}`、`proof_count == len(evidence)`（`consolidate()` 就位校验，坏页直接拒）；**L3** = user 作用域里 `kind=preference` 且 `origin ∈ {user_explicit, user_confirmed}` 的 active L1，按 4 KiB 预算**整行**打包，放不下就停并追加 `[预算已满…]`（不做半行截断）。两次合成**逐字节相同**，改一条源只动它那一块（增量 delta 天然成立，因为正文只由各成员自己的内容决定）。页 id = `mem_<sha256(scope_uri \0 layer \0 content)[:16]>`（同作用域 + 同层 + 同正文 ⇒ 同 id，重复合成幂等）。**写入** `memory.consolidate`（`1.0`/`ACTION_CREATE`/`exposed=True`/args 仅 `{scope, scope_id}`）：workspace **无条件拒**、task 只能自己，结果写 `candidate` + `origin=inferred`，人 `confirm` 后才进召回。**投影** `_apply_consolidate_page` 只认事件 `output`（不重算算法，否则 replay 会"再猜一次"），同作用域同层的旧 **candidate** 页 `_supersede(seq)` 退役（行保留，旧正文与当时 evidence 仍可 `get`；active 页不退役，见下），正文过 `redact_or_block` 闸门。**执行侧先看现有状态再写正文**：同 id 页已存在且是 candidate/active → 不重复写行，`output` 如实给现有 `status` 与 `existing: true`（不能一律写 `candidate`，否则事件说候选、投影是 active，审计与后续 confirm 都对不上）；已退役 / 已擦除 → **在 `put_content` 之前**就 fail-closed（而且是**先把全部页预检完**再统一写：单页先写、后面的页才失败，同样会留孤儿）（先写再失败会留下没有投影行引用的孤儿正文，而恢复流程的孤儿清理只认 `tool.intent.args.content_sha256`，consolidate 的 args 只有 scope，清不到）。`output` 另给 `created` / `existing` 两个计数，读结果的人能分清这几页是新写的还是本来就有的。**freshness**：`source_refs` 带 `seq:<basis_seq>`，作用域内 `MAX(last_seq) WHERE layer='l1' AND status='active' AND erased_at IS NULL AND (expires_at IS NULL OR expires_at > now)` 更大 ⇒ 陈旧；`search()`、`retrieve()` 的**三条路径**（词面 / 无索引 / 融合）共用同一个排序键（`_order_by_priority(..., stale=)` 与融合路都用 `_freshness_rank`）：同一作用域优先级组里按 **`fresh`(0) → `unknown`(1) → `stale`(2)** 三档排（降权不短路，F10 §8"陈旧层先核验再用"）；`L1` 恒为 0——原子事实没有"合成层陈旧"这回事。**`stale` 是三态**：`True` 陈旧 / `False` 已确认新鲜 / `None` **不可判定**（旧行没有 `seq:`；主循环输出里**省略**该字段），不把"判不了"写成"新鲜"。（无索引分支原来没有 stale 排序键，2026-11-11 复审复现后修掉了。）测试：`tests/test_memory_distill.py`（**22 条**：含"无索引也降权"、"stale 不可判定透成 None"、"三档排位 fresh→unknown→stale"、"过期 L1 不算更新证据"、"重复应用幂等"、"active 页不被合成退役"、"正文回退 fail-closed"、"坏页 `layer=l1` / 空 `source_refs` 被拒"）+ `tests/test_memory_dispatch.py` 的 7 条真派发 e2e（`test_consolidate_is_a_real_model_tool_and_writes_candidate_pages` / `test_consolidate_refuses_workspace_scope_at_dispatch` / `test_consolidate_task_scope_only_for_own_task` / `test_confirmed_page_joins_recall_and_stale_after_new_l1` / `test_repeated_consolidate_reports_existing_status_not_candidate` / `test_failed_consolidate_writes_no_orphan_blob` / `test_multi_page_consolidate_prechecks_every_page_before_writing`）；schema 迁移在 `tests/test_memory_store.py`：**v1→v3**（`test_schema_v1_database_is_migrated_in_place`）与 **v2→v3 的 `layer` 补列**（`test_schema_v2_database_gains_the_layer_column_in_place`，旧行一律 l1、可重复执行）各一条。负控 10 条真跑红：每块掺全局字节数（破增量）、`_stale_map` 返回空、派发层去掉 workspace 拒、L3 预算 ×100、L3 去掉 user 限定、就位校验改 `if False`、无索引分支去掉 stale 排序、不可判定当成新鲜、失败路径挪到写正文之后（单页孤儿用例红）、在预检前先写第一页正文（多页孤儿用例红）。**退役语义**：新页只把同作用域同层的 **candidate** 旧页置 `superseded`（行保留、旧正文与当时 evidence 仍可查）；**已 `confirm` 的 active 页不因合成退役**——`memory.consolidate` 是模型可见、免审批的工具，若它能顺手退役人确认过的页面，等于模型可以撤销人的召回授权；人确认的页只有人能退（`forget` / `erase`），新旧页并存时由 freshness 决定谁排前面（2026-11-11 复审提出的设计边界，已按此收口）。页 id 由内容决定，**正文回退**会撞回已退役的 id：执行侧在写正文前 fail-closed，投影侧 `_apply_consolidate_page` 也有同名校验（事件注入路径打红它），不静默停在 superseded、也不悄悄复活。**残留**：① LLM 合成本轮**没做**（确定性合成不等于语义摘要）；② **没有 L2/L3 vs L1 的检索质量评测**，不许宣称"读页更好用"；③ L3 只有 user 作用域；④ 冲突消解没做（矛盾的 L1 在页里并列，不裁决）；⑤ 跨作用域合成 / 对话抽取不做；⑥ 执行侧 workspace 守护经派发路径不可达（与 propose 同款纵深防御，**不声称有测试覆盖**）；⑦ freshness 用逻辑时钟 `seq` 而不是 `consolidated_at`（钟表时间可回拨，**而且 `confirm` 这条生命周期推进会把它改成确认时刻**——拿它当基准会让晚确认的页永远"新鲜"），规格 §4.2 #5 的原文口径已在 SPEC §4.4 记为偏差 |
| 21 | 崩溃恢复（`recover_run`）的记忆分支 | ✅ 已落地（N5，含复审整改） | `recovery.RecoveryReport.memory_not_applied` 单列"这次记忆写没生效"；`recover_run` 在文件写推断**之前**分流记忆写，`assess_run` 也不再把它算进 `pending_writes`。**判据必须是分层的，不能一句话概括**：① **投影/事件层**：投影更新与 `tool.result` 由 `Dispatcher._commit_result` 在同一 SQLite 事务里提交，所以"有 intent 无 result"⇔这一层一定没生效；② **正文 blob 层**：内容型记忆写（propose / remember / edit）在提交**之前**就把正文写进 `memory_blobs/`，崩溃会留下没有行引用的孤儿副本——恢复按 `tool.intent.args.content_sha256` 找到它，**引用计数为 0 才删**（共享 blob 绝不误删），删掉的记进 `memory_orphan_blobs`；③ **`memory.erase` 例外**：它先删正文文件、后提交，删文件在事务之外且不可逆。恢复按 memory_id 查投影：文件还在 ⇒ 确实没生效；文件已没 ⇒ 只能报 **unknown**（`memory_unresolved`，`ok=False`），绝不宣布"没生效"；不阻断目录（记忆没有目录 preimage），但接管判定按 unknown 拒绝。④ 快照清单损坏的早退路径也先判记忆写（清单坏掉不影响这一层的结论）。⑤ 结算时一并把 `dispatches` 行推进到 `error`，不让它继续显示"仍在执行"。测试：`test_recovery_marks_pending_memory_write_as_not_applied`（断电 → `memory_not_applied` / 不阻断 / 投影无行 / 可重试）、`test_content_write_crash_leaves_no_orphan_blob_after_recovery`、`test_recovery_cleanup_never_deletes_a_blob_another_memory_shares`、`test_erase_crash_after_deletion_is_not_reported_as_none_applied`、`test_memory_write_is_settled_even_when_the_snapshot_manifest_is_corrupt`、`test_recover_run_honors_blob_dir_for_a_pending_erasure`、`test_recover_run_honors_a_supplied_memory_projection`、`test_erased_and_missing_rows_have_distinct_recovery_conclusions`、`test_erase_without_a_body_directory_has_no_file_side_effect`、`test_projection_unavailable_still_settles_provable_memory_writes`、`test_cli_recover_names_cleaned_orphan_blobs`。负控（都真跑过）：删 `_recover_run` 记忆分支 → 红；删 `assess_run` 记忆分支 → 红；把 erase 当普通记忆写 → 擦除用例红；关掉孤儿清理 → 孤儿用例红；删清单损坏早退块里的记忆分支 → 该用例红 **残留（复审后补全）**：① `assess_run` 对擦除一律报 `unknown`/拒接管（只读对账里无法安全判定"文件到底删了没有"）——擦除崩溃后先 `recover` 再接管；② §6.3 的"两个真进程断电"未做（用 `SystemExit` 打在 `_commit_result` 上覆盖"副作用已发生、提交未发生"的逻辑窗口，真跨进程死亡主要验证进程/OS 语义）；③ 投影打不开时非擦除写仍判"未生效"（可证明），但孤儿 blob 清不了，报告里写成 problem 待人工核对；④ 正文目录**不是** DB 旁默认值时，调用方要给 `recover_run(memory=...)` 或 `recover_run(blob_dir=...)`——CLI / daemon 已分别传 `stack.memory` / `control_stack.memory`；两者都不给就按默认目录判，擦除结论与孤儿清理会偏（契约限制，见 `_open_memory_projection` 注释） |
| 20 | 向量召回 | ✅ 已落地（N2，**受能力边界约束**） | 新模块 `memory_index.py`：`EmbeddingProvider` 协议 + 确定性 `HashEmbeddingProvider`（**无语义能力**，仅测试/离线基线）+ `MemoryIndex`（表 `memory_vectors` / `memory_vector_meta`，`model_id + dim` 过滤，`rebuild()` 清空重算 + `built` 标记与**覆盖校验**）。`retrieve()` 取 **FTS 候选 ∪ 语义候选**（语义路即使 FTS 为空也跑；语义独有候选按同一套 allowed/active/未擦除/未过期/正文可用过滤后并入），RRF 融合 `bm25` 名次与语义名次，再按作用域优先级分组排序；`memory.search` 输出新增 `semantic` / `degraded` / `fused`，条目带 `score_kind`；CLI `--json` 带上同样三项（形状由数组改成对象，已在 README 标注 breaking）。生产入口：`build_stack(embedding_provider=...)` / `TIANSHU_EMBEDDING_PROVIDER=hash`（CLI / daemon / worker 同一装配路径）**+ `tianshu memory rebuild-vectors` 显式建一次索引**——只装配 provider 不会构建向量（daemon / worker 启动也不自动重建）；只接受 hash 基线，外部 provider 需数据外发许可、尚未接线。**能力边界**：未配置 / 未完整构建（空表、半套向量、换 provider 未重建）→ 一律 `semantic=false` + 原因，不把空索引装成可用。测试：`tests/test_memory_vectors.py`（26 条，含并集、空索引、built 标记、混合 provider、回滚重试、过期过滤、partial tables、search/retrieve 口径）、`tests/test_memory_rank.py`（7 条）。评测：`docs/memory-retrieval-eval.md`（22 query / 25 记忆，FTS MRR 0.864 → hybrid 0.955，Recall@5 两者饱和）。**残留**：没有真 embedding provider；跨千条记忆的召回上限、RRF `k` 敏感性、混合 provider 的产品级切换流程都没评测 |
| 22 | `origin` 的 `user_confirmed` 迁移 | ✅ 已落地（N1） | `memory_exec.MemoryExecutor._transition` 在 confirm 有审批且审批人是人（`Approval.decided_by` 非空）时给 output 加 `origin="user_confirmed"`；`memory._apply_confirm` 只允许 `inferred → user_confirmed`（只升级不降级，别的值直接拒绝）。`user_explicit` 由 `memory.remember` 产生。测试：`test_human_approval_upgrades_origin_to_user_confirmed` / `test_remember_is_control_plane_only` |
| 23 | §8 的作用域优先级（优先当前任务 / 工作区证据） | ✅ 已落地（N2） | 排序键改为 `(作用域优先级, origin 权重, created_at 降序, memory_id)`；优先级只由 `RecallPriority`（当前 task / 本 Run 的 workspace 根）与记忆自身 scope 决定，**不是** `allowed` 的函数——同一上下文逐位可复现。主循环与 CLI 都带上下文（`loop._memory_priority`、CLI `--task-id/--workspace`）。测试：`tests/test_memory_rank.py`（7 条：顺序 / 无上下文退回旧序 / 可复现 / 不捞越权 / 过期不参与 / 裁剪仍在排序后）。负控：去掉优先级 → 顺序用例红 |
| 24 | 编辑 / `supersede` 的**生产入口** | ✅ 已落地（N1） | `memory.edit`（`1.0`，overwrite，需审批）：一次审批 = 旧行 `superseded`（`revision` 不变、仍可按 id 查到）+ 新行 `active`（新 `memory_id`、`revision=1`、`supersedes=旧 id`、`origin` 按"这一版正文的来源"：控制面 edit → `user_explicit`，模型写经人批准 → `user_confirmed`）；`check_request_preconditions` 绑定 `before_revision`，不一致 → `dispatch.memory_stale_revision`；不允许改 `kind`。测试：`test_edit_with_approval_keeps_old_row_and_activates_new` / `test_edit_requires_the_revision_the_human_saw` / `test_cli_edit_keeps_old_row_and_activates_new` |
| 25 | workspace / task 作用域的**创建**入口 | ✅ 已落地（N1） | ① 模型可 `memory.propose` 到**本任务**的 task 作用域；② **workspace 共享**由人经 `memory.promote`（`exposed=False`，需审批）进入：原行立即 `superseded`（原作用域不再召回）、目标作用域新行 `candidate`（再 `confirm` 才召回），`source_refs` 必含原 `memory_id`，Receipt 针对**原行**（`receipt_memory_id`）；③ propose→workspace **无条件拒**（含控制 Task 被复用为模型 Run）。测试：`test_model_can_only_propose_into_its_own_task_scope` / `test_workspace_propose_is_refused_even_on_a_control_task` / `test_promote_is_control_only_and_moves_memory` / `test_promote_to_workspace_canonicalizes_the_target_scope` / `test_cli_promote_moves_memory_into_task_scope` |
| 26 | 派发审计表的正文保留与闸门时序 | ✅ 已落地（N5，含复审整改） | 分三条路径收口：① **从未执行**的拒绝行在 `_reject()` 落库时就是 `content_sha256` + `content_bytes`；② **被采纳**的行在执行期保留完整参数（`args_hash` 绑定 + 执行器从这张表取参），进入终态（`done` / `rejected`，含 `_fail_unknown` 那条）后由 `_set_request_state()` → `_redact_terminal_args()` 脱敏，记忆写的终态推进与投影提交同事务；③ **升级前就在终态的行**由 `Dispatcher._migrate_redacted_args()` 补扫：用 `dispatch_meta` 里的**持久标记**判「做过没有」，标记与补扫在**同一事务**里提交——中途失败/被 kill 时标记不落，下次打开重试（只碰 `args_redacted=0` 的终态行；不要用「列在不在」判，`ALTER TABLE` 会先落盘）。代价是同一 `request_id` 会被写多次，所以 `ON CONFLICT` 取"最后一次写入"的形态：拒绝写脱敏值、重新发起写回完整参数（单向保留会让重新发起被判"参数被改过"）。`args_redacted` 是**事实字段**：非记忆请求 / 无正文的记忆请求一律 0。测试：`test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`、`test_interrupted_backfill_is_retried_on_the_next_open`、`test_accepted_memory_write_args_are_redacted_at_terminal_state`、`test_gate_blocked_memory_write_is_redacted_after_dispatch`、`test_memory_write_that_crashes_mid_projection_is_still_redacted`、`test_request_stored_before_approval_is_redacted_when_it_is_later_rejected`、`test_file_write_args_are_not_redacted`、`test_rejected_file_request_is_not_marked_as_redacted`、`test_intent_events_drop_content_for_every_content_memory_write`、`test_terminal_rows_from_an_older_library_are_redacted_when_opened`、`test_marker_without_the_column_forces_a_rescan`、`test_crash_between_add_column_and_backfill_is_retried`、`test_reissued_request_after_terminal_state_keeps_full_args`。负控：关掉终态脱敏 → 三条脱敏用例红；只修插入路径不改 ON CONFLICT → 等待后否决那条红；`_reject` 无条件标脱敏 → 被拒文件请求那条红；跳过补扫 → 旧库终态行那条红；`_intent_args` 只认 propose → 事件日志正文那条红。**残留（如实记）**：停在 `waiting` / `admitted` 的行仍是完整参数（`dispatch()` 要用它执行）；升级前终态行若 payload 已不可解析则**不补扫**（保留可审计性，`args_redacted` 保持 0 —— 已知 fail-open，只能人工处理）；`blob_dir` 里的正文副本不随审计表脱敏（正文本来就存 blob） **残留（复审后补全）**：① 停在 waiting/admitted；② payload 不可解析的终态行不补扫（fail-open）；③ `blob_dir` 正文副本不随审计表脱敏（设计）；④ 模型工具调用参数里的正文仍在 `model.result` / `run_turns`（replay 必须留，物理擦除见 §10）；⑤ 补扫在 `dispatch_meta` 的持久标记缺失时重试（中断/失败下次打开继续），成功置位后不再回扫——旧库里 payload 修好后不会自动补；⑥ `_backfill_redacted_args` 的 UPDATE 已加 `state IN (done,rejected)`，避免与"重新发起"竞态 |
| 27 | CLI 对 task 作用域记忆的限制 | ✅ 已解除（N1） | `Dispatcher.open_control_run(memory_scopes=...)` 把声明写进 `task.created` payload 与 `Task.is_control`；CLI `memory confirm\|forget --task-id` 用它在**不绑旧 task 目录**的前提下签字。测试：`test_control_run_can_forget_an_old_task_scope_memory` / `test_cli_can_forget_task_scope_memory_with_task_id` |
| 28 | 控制面标记与作用域声明（N1） | ✅ 已落地 | `Task.is_control` / `Task.memory_scopes` 从 `task.created` payload 读回；`Dispatcher.open_control_run` 写 `control: true` + `memory_scopes`；`task.created` 只加键、**不新增事件类型**；rebuild 后仍在（`test_control_marker_and_scope_declaration_survive_rebuild`）。越权 → `c4.memory_control_scope_binding` |
| 30 | 控制面 Host/IPC 记忆端点（N6 / §9） | ✅ 已落地（Host/IPC 一侧；**UI 面板未接**） | 新模块 `memory_control.py` 把控制面记忆操作收成**一份**（CLI 与 daemon RPC 共用；原来 CLI 里四个近似重复的派发助手已收口——写操作永远走 dispatcher 的那条纪律抄两份，早晚有一份会图省事直接改投影）。`ControlPlane._methods` 新增 8 个：`memory.list` / `memory.get` / `memory.remember` / `memory.confirm` / `memory.edit` / `memory.forget` / `memory.promote` / `memory.erase`；`host.py` 原样代理 `POST /rpc`（有真 Host + token 的回归用例），所以**端点层**已通；但 UI 目前**没有**记忆面板/入口——「端点可被调用」不等于「UI 上能看见」。 **身份口径（按事实读，不是授权边界）**：这些端点是**人机界面入口**（UI / CLI），与模型工具面是两条路：`memory.list` 不是工具，其余同名工具走 `AgentLoop` → `Dispatcher` → `MemoryExecutor`（按 `allowed` 装备过滤）——**名字相同不代表权限相同**。**但它们挡不住模型**：v1 没有进程级沙箱，`run_command` 能以同一个 uid 跑子进程，同 uid 进程能读 `run/authkey`、连 IPC、跑 `tianshu memory`（并自己落审批）。所以控制面的真实价值是「人与 CLI 共用一套实现、写操作都走 dispatcher、审批一次性消费、执行侧二次核验」，**不是**「模型无路径」。端点权限只在**跨本地用户**这一层有意义：POSIX `run/` 0700 + socket 0600；Windows 命名管道 ACL 继承创建者、**未**额外加固且**未验证**（M5 已知限制）。写操作用 `HumanIdentity.local_cli()` 落审批（与 CLI 同一 actor）；**不新造身份令牌**——令牌挡不住同 uid，只会多一个能被抄走的东西。视图六项判据由 `memory_control.memory_view` 统一产出（`memory.get` 另给 `content` 与 `content_state`；正文纯文本原样返回，不做 HTML 转换）。错误面：不存在 → `not_found`、参数不对 → `bad_params`、revision 不是当前版本 → `conflict`（先拒，不派发）、未知方法 → `unknown_method`。**方法清单（8 个）**：`memory.list` / `get` / `remember` / `confirm` / `edit` / `forget` / `promote` / `erase`。`promote` 的**源作用域**用 `source_scope` / `source_scope_id`（目标仍用 `scope` / `scope_id`）——两者共用一组键会让「workspace → user」永远看不到源行。写后视图按**记忆自己的作用域**读回（不再默认 user；否则 task/workspace 的编辑会「成功但视图是 null」）。测试：`tests/test_host_memory.py`（**14 条**，真起 daemon + 真 IPC）：六项判据与纯文本正文、默认只看 user（看 task 必须显式给 scope）、`memory.list` 没有同名模型工具、8 个方法都注册、edit 后旧行 superseded 且事件里有 **edit 的** `tool.intent`/`tool.result`、forget 变 tombstone、对已 active 的记忆 confirm 是明确拒绝且 `none_applied`、promote 是移动、task 作用域 edit/forget 带回写后视图、workspace → user 的 promote（`source_scope`）、`memory.erase` 删副本 + `content_state=unavailable`、未知 id/缺参数/未知方法、revision 过期 conflict、**真 Host `/rpc` + token 代理（无 token 401）**。负控：把 edit 端点改成「返回假成功、不派发」→ edit 事件用例红。**控制 Run 的作业入队/认领必须原子**（CI 上抓到的真 bug）：Daemon 的驱动线程与 `ControlPlane` 共用一个作业队列；`open_control_run` 原先先 `enqueue` 再 `claim`，慢机器上驱动线程会先 `claim_next()` 认领那条 pending 作业，控制面随即报 `internal: job 仍由活着的 owner 持有`（`memory.remember` 在 ubuntu CI 上真红）。改成 `JobStore.enqueue_and_claim()` 在**同一个写事务**里入队 + 认领；判据 `test_enqueue_and_claim_never_exposes_a_pending_job`（另一条连接在 `enqueue` 的瞬间抢）与 `test_control_run_claims_its_job_atomically`（控制面路径），负控：改回两步 → 两条都红。**残留**：① **这些端点不是授权边界**：`run_command` 同 uid 可经 CLI/IPC 调同一控制面（模型被授予 `run_command` 时等于可达）——属 F2/exec 的已知边界，需要进程级沙箱或独立 OS 用户才能收口；② React 记忆面板未接（UI 目前没有记忆入口）；③ `memory.search` 的 RPC 未暴露；④ 「无身份拒绝」无法构造反例（同机同用户），只有代码/文档论证；⑤ 越权作用域 → `c4.memory_scope_denied` 在**人**这条路走不到（人本来就是 owner），该判据由模型路径的派发测试覆盖；⑥ Windows 命名管道 ACL 未加固且**未验证** |
| 29 | 物理擦除 `memory.erase`（N4） | ✅ 已落地 | 控制面专属（`exposed=False`）、`delete` + 一次性审批 + Receipt；删唯一 blob + 置 `forgotten`/`erased_at`；引用计数为 0 才删（**已擦除行不计数**），否则回执列 `retained_by`；重建不复活、`read_content` 一律 None。**并发保护：引用计数 + 删文件 + 提交在同一把跨进程执行守卫内**（否则两条共享 blob 并发擦除会都保留文件、留孤儿）。投影 schema v1→v2 只加可空列、不重建；`schema_version` 高于程序支持时 fail-closed；CLI 可擦 forgotten / candidate / superseded（DELETE 前置不再默认 `active`）。测试：`test_erase_deletes_unique_blob_and_marks_erased`（含 `verify_receipts(...) == []`）/ `test_erase_keeps_shared_blob_and_reports_retained_by`（含文件仍在时的 rebuild）/ `test_erase_is_control_plane_only` / `test_erase_commit_runs_inside_the_execution_guard` / `test_cli_erase_deletes_content_and_marks_erased` / `test_cli_can_erase_forgotten_and_candidate_memories` / `test_schema_v1_database_is_migrated_in_place` / `test_newer_schema_version_fails_closed` |

**数据落点（别写成系统标准目录）**：投影表（`memories` / `memory_fts` / `memory_meta`）建在**事件日志同一个 sqlite 库**里——构造时强制 `conn is log.conn`（`MemoryProjection.__init__`），所以事件与投影同事务。正文 blob 默认落在 **DB 旁的 `memory_blobs/`**（`MemoryProjection.blob_dir`），**刻意不放进 `log.blob_dir`**：事件 payload 必须留存供 replay / 审计，记忆正文将来要能按策略物理擦除；两者同处一个内容寻址目录时，同一段字节会变成同一个文件——按策略删记忆正文会误删必须留存的日志材料（理由写在 `MemoryProjection.__init__` 的注释里）。纯内存库（`:memory:`）没有"DB 旁边"，此时 `blob_dir` 为 `None`、正文如实标成不可用，不编一个当前目录下的路径。内核子命令默认 `--db .tianshu/events.db`（`cli.DEFAULT_DB`），daemon 用 `--data-root` 时事件库在 `<data root>/events.db`（`daemon.py`）；记忆投影跟着这个库走。测试钉住这条分离：`test_default_blob_dir_is_separate_from_event_payload_store`（`EventLog` 就算配了 `blob_dir`，记忆正文也不跟过去）。`~/Library/Application Support/AgentOS` 那套只存在于 TECH-ARCH 的设计里，别当成现状。

## 14. 出口验收（判据，逐条对上真实测试）

> **状态时点**：2026-11-11 20:35（`feat/host-memory` 工作区实测，`1728 passed / 7 skipped`——合并 `main @ da6b4a9`（N3 已合）后实测，该基线为 `1714 passed / 7 skipped`；N6 自身加 14 条（`tests/test_host_memory.py`：真 IPC + 真 Host `/rpc`），其余来自 main；其中 **272 条**来自本特性的**八个**记忆测试文件：14 + 67 + 67 + 26 + 22 + 43 + 7 + 26）；以 CI 为准。独立复审的结论与逐条处置见
> [REVIEW-F10-memory.md](REVIEW-F10-memory.md) 与 [REVIEW-RESPONSE-F10-memory.md](REVIEW-RESPONSE-F10-memory.md)。
>
> 判据是"能被一条测试打红"。下表每条都指向**真实存在的测试函数**；写"未覆盖"的就是
> 真的没有测试，别把它当已验收。函数名来自**八个**文件：`tests/test_memory_store.py`
> （54 个 `def test_` / 67 条：投影 / 检索 / 闸门 / 去重 / 擦除 / schema 迁移到 v3（v1→v3 与 v2→v3 各一条））、`tests/test_memory_dispatch.py`
> （67 个：真派发 / 审批 / 事务 / 资源种类 / 伪造票据 / 作用域越权 / 零绑定控制 Run / 错装配投影 / 拒绝行脱敏 / 终态脱敏 / 同号重写 / 崩溃窗口 / 擦除不可逆 / 孤儿 blob / 旧库补扫 / 迁移中断重试 / CLI 打印两栏 / blob_dir 与投影接线 / 恢复分支 / 标记与列脱钩 / memory 优先 / 加列与补扫之间崩溃重试 / **consolidate 派发与召回 / 重复合成如实报状态 / 单页失败不写正文 / 多页失败先预检全部页（一个字节都不写）**）、`tests/test_memory_loop.py`（26 个：主循环 / 预算 /
> replay / CLI / 工具封闭集 / 遗忘后不可按 id 复活）、`tests/test_memory_distill.py`（22 个：L2 引用拼装 / 逐字节确定 / 增量 delta / proof 一致 / 坏页就位校验与应用层参数化 / 重复应用幂等 / 正文回退 fail-closed / active 页不被合成退役 / L3 预算与作用域 / 旧页 supersede（含 evidence 快照）/ replay / freshness（三态、无索引路径、过期 L1））、`tests/test_resources.py`（23 个 / 43 条：资源种类）、`tests/test_memory_rank.py`（7 个：作用域优先级）、`tests/test_memory_vectors.py`（26 个：向量索引 / RRF / 并集 / 就绪状态 / built 标记 / 降级边界 / partial tables）、`tests/test_host_memory.py`（14 个：**真起 daemon + 真 IPC + 真 Host `/rpc`** 的控制面端点：六项判据 / 默认作用域 / 模型工具面 / 8 个方法注册 / edit 走 dispatcher / forget tombstone / confirm 拒绝非候选 / promote 移动 / task 作用域写后视图 / workspace→user promote（source_scope）/ erase 删副本 / 错误面 / revision 过期 / Host token 代理）。

| 判据 | 状态 | 证据 / 缺口 |
|---|---|---|
| 记住「报告用中文、先列结论」后，新任务能检索到 | ✅ 已覆盖 | 存储层 `test_propose_confirm_get_search_round_trip`；**新任务**（后来的 task 装备）`test_user_memory_is_recalled_by_a_later_task`（user 作用域被新任务检索到，且别的工作区私有记忆混不进来）；主循环 `test_zero_directory_task_can_recall_memory`（零目录任务检索到、`allowed` 装备正确、`context.memories` 落身份） |
| 不同 workspace 不串记忆；模型读不到另一个 workspace 的私有记忆 | ✅ 已覆盖 | `test_cross_scope_isolation_and_loadout`、`test_observe_is_scope_filtered`、`test_workspace_uri_scope_mismatch_is_rejected`、`test_workspace_scope_is_denied_cross_task_and_rule_is_registered`、`test_workspace_scope_can_be_used_by_its_own_task`、`test_task_scope_is_only_for_its_own_task`、`test_loadout_equips_only_authorized_scopes` |
| 中文无空格文本与中英混合标识符的词面召回用固定样例量化 | ✅ 已覆盖 | `test_cjk_bigram_recall_is_quantified`、`test_mixed_cjk_and_identifier_recall` |
| 候选未经确认不激活；摘要 / 网页不能自动成为长期用户事实 | 部分覆盖 | 候选不召回：`test_propose_confirm_get_search_round_trip`、`test_propose_really_goes_through_dispatcher_and_orders_intent_before_result`。`origin` 固定写 `inferred` **有断言**：`test_agent_propose_then_confirm_through_dispatcher` 在真实主循环里断言 `memory.origin == "inferred"`（"模型不能自称 user_explicit 绕过审批"）；"网页/摘要不能自动变事实"的入口级判据仍缺 |
| 编辑保留 revision；忘记后新任务 / 重建索引 / 应用重启均不再召回 | 部分覆盖（**编辑侧未兑现**） | 忘记侧 ✅：`test_forget_is_not_recalled_and_rebuild_does_not_resurrect`、`test_forget_and_content_survive_process_restart`、`test_agent_forget_then_new_task_cannot_recall`、`test_candidate_can_be_forgotten`、`test_forgotten_memory_cannot_be_revived_by_id`（遗忘后按 id 也读不到正文，堵住 `memory.get` 绕过 forget 的旁路）。编辑侧 ❌：`test_supersede_keeps_old_row_but_removes_it_from_recall` 是**测试播种** output，**没有生产入口**（§13 #24） |
| 删除整个检索索引可重建，正文与来源仍在 | ✅ 已覆盖 | `test_rebuild_reproduces_projection_hash_and_search`、`test_rebuild_reads_events_not_the_stale_projection`、`test_projection_hash_covers_the_fts_index`、`test_projection_rejects_events_that_replay_order_cannot_explain` |
| 离线 replay 不读当前记忆库最新版本（用当时快照） | ✅ 已覆盖 | `test_replay_uses_the_snapshot_not_the_latest_memory`（忘记 + 删旧 blob 后 replay 仍通过，且 `memory.get` 明确报"历史内容不可用"、不回落新版本）；`test_missing_blob_is_content_unavailable_and_never_falls_back`、`test_missing_blob_at_propose_marks_unavailable` |
| 记忆**激活 / 遗忘**有 intent/result + Receipt；审批被拒则不写 | ✅ 已覆盖 | `test_propose_really_goes_through_dispatcher_and_orders_intent_before_result`（intent 在 result 之前）、`test_confirm_requires_approval_and_rejection_changes_no_projection`（拒绝后投影一个字节不变、无 receipt；批准后 `verify_receipts` 无问题）、`test_forget_requires_approval_and_tombstones`、`test_agent_propose_then_confirm_through_dispatcher`（模型侧 propose→confirm 主循环路径）、`test_projection_update_runs_inside_tool_result_transaction`、`test_mid_write_failure_is_unknown_and_leaves_no_half_projection`、`test_memory_executor_refuses_a_forged_ticket_without_a_dispatch_record`（执行器不接受伪造票据） |
| L2 每条信念能指回证据与原文引文；`proof_count` 与实际证据条数一致（机械核对） | ✅ 已覆盖 | L1 侧：`test_unknown_or_invalid_propose_values_raise`（`proof_count=99` 被拒）+ `MemoryProjection._apply_propose` 的机械校验。L2 侧：`test_l2_content_is_reference_assembly_not_free_form`（正文每行都带 `[memory_id@revision]`，不是自由摘要）、`test_proof_count_and_evidence_are_mechanically_consistent`（`proof_count == len(evidence)`，evidence 每项恰好 `{memory_id, revision, quote}`，长引文截断带标记）、`test_consolidate_refuses_inconsistent_page_from_a_buggy_builder`（合成器交回 `proof_count` 少一条的坏页 → `consolidate` 直接拒，坏页进不了投影）、`test_consolidate_rejects_bad_pages`（**投影应用层**另一条入口：未知 layer / `layer='l1'` / 空 `source_refs` 参数化三例，事件里的坏页也进不去）、`test_reapplying_the_same_page_is_idempotent`（同一页事件应用两次不长行、状态与 revision 不动）、`test_reverting_to_an_old_page_body_fails_closed`（正文回退撞回已退役的页 id → 明确报错，不静默停在 superseded）。负控：就位校验改 `if False` → 坏页用例红 |
| 蒸馏只做增量：未变部分逐字节不变 | ✅ 已覆盖 | `test_two_consolidations_are_byte_identical`（同输入两次合成**逐字节**相同）、`test_changing_one_source_only_moves_its_own_block`（改 m4 一条源 → 只有 m4 那一块变，其余块 `bytes` 逐字节相同）、`test_consolidate_output_is_replayable`（事件重放重建出同一页）。负控：每块掺入全页面字节数 → 逐字节用例红 |
| 陈旧层先核验再用：作用域内有新记忆未合并时，L2/L3 不短路检索 | ✅ 已覆盖（N3 + 复审整改） | `_stale_map` 按 `(scope, scope_id)` 比 `MAX(last_seq) WHERE layer='l1' AND status='active' AND erased_at IS NULL AND (expires_at IS NULL OR expires_at > now)` 与页的 `seq:<basis_seq>`；排序键 `_order_by_priority(..., stale=)` 被**三条路径共用**（`search()`、`retrieve()` 无索引 / 索引不可用、融合路）。测试：`test_freshness_marks_pages_and_demotes_them`（新增 L1 → 旧页 stale 且排在后面）、`test_retrieve_demotes_stale_pages_even_without_a_vector_index`（**页与 L1 同 origin / 同 created_at**，把 origin 权重的掩盖去掉——复审就是在这里抓到的真洞：无索引分支原来没有 stale 排序键）、`test_confirmed_page_joins_recall_and_stale_after_new_l1`（真派发 e2e：改走 `retrieve()`，只断言 stale 标记并校验 `search()` 两路一致——L1 经人 confirm 会升 `user_confirmed`，用它的排序当 stale 证据是假绿）、`test_old_page_is_superseded_and_still_gettable`（旧页被取代后仍可按 id 查到，且 evidence / `source_refs` 与当时一致）。**`stale` 三态**：`test_unknown_freshness_stays_unknown_and_is_not_reported_as_fresh`（无 `seq:` 的旧页 → `Recalled.stale is None`，主循环输出省略该字段，不冒充新鲜）。负控：`_stale_map` 直接 `return {}` → stale 用例红；把无索引分支的 stale 排序去掉 → 新用例红 |
| 记忆写入过 PII / 密钥闸门：含密钥的候选被红act 或阻断，不入库 | 部分覆盖 | **投影与正文 blob 成立**：`test_secret_patterns_are_blocked_before_storage`（参数化）、`test_pii_is_redacted_before_storage`、`test_blocked_candidate_does_not_enter_projection`、`test_pii_bypassing_put_content_is_rejected`、`test_blob_restored_after_propose_still_passes_the_gate`；被拒绝的请求行已脱敏：`test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`。**派发审计表不在闸门覆盖内**（§13 #26）：被采纳的请求行保留原文——闸门在 executor 跑，行在 `prepare()` 就落库 |
| 写入前去重：重复事实不重复进索引（进索引前条数已下降） | ✅ 已覆盖 | `test_find_duplicate_and_deduped_event_do_not_grow_the_index`、`test_duplicate_propose_without_deduped_is_rejected_atomically`、`test_deduped_event_cannot_point_at_a_forgotten_row`、`test_duplicate_propose_is_policy_denied_before_approval` |
| 资源种类封闭：历史裸绝对路径仍是 `file` 且回写逐字节不变；未知 scheme / 相对路径 fail-closed | ✅ 已覆盖 | `test_absolute_path_is_file_and_byte_identical`（参数化）、`test_absolute_path_with_double_colon_slash_is_still_file`、`test_unknown_scheme_rejected`、`test_relative_or_empty_rejected`、`test_relative_workspace_scope_is_rejected_before_realpath`（`memory://workspace/relative` 与 `MemoryScope.workspace("relative")` 都拒，**不再**被 realpath 按 CWD 补成绝对路径）、`test_memory_uri_never_becomes_a_file_path`、`test_resource_kinds_are_closed` |
| 作用域编码可指回来源：`memory://workspace/<percent-encoded 绝对路径>` 解析回同一路径 | ✅ 已覆盖 | `test_workspace_uri_percent_encodes_and_round_trips`、`test_workspace_scope_is_canonicalized`、`test_user_scope_uri`、`test_task_scope_uri`、`test_memory_scopes_are_closed` |
| 工具与资源种类不得互换：文件工具收到 `memory://`、记忆工具收到绝对路径都要拒 | ✅ 已覆盖 | `test_tool_and_resource_kind_must_match`（两个方向都拒，断言 `dispatch.memory_resource_mismatch`）、`test_unknown_memory_kind_is_policy_denied`（未知 kind → `c4.memory_kind_unsupported`）、`test_file_dispatch_still_uses_bare_absolute_path_and_receipt`、`test_absolute_file_path_with_double_colon_slash_stays_a_file`（文件路径仍走原路）、`test_zero_binding_control_run_cannot_write_files`（零绑定控制 Run 不能写文件） |
| `memory.*` 七个模型可见工具真出现在 `tool_manifest()` 里；控制面专属的 `memory.remember` / `promote` / `erase` **不在**清单里 | ✅ 已覆盖 | `test_memory_tools_are_real_specs_without_path_args`（真实 `tool_manifest()` + 真实 `SPECS`：propose 2.0、其余 1.0、`path_arg==""`、`action` 映射、`additionalProperties=False`、`memory.remember.exposed is False` 且不在清单）；`test_memory_tool_sets_match_the_registry`（主循环读 / 写工具封闭集与注册表一致） |
| 零目录任务也能用记忆（读与写都不该被"没有目录"挡住） | ✅ 已覆盖 | `test_propose_works_for_zero_directory_task`、`test_zero_directory_task_can_recall_memory` |
| 检索文本走 F5 字节预算：超预算时是明确预览 / 丢弃，不是静默塞入 | ✅ 已覆盖 | `test_recall_is_capped_at_five_items_and_4kib`、`test_recall_text_goes_through_compile_context_budget`、`test_default_budget_keeps_recall_verbatim_and_pure`（同输入同 digest，纯函数） |
| 实际进入视图的记忆身份落进 `model.intent.payload.context`，供回放核对 | ✅ 已覆盖 | `test_zero_directory_task_can_recall_memory`（断言 `context["memories"]` 的 `memory_id` / `revision` / `content_ref`）；预算压掉时 `test_recall_text_goes_through_compile_context_budget` 断言 `memories` 不声称选用 |
| CLI 控制面能列出 / 检索 / 读取 / 确认 / 忘记 | ✅ 已覆盖 | `test_cli_memory_list_search_get_confirm_forget`（黑盒敲 CLI：list → search → get → confirm → forget，确认 / 忘记仍走真实审批与一次性消费） |

| `memory.remember` 只能控制面调用；普通任务被拒（`c4.memory_control_scope_binding`） | ✅ 已覆盖 | `test_remember_is_control_plane_only`（普通任务 prepare 被拒；控制 Run 成功创建 `active` + `user_explicit`） |
| 控制 Run 能替旧 task 作用域签字，且**不**获得目录租约 | ✅ 已覆盖 | `test_control_run_can_forget_an_old_task_scope_memory`（`lease_paths == ()`）、`test_cli_can_forget_task_scope_memory_with_task_id` |
| 人批准 confirm 后 `origin` 升为 `user_confirmed`（模型不能自称） | ✅ 已覆盖 | `test_human_approval_upgrades_origin_to_user_confirmed`；模型侧仍是 `inferred`：`test_agent_propose_then_confirm_through_dispatcher` 的 origin 断言 |
| 控制面标记与声明作用域落进事件、重建后仍在 | ✅ 已覆盖 | `test_control_marker_and_scope_declaration_survive_rebuild`（`tasks.rebuild_from_events()` 后 `is_control` / `memory_scopes` 不变） |
| 模型只能写本任务的 task 作用域；workspace **无条件**只能由人 `promote` | ✅ 已覆盖 | `test_model_can_only_propose_into_its_own_task_scope`、`test_model_cannot_propose_into_workspace_scope`（派发层即拒、投影无行）、`test_workspace_propose_is_refused_even_on_a_control_task`（**控制任务被复用为模型 Run** 也拒——复审探针复现过的洞） |
| CLI `memory remember` 直接创建 `active` + `user_explicit` 记忆 | ✅ 已覆盖 | `test_cli_remember_creates_active_user_explicit_memory`（黑盒 CLI + 投影复核） |

| 编辑一次审批两行：旧版 `superseded` 可查、新版 `active`、不许改 kind | ✅ 已覆盖 | `test_edit_with_approval_keeps_old_row_and_activates_new`（旧行 revision 不变 + 新行 origin=user_confirmed + 凭据可核对）、`test_edit_requires_the_revision_the_human_saw`（过期 revision 拒）、`test_cli_edit_keeps_old_row_and_activates_new`（控制面 edit → 新行 origin=user_explicit） |
| 提升是移动：原行退役、目标作用域候选、Receipt 指原行、只控制面能做 | ✅ 已覆盖 | `test_promote_is_control_only_and_moves_memory`（模型路径 `c4.memory_control_scope_binding`；原作用域不再召回；`source_refs` 含原 id；**断言 `receipt.payload.resource == 原 id`、`root == 原作用域`**；`verify_receipts` 通过）、`test_promote_to_workspace_canonicalizes_the_target_scope`（原始路径参数归一成 canonical 根）、`test_cli_promote_moves_memory_into_task_scope`（控制面 CLI；来源随行 `user_explicit`） |
| 编辑 / 提升**永远**要人工审批（`user_explicit` 的免审批只管 confirm） | ✅ 已覆盖 | 策略与 `_requires_approval` 都按 `memory_tool == "memory.confirm"` 收口；负控：去掉该限制 → `test_cli_edit_keeps_old_row_and_activates_new` 红（`overwrite 缺少审批绑定`） |

| 擦除只删唯一引用的正文；共享 blob 保住别的记忆；已擦除行不再计数 | ✅ 已覆盖 | `test_erase_deletes_unique_blob_and_marks_erased`（blob 消失、`erased_at`、重建不复活、`verify_receipts(...) == []` 且回执含 `memory_erasure` 证据）、`test_erase_keeps_shared_blob_and_reports_retained_by`（共享同一 `content_ref`：第一条 `deleted=False`/`retained_by=[另一条]`，第二条才删；**文件仍在时 rebuild 也不复活**）、`test_erase_commit_runs_inside_the_execution_guard`（计数/删文件/提交在同一把跨进程守卫内，负控：守卫外提交 → 红） |
| 擦除只控制面能做；重复擦除是确定拒绝；旧库自动补列；高版本 fail-closed | ✅ 已覆盖 | `test_erase_is_control_plane_only`（模型路径 `c4.memory_control_scope_binding`、不在 `tool_manifest()`）、`test_erase_deletes_unique_blob_and_marks_erased`（第二次 prepare 即拒）、`test_schema_v1_database_is_migrated_in_place`（v1 库补 `erased_at` + `schema_version=2`、不重建不丢正文）、`test_newer_schema_version_fails_closed`（`schema_version=3` 拒绝打开）、`test_cli_erase_deletes_content_and_marks_erased`、`test_cli_can_erase_forgotten_and_candidate_memories`（忘记后 / 候选可擦） |
| 崩溃未落 result 的记忆写：投影层判"未生效"、擦除层判 unknown，都不编造投影、不误删共享 blob | ✅ 已覆盖（N5 + 复审整改） | `test_recovery_marks_pending_memory_write_as_not_applied`（断电模拟：`assess_run` 归 `none_applied` 且 `not blocks_takeover`；`recover_run` 报 `memory_not_applied`、`blocked_workspaces == []`、投影无行、恢复后可换 `call_id` 重写）；`test_content_write_crash_leaves_no_orphan_blob_after_recovery`（正文已落盘 → 恢复清孤儿副本，`memory_orphan_blobs` 可核对）；`test_recovery_cleanup_never_deletes_a_blob_another_memory_shares`（共享 blob 一条都不删）；`test_erase_crash_after_deletion_is_not_reported_as_none_applied`（擦除已删文件 → `memory_unresolved` + `ok=False` + `assess_run.unknown` + `dispatch` 行转 `error`，不报"没生效"、不阻断目录）；`test_memory_write_is_settled_even_when_the_snapshot_manifest_is_corrupt`（清单坏掉也照常结算、不挡目录）。负控：删 `_recover_run` 记忆分支 → 红；删 `assess_run` 记忆分支 → 红；把 erase 当普通记忆写 → 擦除用例红；关掉孤儿清理 → 孤儿用例红 |
| 记忆写的侧效应分三层判定：投影/事件、正文 blob、不可逆文件删除 | ✅ 已覆盖（N5 + 复审整改） | 同一份代码里三种副作用，判据不能混：投影/事件同事务（可证明）、blob 在提交前落盘（孤儿要清）、erase 删文件在事务外（不可逆 → unknown）。分层写在 `recovery._memory_effect` 的 docstring 里，三条判据各有用例（见上一行）；`memory_unresolved` 与 `none_applied` 是**两个栏位**，不合并三态（红线 5/11） |
| 派发审计表不永久保存记忆正文原文；非记忆请求的审计不被过度脱敏 | ✅ 已覆盖（N5） | `test_accepted_memory_write_args_are_redacted_at_terminal_state`（被采纳 → 终态后只剩 `content_sha256` / `content_bytes` / `args_redacted=1`，`kind` 仍在可审计）、`test_gate_blocked_memory_write_is_redacted_after_dispatch`（采纳后被闸门挡掉也脱敏）、`test_memory_write_that_crashes_mid_projection_is_still_redacted`（执行中途炸掉 → 请求转 rejected，同样脱敏）、`test_request_stored_before_approval_is_redacted_when_it_is_later_rejected`（`prepare()` 已存的 waiting 行在人否决时走 ON CONFLICT 分支，也必须覆盖正文——不覆盖就是永久残留）、`test_rejected_memory_write_keeps_no_raw_content_in_the_request_table`（拒绝侧）、`test_file_write_args_are_not_redacted`（文件写请求 `args_redacted=0`、正文原样）。负控：关掉终态脱敏 → 三条全红、文件写那条仍绿；只修插入路径不改 ON CONFLICT → 等待后否决那条红 |
| 作用域优先级：当前任务 > 本 Run 的 workspace > user，且与 `allowed` 无关、逐位可复现 | ✅ 已覆盖（N2） | `tests/test_memory_rank.py` 7 条（顺序 / 无上下文退回旧序 / 可复现 / 不捞越权 / 过期不参与 / 裁剪仍在排序之后 / `task_id=None` 不抬 task）。负控：把排序键改回 origin 优先 → 顺序用例红 |
| 向量召回：语义候选与 FTS 取**并集**（FTS 空也跑）、能力边界自报（未配置 / 未构建 / 换 provider → FTS-only + 原因）、built+覆盖校验、并集过滤、重建逐字节确定、RRF 可手算、索引跟随生命周期 | ✅ 已覆盖（N2） | `tests/test_memory_vectors.py` 26 条；评测报告 `docs/memory-retrieval-eval.md`（22 query / 25 记忆，只报实测）。负控：语义只采纳 FTS 候选 → 并集用例红；availability 永不降级 → 红；去掉确认后的索引钩子 → 红；删 built 分支 → built 用例红 |

| 控制面能看见记忆：`memory.list` / `memory.get` 走真 IPC、给六项判据与纯文本正文 | ✅ 已覆盖（N6） | `test_list_and_get_return_the_six_judgement_fields`（六项判据一个不少、列表不带正文、get 正文逐字节原样——含 `<b>` 与 URL，后端不做 HTML 转换）、`test_default_scope_is_user_and_other_scopes_must_be_named`（默认只看 `user`，看 task 必须显式给 scope；缺 `scope_id` → `bad_params`）。负控：把端点改成"返回假成功、不派发" → edit 事件用例红 |
| 控制面能改记忆，且**只能按规矩改**：edit / forget / confirm / promote 全走 dispatcher（事件里有 intent/result、审批一次性消费） | ✅ 已覆盖（N6） | `test_edit_rpc_writes_intent_and_result_events`（断言事件里是 **edit 的** `tool.intent` / `tool.result`，旧行 superseded、新行 active、审批已决定——只断言"有 intent/result"会被 remember 的旧事件掩盖）、`test_forget_moves_state_through_the_dispatcher`、`test_confirm_on_an_already_active_memory_is_rejected`（非候选 confirm 明确拒绝且 `none_applied`）、`test_promote_moves_the_memory_and_old_scope_stops_recalling`（移动语义、新行 candidate、`source_refs` 指回原行）、`test_stale_revision_is_refused_before_dispatch`（revision 过期先 `conflict`，不先派发） |
| 控制面端点**不在模型工具清单**：`memory.list` 不是工具，8 个方法都注册在控制面 | ✅ 已覆盖（但**只证明名字/路径**） | `test_memory_list_has_no_model_tool_counterpart`、`test_all_eight_control_plane_methods_are_registered`。**这只证明「方法名不在 tool_manifest()」，不证明「模型不可达」**：v1 无进程沙箱，`run_command` 能以同 uid 调 CLI/IPC（见下一行）。`memory.get|edit|forget…` 与模型工具同名不同路（模型按 `allowed` 过滤），名字相同不代表权限相同 |
| 控制面端点只对**本机人**可达 | ❌ **不成立为授权边界**（如实记） | 端点权限只在跨本地用户这层起作用（POSIX `run/` 0700 + socket 0600；Windows 命名管道 ACL **未加固且未验证**）。**同 uid 进程（含模型经 `run_command` 起的子进程）可以连 IPC / 跑 CLI**，因此这个端点**不能**被当成「模型不能写记忆」的判据；它交付的是「人机界面与 CLI 共用实现 + 写操作都走 dispatcher」。无身份拒绝无法构造反例（同机同用户），只有代码/文档论证——不声称已验证 |

## 15. 与实施计划的关系

[NEXT-CAPABILITIES-PLAN.md](NEXT-CAPABILITIES-PLAN.md) §5 是实施拆解（PR08 `docs(contract)` / PR09 `feat(memory)`）；本文是该 PR 的设计输入。两者冲突时**以本文为准（设计）**，但任何契约变更仍需走 `docs(contract):` 独立 PR。

N1–N6 的**逐条施工规格**（判据与负控口径、文件所有权、契约增量清单）都在 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md)，每条都有落地记录（§2.3 / §3.4 / §4.4 / §5.4 / §6.4 / §7.4）。**N6 只交付了 Host/IPC 一侧的端点，React 记忆面板还没接**——那属于 UI 工作流，别把这句读成"UI 上已经能看见"。该规格**不改变**本文的设计结论；与本文冲突时以本文为准。

## 16. 参考

- [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) — 分层蒸馏（L0–L3）、记忆资产化、装备（loadout）、默认私有、可编辑记忆、冷启动 save file。
- [OpenViking](https://github.com/volcengine/OpenViking) — 上下文即虚拟文件系统、三层读取（abstract/overview/detail）、目录作用域检索、会话成文件 + diff、记忆带引用。
- [OpenViking 设计自述](https://blog.openviking.ai/post/openviking-context-database/) — 五条件公式与「把复杂度从用户移到系统」。
- [mem0](https://github.com/mem0ai/mem0) — ADD-only 加性抽取（不静默改写）、写入前去重、实体抽取、语义+BM25+实体+时序四路检索、SQL 事实为源 + 向量/实体为投影。
- [Hindsight](https://github.com/vectorize-io/hindsight) — Observations（带证据与 proof count、refine 而非覆盖）、Mental Models（预算好的可启动页面、读取为 DB read）、增量 delta 刷新防漂移、freshness awareness、Decision Models、Memory Defense。
- 天枢既有地基：[F5-context-compile.md](F5-context-compile.md)（纯函数预算编译）、[M5-desktop.md](M5-desktop.md)（作用域与审批语义）。
