# Desktop Agent OS — PRD v1.0

> 本文合并自：`convergence-v1.md`（架构收敛）、`general-agent-v2.md`（通用 agent 与执行模型）、`review-8-projects.md`（8 项目核对）、`agent-core-v1-prd.md`（首版 PRD）。
> 本文取代以上四份，作为唯一有效版本。
> 范围变更：**v1 不做图片与视频生成**，但 E2 作业模型的协议与接口预留在 v1 定义、不实现。
> 决策状态：D1–D7、T1 全部已定（D6 于 §4.5 定稿）。见 §13。
> **2026-11-11 工程评审合并**：新增后续交付要求与发布门禁（§11.1），保留既有核心和十一条红线。要求纳入 PRD ≠ 已实现、契约已冻结或验收已通过；本轮仅改文档，不追溯改写 M0–M5 的历史验收。原方案 → 新要求 → 原因 → 影响范围见 §13.6。
> **阅读口径**：本文规定产品要求；当前实现与未决问题以 [KNOWLEDGE.md](KNOWLEDGE.md) §2 / §7 的带日期记录为入口，再核对代码与验收证据。各阶段计划是实施拆解，不能自行覆盖本文；代码与要求不一致时登记缺口，不把任一方静默当作已完成。

---

## 1. 产品定义

### 1.1 一句话

一个**桌面 Agent 操作系统**：本地常驻、任务可恢复、权限可审计、模型可自选。用户把它当成一台"能自己干活的电脑"，而不是一个聊天框。

### 1.2 三种角色，三个所有权

这是整个架构的第一原则，任何设计冲突都以此裁决：

| 角色 | 拥有什么 | 不拥有什么 |
|---|---|---|
| **Rust Desktop Host** | 操作系统权限（窗口、TCC、Keychain、系统 API） | 不拥有任务状态，不做模型调用 |
| **Python Agent Daemon** | 任务执行的拥有者（循环、上下文、工具、恢复；记忆属 **v1.1**，见 §7 的 F10） | 不拥有 OS 权限，不直接弹授权框 |
| **React UI** | 用户交互 | 不拥有任务执行，不是唯一事实源 |

推论：**关掉 UI ≠ Agent 停止；Agent 停止 ≠ 任务数据丢失。** 这条写在最前面，因为它是桌面 agent 与 Web agent 最根本的差别。

### 1.3 不是什么

- 不是聊天客户端（对话只是它的一种界面）
- 不是某个 Agent SDK 的包装器（协议必须自己持有）
- 不是工作流引擎（n8n/Dify 那种 CRUD 形态不适合长任务 agent）
- 不是多 agent 角色扮演平台（v1 不做）

### 1.4 保留的核心与产品成熟度标准（2026-11-11）

后续网络、MCP、记忆与子代理建设保留以下基础，不为追随某个框架而重建另一套状态或授权系统：

1. **持久化事实与用户任务独立于模型**：SQLite 事件日志及同事务投影、Task/Run 分离、claim/lease fencing、intent/result/Receipt 继续作为恢复与审计基础。
2. **执行侧授权**：审批绑定具体动作，单次消费，执行前复核；撤销与执行的先后关系可解释，不把提示词当安全边界。
3. **证据驱动验收**：真子进程、故障注入、负控与独立复审继续保留；测试数量不能代替测试有效性与真实任务效果。
4. **确定性上下文编译**：保留纯函数与来源追溯；检索、模型摘要等非确定性工作先产出持久化材料，再交给编译器（§7.1）。

产品成熟度按**安全性、完成度、可靠性、效率**分别衡量（§10.4）。核心里程碑完成不等于可面向普通用户正式发布；新增技术必须对应可复现的问题或用户收益，并以固定评测、平台证据与回退路径验收。

---

## 2. 范围

### 2.1 v1 做什么

1. 通用任务执行：编码、文件整理、信息搜集、调用本地与远端工具
2. 模型自选：用户可配多个 chat provider（OpenAI / Anthropic / Gemini / Ollama / OpenAI-compatible）
3. 长任务：可暂停、可恢复、可取消、崩溃后可重建
4. 权限与审计：执行侧强制授权，副作用有 receipt
5. 记忆：用户可见、可编辑、有来源、有作用域（**v1.1 交付**，见 §7 的 F10）
6. 评测：文本/工具类任务的可复现评测集

### 2.2 v1 明确不做

| 不做 | 原因 | 何时重新考虑 |
|---|---|---|
| 图片 / 视频生成（E2） | 执行模型与 E1/E3 不同，强行合并会把两条线都做坏 | v2（协议钩子在 v1 已定义） |
| 多 agent 自由协作 / 角色制 | 状态不可审计、权限不可证明 | 有 receipt 与权限域之后，按权限域拆 |
| Windows Computer Use | UIA 不稳定，驱动成本高 | macOS 版真实使用 3 个月后 |
| Plugin 市场 | Skill + Tool 已覆盖 90%，只增加供应链攻击面 | 出现真实第三方分发需求 |
| 自研向量数据库 / 自研沙箱隔离 | 无理由；后者等于自研内核漏洞 | 永不 |
| 记忆 / 知识库（F10） | v1 不实现语义检索，事件日志已足够支撑单会话 | v1.1 |

### 2.3 为 v2 预留的缝（v1 定义形状；`job.*` 已提前使用）

1. `JobResult` 类型与 `JobState` 枚举（见 §4.4）——先有类型，后无迁移
2. `Artifact` 表与内容寻址存储（表见 §4.1，v1 要求见 §7 F11）——**独立产物表与截图存储尚未实现**。现有事件大 payload、写前副本，以及上下文编译器超阈值工具消息的 spills 已使用内容寻址；`loop._persist_spills` 在模型请求前保存编译器收到的原文。工具本身已截断的内容不会因此被恢复，不能等同于完整原始数据档案或通用 `artifact.read`（见 §5.2.3）。
3. `ToolDefinition.exec_model ∈ {E1, E3}`，枚举里预留 `E2` 值但不允许使用
4. 事件类型 `job.*`：**提前使用**——本条原本只要求定义形状、v1 不产生，但 M2 起已实际产生，共五种：`job.enqueued` / `job.claimed` / `job.finished` / `job.requeued` / `job.parked`。早期的 `job.lost` 常量没有任何发射点，已删除："作业丢失"由租约过期 + `job.parked` / `job.requeued` 表达，多一个没人写的类型只会让人以为存在这种状态转移。

---

## 3. 架构

### 3.1 进程架构

```
┌─────────────────────────────────────────────┐
│ Desktop Application (Tauri 2 · React · TS)  │
│ 对话 / 任务中心 / 权限审批 / Trace 查看器      │
└────────────────┬────────────────────────────┘
                 │ 类型化 IPC（版本化）
┌────────────────┴────────────────────────────┐
│ Desktop Host (Rust)                          │
│ Native System Bridge · Permission Broker     │
│ 窗口 · 系统 API · TCC · Keychain · 文件监护   │
└────────────────┬────────────────────────────┘
                 │ 本地 RPC
┌────────────────┴────────────────────────────┐
│ Agent Daemon (Python)                        │
│ Agent Kernel · Harness · Model Gateway       │
│ Task Manager · Context · Memory · Tool Router│
│ Skill Engine · Policy Engine                 │
└───────┬──────────────┬──────────────┬───────┘
        │              │              │
   Tool Workers   Sandbox Workers   Computer Use
   文件/浏览器/MCP   Shell/Python      (macOS 优先)
        │
┌───────┴─────────────────────────────────────┐
│ Local Persistence                            │
│ SQLite · Event Store · Memory · Trace        │
│ Artifacts (content-addressed)                │
└─────────────────────────────────────────────┘
```

### 3.2 三种执行模型（v1 实现两种）

| 模型 | 形态 | v1 | v2 |
|---|---|---|---|
| **E1 会话循环** | token 流、毫秒～秒、可流式、回合边界可暂停 | ✅ 实现 | — |
| **E3 沙箱副作用** | 有副作用、需 before/after 证据、要 receipt | ✅ 实现 | — |
| **E2 生成作业** | 异步、秒～分钟、有队列位置、产物是二进制大件 | ⛔ 只定义类型 | ✅ 实现 |

**三条硬性边界规则：**

1. **E2 不得伪装成 E1 的工具调用。** 长作业塞进工具循环会导致流式超时、会话阻塞、取消语义失真。v1 虽不实现 E2，但工具定义里必须预留 `exec_model`，防止 v2 时有人"顺手加个工具"。
2. **E2 的重试与 E1 不同，但不是"免费 vs 花钱"。** E1 重试**没有数据副作用**，但**每一次重试都真实计费**——上一版写"E1 重试幂等"是错的，幂等指的是不重复写数据，不是不重复花钱。E2 重试则在计费之外还会重复产出产物。所以两者都要进预算账本（§5.6）；区别只在"能否安全重试"的判定条件不同。

3. **E3 的 receipt 在 E2 不适用。** 生成作业的"副作用"是计费与产物文件，对账对象是账单和产物哈希，不是文件 diff。

### 3.3 三层契约

依赖严格单向：**Agent Core 不 import 任何 provider SDK**。所有跨层通信只走 §5 的契约。

### 3.4 依赖与技术演进（新增要求，待依赖 ADR 落地）

- 采用“**核心少依赖、集成使用维护中的 SDK / 库**”的演进方向，不把零依赖本身当产品目标，也不把某个框架的私有状态当事实源。官方协议 SDK、完整 schema 校验器、网络与检索组件可进入候选。
- **当前运行时仍是零第三方依赖**；本轮不改依赖清单或 Python 版本。新增依赖前必须以独立 ADR / 契约 PR 同步 AGENTS、TECH-ARCH、锁文件与构建门禁，说明收益、维护责任、许可证、升级及替换路径。SDK 类型与异常必须在 adapter 内转为自有契约。
- 保留 macOS 13+、mac-arm64 / mac-x64 / win-x64 / win-arm64 目标。下载到制品不等于原生 import、启动或任务验收通过；候选不满足目标时，更换实现或显式标注该可选能力不可用，不静默抬高最低系统或删除架构。
- 模型、检索、浏览器和协议版本按固定任务集验证后切换，保留可回退 adapter；季度评估候选，安全修复及时处理。动态工具、混合检索、持久化子任务按真实收益逐步交付；GraphRAG、外部 A2A 或分布式编排须有明确需求与评测收益后再引入。

---

## 4. 核心数据模型

### 4.1 实体

| 实体 | 用途 | 关键字段 |
|---|---|---|
| `Agent` | Agent 定义（**v1 不建表**，见下方说明） | `id, name, profile, config_ver` |
| `AgentSession` | 会话（**v1 不建表**，见下方说明） | `id, agent_id, status, workspace` |
| `Conversation` | 对话（**v1 不建表**，见下方说明） | `id, session_id, title` |
| `Message` | 消息 | `id, role, content[], meta` |
| `Task` | 用户想完成的事 | `id, goal, status, priority, acceptance, workspaces[]` |
| `Workspace` | 任务的授权目录（0..n） | `id, root_path, mode: readonly\|writable, snapshot_policy` |
| `Run` | 为完成 Task 的一次执行 | `id, task_id, agent_id, state, budget` |
| `RunStep` | 执行步骤 | `id, run_id, seq, step_type, state` |
| `ToolExecution` | 工具调用 | `id, run_id, tool_id, args_hash, status, effect_state` |
| `Approval` | 审批 | `id, execution_id, digest, state, decided_by` |
| `Receipt` | 副作用凭据 | `id, execution_id, before_ref, after_ref, evidence[]` |
| `Artifact` | 产物（**v2 独立表**，见下方说明） | `id, sha256, mime, bytes, kind, run_id` |
| `Memory` | 长期记忆（**v1.2**，F10；**投影，不是独立事实源**；字段细化见 [F10-memory-design.md](F10-memory-design.md) §5） | `memory_id, revision, scope, scope_id, kind, layer, content_ref, source_refs[], evidence[], proof_count, origin, status, consolidated_at?, created_at, expires_at?, supersedes?, erased_at?` |
| `Event` | 事件日志 | `seq, run_id, type, actor, payload_ref, payload_hash, parent_seq` |
| `Checkpoint` | 恢复指针（**v2**，见下方说明） | `id, run_id, seq, state_digest, pending_intents[]` |

**三行 `v1 不建表` 怎么读**：`Agent` / `AgentSession` / `Conversation` 记录的是概念模型，不是待建的表——`Agent` 的职责由 `Task` 承担（Task 才是用户可见单位），`AgentSession` / `Conversation` 的职责由 `Run` + 运行游标（`RunCursor`）承担。

**独立 `Artifact` 与 `Checkpoint` 表的原规划属 v2**：内容寻址和上下文 spills 的现状见 §2.3，不与独立表或完整原文保留混称。网络阶段的内容引用与有权分页读取按 §5.2.3 增量交付，不必等待一套通用产物表。`Checkpoint` 在 v1 的等价物是「事件日志 + 游标」（`run.cursor` 事件 + `RunCursor`），无需另建第二个事实源。

**`Memory` 也不是第五张事实表**：它由事件日志（`tool.intent` / `tool.result` + 审批事件）**确定性投影**出来，删掉投影可重建（[F10-memory-design.md](F10-memory-design.md) §3.1）。三个字段要读对：`evidence[]` 是「支撑这条信念的源记忆 + 原文引文」，`proof_count` 是证据条数（L2 必填，机械可算、可核对），`consolidated_at` 是上次蒸馏 / 合并时间（freshness 判据：作用域内有新记忆尚未合并时，该层视为陈旧）。`source_refs[]` 是「每条记忆必须能指回来源」的锚点。字段语义见 [F10-memory-design.md](F10-memory-design.md) §5，**实现状态以该文 §13 的契约增量清单为准**——别把这张表的字段当成已经写进库的列。

**v1.2 新增两个字段**：`erased_at?` **已落库**（N4）——**物理擦除**时间戳，与 `status=forgotten` 分开：`forgotten` = 停止召回但正文仍在本机，`erased_at` = 正文副本与检索索引已删；`layer` 仍是**契约、未落库**（N3）：资产层 `l1` 原子事实 / `l2` 带证据的信念 / `l3` 偏好页面（L2/L3 是可重建的合成层）。语义与边界见 [F10-memory-design.md](F10-memory-design.md) §5 / §10，工具入口见 §5.2.2。

### 4.2 Task 与 Run 必须分离

**Task 是用户想完成的事；Run 是为完成它的一次执行。** 一个 Task 多个 Run，失败不覆盖、重试留痕。

```
Task: 整理 ~/Reports 下的 PDF
 ├─ Run 1 … 失败：权限不足（保留）
 └─ Run 2 … 成功（不覆盖 Run 1）
```

### 4.3 Run 状态机（按执行模型分叉，含目标变更 fencing）

**上一版把 `CANCELLED` 当普通终点，处理不了"用户中途改主意"，而且会让"没产生副作用"和"已改了一半"看起来一样。** 本版修正：

```
CREATED → QUEUED → RUNNING ─┬→ WAITING_APPROVAL → RUNNING
                            ├→ WAITING_USER      → RUNNING
                            ├→ PAUSED            → RUNNING     # 仅 E1/E3，安全点暂停
                            ├→ RECONCILING       → SUCCEEDED|FAILED
                            ├→ CANCELLING        → CANCELLED
                            └→ SUCCEEDED | FAILED
```

**`WAITING_APPROVAL` 只是"在等人对某个动作做决定"，不是"一直在等人批准"。**
人否决一个动作之后，这个动作在同一 Run 内即被否决（§5.4 的 `c4.approval_rejected`），
Run **不会**再为它回到 `WAITING_APPROVAL`：resume 后要么由模型改参数/改目标
重新发起（那会是一张新的、可以再批的审批），要么以 `FAILED` 收束且
`effect_state` 如实。把"被否决"实现成"再问一次"会让 Run 永远收不了尾，也让
用户以为自己的决定没有生效。

**终态必须两个维度，不是一个枚举：**

```python
outcome      = succeeded | failed | cancelled
effect_state = none | applied | partial | unknown     # 复用 C2 的语义
```

**目标变更（用户改主意）用 fencing，不用"覆盖"：**

| 对象 | 字段 | 规则 |
|---|---|---|
| `Task` | `goal_revision` | 用户新指令追加为带序事件，**不覆盖旧目标** |
| `Task` | `active_run_id` | 只有一个 Run 是 active |
| `Task` | `workspace_lease_epoch` | 每次目标变更 +1 |
| `Run` | 绑定不可变的 `goal_revision + lease_epoch` | 两者任一与 Task 当前值不符，即视为被 fence |

fencing 触发时：旧 Run 的审批、grant、排队中的 intent **立即失效**，Run 进入 `CANCELLING`。新目标创建**新的 Run**，不改写旧 Run 的记录。

- 正在执行的副作用允许进入 `RECONCILING`（对账中）
- **取消只保证"不再派发新副作用"，不保证撤销已经发生的那一次**
- 旧 Run 仍有 `partial/unknown` 副作用或未释放工作区租约时，**新 Run 不得写同一工作区**，只能排队或只读检查

**暂停 = 安全点暂停**：先阻止新 intent，当前工具执行完才进入 `PAUSED`。不假装模型流和工具调用能瞬时冻结。

**控制请求必须与生效状态分开持久化。** 用户按下暂停的那一刻，Run 可能正处在一次文件写入中间；把 `control_requested` 直接写成 `state=PAUSED`，等于对外宣称"已经停稳了"，而实际上还有一个副作用在飞。所以 Run 的投影里有两个字段：`control_requested ∈ {pause, cancel}` 记录"人想干什么"，`state` 记录"现在到底在哪"。执行器只在安全点读取请求并确认成状态，且确认与清除请求是同一次提交——否则恢复后会把同一条请求再执行一遍，刚 resume 又立刻停下。

**`PAUSED` 不属于 E2。** v2 的生成作业只有 `QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED`——暂停一个 5 分钟的生成任务没有意义，GPU 仍在计费。

**UI 必须显示**「已停止，但部分修改已生效」，不能只显示"已取消"。

验收必须覆盖三种时点改目标：等待审批时 / intent 已写但未派发 / 副作用执行中。


### 4.4 三个结果类型不可合并

```python
LoopResult   { turn_id, content[], stop_reason: StopReason, usage }
EffectResult { call_id, status, effect_state: none|applied|partial|unknown,
               receipt_id, evidence[] }
JobResult    { job_id, state: JobState, queue_pos?, artifact_refs[],
               provider_cost, error? }          # v1 仅定义，不产生
```

### 4.5 「文件夹」与「任务」不是二选一（D6 定稿）

它们**不是同一层的东西**，这是最容易被做错的地方：

| 概念 | 是什么 | 负责什么 |
|---|---|---|
| **Task** | 用户视角的最小单位 | 意图、生命周期、重试、恢复 |
| **Workspace**（文件夹） | 任务的属性，0..n 个 | 权限边界、快照与回滚边界 |

所以：**Task 是用户可见的最小单位；Workspace 是权限与回滚的边界。** 两者是 0..n 关系。

**为什么不能以文件夹为最小单位：**

- 大量任务没有文件夹：「研究一下 RAG 和 GraphRAG 的差别」「帮我把这周日程排一下」。强制先选目录 = 把 onboarding 卡在第一步
- 一个任务跨多个目录就崩了（读 A 写 B）
- 会话绑死目录，用户换个目录就像"换了个人"，记忆归属混乱
- 普通用户的心智是「我要做什么」，不是「我要在哪个目录做什么」

**为什么不能只有任务：**

- 权限无从落地。「整理我的报告」要动哪个目录？没有 Workspace 抽象，grant 就没有绑定对象
- 快照与回滚需要稳定边界（§8.4 的"每个工作区最近 10 个 Run"依赖它）
- 两个任务同时改同一批文件时，需要 workspace 租约互斥（§4.3 的 `workspace_lease_epoch`）

**落地形态：按需索取权限，不预先选文件夹。**

```
新建任务
 ├─ 输入目标（一句话）
 ├─ 需要文件吗？
 │   ├─ 需要 → 选目录（可跳过，等真用到时再问）
 │   └─ 不需要 → 直接开跑（Task.workspaces = []）
 └─ 执行中若越界访问 → 走 §8.3 的「目录外写入需审批」
```

任务卡片上显示：`作用范围：~/Documents/Invoices`｜`已停止，部分修改已生效`｜`可撤销`。

**对现有设计的影响**：`Task` 增加 `workspaces[]`；没有 workspace 的 Task 是合法的（纯对话/研究类）；`goal_revision` 变更时 workspace 集合也可变，同样走 fencing。

### 4.5.1 D6 落地后的精确形态（M2 实现契约）

D6 只定了概念，下面这些是实现方必须一致的具体形状，改它们要走 `docs(contract):`。

```python
WorkspaceBinding { root_path: str, mode: "readonly"|"writable", snapshot_policy: "none"|"on_write" }

Task  { task_id, goal, goal_revision, lease_epoch, active_run_id, status,
        workspaces: WorkspaceBinding[] }        # [] 合法，且不产生任何快照
Run   { run_id, task_id, goal_revision, lease_epoch, state, outcome?, effect_state? }
```

**Task/Run 的状态是事件投影，不是事实源。** 每条状态变更与对应事件在同一次 SQLite 提交：

| 事件 | 何时 | 关键负载 |
|---|---|---|
| `task.created` | 新建任务 | `goal, goal_revision, lease_epoch, workspaces` |
| `task.revised` | 用户改目标 | 新的 `goal, goal_revision, lease_epoch` |
| `run.start` | 开一次执行 | 绑定的 `goal_revision, lease_epoch` |
| `run.finish` | 收尾 | `outcome, effect_state` |
| `run.fenced` | 因改目标/租约变更而失效 | `reason` |

投影表可以随时删掉重建（`rebuild_from_events()`）；回放必须能解释"这个 Run 为什么被 fence"，
而不是只能看到一个覆盖过的最新目标。Task 级事件不属于某个 Run，`run_id` 为空串，靠 `task_id` 归属。

**资源租约的身份口径（M2 起）**：

- 互斥对象是**目录树**，不是目录：`(st_dev, st_ino)` 判断"是不是同一个目录"，
  规范路径前缀判断"是不是同一棵子树"。父子目录、符号链接别名一律视为同一资源。
- 持有者身份是**进程实例**（`instance_id` + `pid` + 启动时间），不是裸 pid。
  pid 会被复用；同一个 `run_id` 也不代表同一个执行者。
- 默认 TTL 30s、心跳 5s。收尾与被 fence 时必须立即释放，否则下一个 Run 会被自己的残留租约挡住。
- TTL 到期**不证明**旧 worker 已停止。接管必须先取得本机执行守卫（`execution_guard`，
  POSIX `flock` / Windows `msvcrt.locking`）、核实 owner，并核对未决副作用。

---

## 5. 契约（五道，版本化）

| ID | 名称 | v1 状态 |
|---|---|---|
| C1 | Model I/O | **原则已定**｜待补字段级可校验 schema 后冻结 |
| C2 | Tool | **原则已定**｜待补字段级可校验 schema 后冻结 |
| C3 | Event | 已定义｜随 §5.3 定稿冻结 |
| C4 | Permission / Approval | 已定义｜随 §5.4 定稿冻结 |
| C5 | Eval | 原则已定 |

**冻结条件（不是"写完了"就算冻结）**：字段级 JSON Schema 可校验 + 契约测试全绿 + 第二家 provider 接入验证过。缺任一条只能标"原则已定"——提前宣布冻结会让实现方误以为边界不会变。

**内部契约版本策略**：内部通信按双方声明的版本区间取交集最高版；每条消息与持久化事件必须能够定位其精确契约版本。历史回放使用记录版本，不重新协商。此条是内部 C1–C5 的目标要求，未落地部分见 §5.3.4 / §5.3.5；**不是 MCP 等外部协议的握手规则**，外部 adapter 按锁定版本的实际协议与兼容矩阵实现。

### 5.1 C1 Model I/O

核心对象：`ModelRequest / Message / ContentBlock / ToolCallProposal / ModelTurn / Usage / ContinuationRef / StreamEvent / ModelError`。

必须遵守：

- `Usage` 未知为 `null`，不用 `0`；标 `snapshot`/`delta`，累计值不得逐片相加
- `ContinuationRef` 的签名/thinking 按 opaque 原样往返，不解析、不伪造，只在兼容作用域内续接
- **`finish_reason`（模型侧）与 `run_stop_reason`（运行侧）是两个字段，禁止混用**——见 §5.5
- **派发工具的条件是三元组，不是单个枚举值**：`finish_reason == tool_use` **且** 整组参数完整 **且** 通过固定版本 C2 schema 校验 **且** assistant turn 已持久化。任一项不满足即不派发（含"参数是合法 JSON 但被截断"的情况）

**M3 补充：C1 吃的是一种"内部规范化消息形状"**

M3 加第二个 provider（Anthropic Messages API）时才被迫写清这件事。此前
`Model.complete(messages, tools)` 收的是 OpenAI 形状的 dict，没人写过它算不算
契约的一部分——而它其实是：这份 dict 会被 `save_cursor` **持久化进游标**、
被 `messages_hash` 盖进事件、被 `replay` 用来重建消息序列。换句话说，
"换模型不用改主循环"的真正前提是：**主循环只说一种中立形状，adapter 负责翻译**。

| role | 字段 | 说明 |
|---|---|---|
| `system` | `content` | 提示词（Anthropic 要求提到顶层，由 adapter 提出去） |
| `user` | `content` | 用户输入 |
| `assistant` | `content`, `tool_calls[{id, type, function{name, arguments}}]` | `arguments` 是 **JSON 字符串** |
| `tool` | `tool_call_id`, `name`, `content` | 工具结果回灌（Anthropic 要求包成 `tool_result` 并合并进同一条 user 消息） |

三条规则：

1. adapter 做 **内部形状 ↔ 各家线上协议** 的双向翻译；**不得反向改写内部形状**
   （改了，replay 重建出来的序列就和当时发出去的不一致，`messages_hash` 立刻对不上）
2. 事件里的 `tool_calls[].arguments` 是解析后的对象；解析失败时保留原始字符串，
   执行侧据此拒绝派发——adapter 不许"修好"半截 JSON
3. 新 provider 若需要"三张表之外"的语义进内部形状，那是 C1 契约变更，
   必须走单独的 `docs(contract):` 提交

**`ModelError` 的落地口径（M3）**

- 传输、超时、HTTP 状态、响应体不合协议，一律**抛 `ModelError`**，
  **禁止**编码成 `finish_reason`
- 字段：`kind ∈ {transport, timeout, http_status, auth, rate_limit, protocol}`、
  `status_code`、`retryable`（**建议**，不是保证——429 的重试仍可能连续失败）
- `finish_reason` 的 13 值封闭集由 `ModelResponse` 构造时强制校验；
  线上语料里不认识的结束原因（含缺失）抛 `ModelError(kind=protocol)`，
  **绝不兜底成 `end_turn`**——那会把一次异常收尾记成成功
- `usage`：缺字段是 `null`；`cache_read_input_tokens` / `cache_creation_input_tokens`
  只在 provider 上报时出现，且**不并入** `input_tokens`（否则同一批 token 计两次费）


### 5.2 C2 Tool

```python
ToolDefinition {
  contract_version, tool_id, version, description,
  input_schema, schema_hash,          # schema_hash 覆盖整个规范化 schema
  side_effect_level,                  # pure | idempotent | non_idempotent
  exec_model,                         # E1 | E3（E2 预留）
  default_timeout_ms, max_timeout_ms, cancellation,
  output_schema?, idempotency?, reconciliation?
}
```

- 固定执行顺序：完整参数 → schema 校验 → RFC 8785 canonical JSON 算 `args_hash` → 执行侧核验 C4 → 持久化 intent → 派发 → 持久化 result
- `call_id` 稳定（逻辑调用），`attempt_id` 每次派发变化
- `idempotent` 必须由**执行端**持久去重保证；客户端生成的 key 不构成保障
- `MCP` 的 `idempotentHint / readOnlyHint` 只是提示，不参与授权或幂等
- JSON-RPC 协议错误 与 `isError=true` 执行错误分开映射

#### 5.2.1 记忆工具（F10，v1.1）

记忆操作是 C2 工具，不是新机制（设计见 [F10-memory-design.md](F10-memory-design.md) §3.2）。**七个模型可见**工具与文件工具同级登记在 `src/tianshu/tools.py` 的 `SPECS`（记忆段），`tool_manifest()` 把它们一起投影给模型；另有三个控制面专属、`exposed=False` 的 `memory.remember` / `memory.promote` / `memory.erase`（见 §5.2.2），模型清单里没有它们。

| tool_id | version | `side_effect_level` | `exec_model` | C4 动作 | 审批 | 走哪条执行路径 |
|---|---|---|---|---|---|---|
| `memory.search` | `1.0` | `pure` | E1 | `read` | 不需要 | 主循环只读分支，**不经派发**（`loop.AgentLoop._exec_memory_read`） |
| `memory.get` | `1.0` | `pure` | E1 | `read` | 不需要 | 同上 |
| `memory.propose` | `2.0` | `non_idempotent` | E3 | `create` | 不需要（候选是惰性草稿） | dispatcher |
| `memory.confirm` | `1.0` | `non_idempotent` | E3 | `overwrite` | 需要（`origin=user_explicit` 除外，见下方注） | dispatcher |
| `memory.forget` | `1.0` | `non_idempotent` | E3 | `delete` | 需要（等同删除） | dispatcher |
| `memory.edit` | `1.0` | `non_idempotent` | E3 | `overwrite` | 需要（覆盖类） | dispatcher |
| `memory.consolidate` | `1.0` | `non_idempotent` | E3 | `create` | 不需要（只写可重建投影，结果仍是候选） | dispatcher |

> **注（N1 之后）**：`origin` 的两档现在都有产生入口——`user_explicit` 由控制面 `memory.remember` 创建（人敲命令就是签发者），`user_confirmed` 由"人批准了 `memory.confirm`"在执行侧升级（`tests/test_memory_dispatch.py::test_human_approval_upgrades_origin_to_user_confirmed`）。模型发起的 propose 仍固定写 `inferred`（防模型自称"用户明示"绕过审批）。**免审批的 user_explicit 候选确认**这条策略分支仍没有生产入口：`memory.remember` 直接写 active，不产生候选。别把这段读成"用户说一句记住就自动激活一条已有候选"。

- `side_effect_level` / `exec_model` / `action` 是 `ToolSpec` 的字段（`tools.ToolSpec`）；`version` 进 `ActionRequest.tool_version`、`input_schema` 的规范化摘要进 `schema_hash`（`dispatcher.build_request`）。两者都被审批摘要绑住（§5.4）：**schema 变化即新工具**（红线 8），不自动升级。
- 映射到 C4 动作的口径：`propose`→`create`、`confirm`→`overwrite`、`forget`→`delete`（`SPECS` 里的 `action=`）。所以 `confirm` / `forget` 天然落到 `permissions.PolicyEngine.classify` 的 overwrite / delete 分支，与"覆盖、删除需审批"（红线 6）同一口径；`propose` 落在 `AUTO_ACTIONS`（`permissions.AUTO_ACTIONS`）。
- **模型可见的七个 `ToolSpec` 一律没有 `path_arg`**（`SPECS` 记忆段前的注释写明理由）。记忆没有文件路径，主循环不能拿它去 `Workspace.resolve()` 一个 URI；目标由派发请求的 `resources` 携带，形状是 `memory://<scope>/<scope_id>`（[F10-memory-design.md](F10-memory-design.md) §6.1）。"给记忆编一个假路径"是被明确拒绝的形状——那会让删掉投影等于删掉用户文件。
- **`input_schema` 原样**（`additionalProperties: false`）：

```text
memory.search      { query: string, limit?: integer (1..5) }         required: query
memory.get         { memory_id: string }                            required: memory_id
memory.propose     { kind: string, content: string, scope?: string,
                     scope_id?: string, source_refs?: string[],
                     evidence?: object[], expires_at?: integer }    required: kind, content
memory.confirm     { memory_id: string, revision: integer }          required: memory_id, revision
memory.forget      { memory_id: string, revision: integer,
                     status?: string }                              required: memory_id, revision
memory.edit        { memory_id: string, revision: integer, content: string }
                                              required: memory_id, revision, content
memory.consolidate { scope: string, scope_id: string }              required: scope, scope_id
```

`kind` / `status` 的取值域在这些 schema 里是 `string`，不是 `enum`——封闭集在存储侧校验（`memory.MEMORY_KINDS` / `MEMORY_STATUSES` / `MEMORY_ORIGINS`；校验点 `memory.MemoryProjection._apply_propose`）。写错的值会让 `tool.result` 落一条失败，而不是被静默接受。

- **首版不新增事件族。** 记忆生命周期由 `tool.intent` / `tool.result` + 审批事件承载，投影据此重建：`memory.MemoryProjection.apply_event()` 只认 `tool.result`、且 `tool_id ∈ {memory.propose, memory.remember, memory.confirm, memory.forget, memory.edit, memory.promote, memory.erase, memory.consolidate}`（`memory._WRITE_TOOL_IDS`）、`status == "succeeded"` 的事件；`memory.search` / `memory.get` 的 result 即便落库也被忽略（读不改变投影）。`memory.MemoryProjection.rebuild()` 清空投影后按 seq 重放同一批事件，**重放也走 `apply_event()`**，所以实时写入与重建不可能走岔。新增 `memory.*` 事件会与 `tool.intent`/`tool.result` 形成同一因果的第二份指针（§5.3.3 已为 `parent_seq` 立过这条规矩）。
- 读路径不经 dispatcher：`memory.search` / `memory.get` 在 `_exec_read` 里于**任何目录解析之前**分流（`loop.AgentLoop._exec_read`），结果记 `none_applied`（纯读，与文件只读工具同口径），零目录任务也能检索。写路径经 dispatcher，因此自动获得 intent/result 与完整审计。
- **逐项落地状态**（哪一条已接线、哪一条还只是契约）不在本节重复，见 [F10-memory-design.md](F10-memory-design.md) §13。

#### 5.2.2 记忆工具契约增量（v1.2，已由 N1 / N3 / N4 落地）

本节是**契约定义**；其中的工具与新版本已按红线 8（schema 变化即新工具，旧审批与旧 grant
自动失效、不自动升级）在 N1（`propose` 2.0 / `edit` / `remember` / `promote`）、N4
（`erase`）、N3（`consolidate`）三个实现 PR 里与产生路径同落地，现全部在 `SPECS` 与
`tool_manifest()`（模型可见的那 7 个）里。逐条实施状态见
[F10-memory-design.md](F10-memory-design.md) §13 与
[SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) §2.3 / §4.4 / §5.4。

**"仅控制面"是可判定的**：`Dispatcher.open_control_run` 创建 task 时在 `task.created`
payload 写 `control: true`，`Task.is_control` 从事件投影读回；只有 `is_control` 的 Run 能调
`memory.remember` / `memory.promote` / `memory.erase`。执行侧再核一次（越权 →
`c4.memory_control_scope_binding`）。这不是"信任调用方"，是把可信来源落成事件事实。

| tool_id | version | C4 动作 | 审批 | 谁能调 | 语义要点 |
|---|---|---|---|---|---|
| `memory.propose` | `2.0`（由 `1.0` 升级） | `create` | 不需要（仍是惰性候选） | 模型 + 控制面 | schema 增 `scope`（`user` / `task`）与 `scope_id`。模型：`task` 的 `scope_id` 必须是**本 Run** 的 `task_id`；`workspace` 不从模型进入（共享边界由人签发）。控制面：可写 `memory_scopes` 声明的旧 task 作用域。 |
| `memory.edit` | `1.0` | `overwrite` | 需要（覆盖类） | 模型 + 控制面 | args `memory_id` / `revision` / `content`。**两行形状写死**：新行是**新 `memory_id`**、`revision=1`、`status=active`、`supersedes=旧 id`；旧行 `status=superseded`、`revision` 不变、仍可按 id 查到（"旧版可查"就是这条）。审批绑定 `preconditions.before_revision`，执行侧从权威投影再核一次（不一致 → `dispatch.memory_stale_revision`）。**不允许改 `kind`**：改 kind 等于换一条记忆。 |
| `memory.remember` | `1.0` | `create` | 不需要（人本人就是签发者） | **仅控制面**（`exposed=False`） | 创建即 `status=active`、`origin=user_explicit`；模型清单里没有这个工具。 |
| `memory.promote` | `1.0` | `overwrite` | 需要（共享是显式动作） | **仅控制面**（`exposed=False`） | **移动语义**：原行立即 `superseded`（原作用域不再召回，避免双份真相），目标作用域新行 `candidate`（人 `confirm` 后才召回）。一次审批；Receipt 针对**原行**（`root`=原 scope URI、`resource`=原 `memory_id`、写前/写后用稳定状态串）。新行 `source_refs` **必须包含原 `memory_id`**（执行侧机械校验）。 |
| `memory.consolidate` | `1.0` | `create` | 不需要（只写可重建投影） | 模型 + 控制面 | 触发 L2/L3 合成（确定性合成，见 F10 §4）。作用域限制与 `propose` 相同：模型只能 `user` 与本 Run 的 `task`；`workspace` 的合成结果只能由人 `memory.promote`。结果 `candidate`；人 `confirm` 后进召回。**freshness 的基准不是 `consolidated_at`**：`confirm` 这条「生命周期推进」会把该列改成确认时刻（L1 也如此），拿它当基准会让页在晚确认后"永远新鲜"；判据落在 `source_refs` 里的 `seq:<basis_seq>`（合成那一刻看到的最大逻辑时钟），见 F10 §8 / §13 #19。 |
| `memory.erase` | `1.0` | `delete` | 需要（等同删除，另加 Receipt） | **仅控制面**（`exposed=False`） | 删除正文副本与检索索引；投影行置 `forgotten` + `erased_at`。**引用计数**：`content_ref` 是 `sha256` 内容寻址，不同记忆可能共享同一 blob——只有**没有任何其它投影行引用**该 `content_ref` 时才物理删文件，否则保留文件、只把这个行标 `content_state=unavailable`。擦除必须产出可独立核对的 Receipt（删了哪些 `content_ref`、多少字节；被别的行引用而未删的也列出）。**事件日志里的读快照保留**——边界见 F10 §10。 |

```text
memory.propose     { kind: string, content: string, scope?: string, scope_id?: string,
                     source_refs?: string[], evidence?: object[],
                     expires_at?: integer }                        required: kind, content
memory.edit        { memory_id: string, revision: integer, content: string }
                                                        required: memory_id, revision, content
memory.remember    { kind: string, content: string, source_refs?: string[],
                     expires_at?: integer }                        required: kind, content
memory.promote     { memory_id: string, revision: integer,
                     scope: string, scope_id: string }             required: memory_id, revision, scope, scope_id
memory.consolidate { scope: string, scope_id: string }             required: scope, scope_id
memory.erase       { memory_id: string, revision: integer }        required: memory_id, revision
```

三条共用规则：

- **不新增事件族**：新工具仍走 `tool.intent` / `tool.result` + 审批事件；`memory.edit` /
  `memory.promote` / `memory.remember` 各多一种 `tool.result` 结果形状（`apply_event` 据此走
  对应分支），事件类型不变。控制 Run 的 `control` 与 `memory_scopes` 记在 `task.created`
  payload——payload 形状尚未冻结（§5.3），**加键不加类型**。
- **`exposed=False` 的边界是两道，不是一道**：`tool_manifest()` 只投影 `exposed=True`；
  模型点名清单外的工具走 `tool.not_dispatched`，不进派发。三个控制面工具各钉一条测试
  （先例 `restore_file`）。
- **`origin` 不由模型参数决定**：`user_explicit` 只能由 `memory.remember` 产生；
  `user_confirmed` 只能由"人批准了 `memory.confirm`"这件事产生——执行侧读审批记录，并把
  `inferred` **升级**为 `user_confirmed`（只升级、不降级）。schema 里没有 `origin` 字段，
  `additionalProperties: false` 保持。

**落地状态（N1 / N3 / N4，2026-11-11）**：`memory.propose` 已升 `2.0`；`memory.edit` / `memory.remember` / `memory.promote` / `memory.erase` / `memory.consolidate` 全部落地（含控制面判据、一次性审批、旧版可查、擦除引用计数、确定性 L2/L3 合成与 freshness、Receipt 与 CLI）——**已落地 10 个**（**7 个模型可见** + 3 个控制面专属）。逐条实现状态见 [F10-memory-design.md](F10-memory-design.md) §13。

**`origin` 的口径（N1 细化）**：它描述的是**这一版正文**的来源，不是记忆的终身属性——控制面 `memory.edit`（人自己写的新正文）→ `user_explicit`；模型写、人批准 → `user_confirmed`；`memory.promote` 搬的是同一段正文，来源**随行**（旧行 `user_explicit` → 新行仍 `user_explicit`，不降档）。旧版的 `origin` 留在旧行，不被改写。

#### 5.2.3 通用工具准入（新增要求；网络 / MCP 接入前完成）

**当前缺口**：内置只读文件工具仍走独立读取路径，`ToolSpec` / `AdmittedCall` 偏文件资源，参数校验仅覆盖少量顶层规则。本节规定演进目标，不表示通用工具契约已经实现或冻结。

- 所有工具进入同一套注册、参数校验、任务状态、权限、预算与留痕路径，再分派到文件 / 网络 / MCP / 记忆 / 委派执行器；复用现有 dispatcher，不另建授权系统。统一准入不意味着所有读取都要人工批准或持有文件写租约。
- 资源使用带种类的描述：文件、网络目标、MCP 工具、记忆作用域、子任务各自规范化与校验。无 workspace 的研究任务合法，URL 不伪装成文件路径；多个目录必须明确目标绑定。具体字段与新增枚举通过 C2/C4 schema 和契约测试后冻结。
- **状态变化、数据外发、计费、可重试性分开表达**。网络 GET / 搜索不是无成本、无外发的本地纯读；网页、工具输出、记忆和子任务结果均保留来源，不能提升为用户授权。
- 执行侧验证所声明的完整 schema，包括嵌套对象、数组和约束；不支持的 dialect / 关键字拒绝准入，不忽略，不自动联网解析外部引用。任务绑定固定工具版本、schema 与描述摘要；动态发现不等于获得调用权限，更新不得热替换既有批准。
- 执行上下文由系统提供 task/run/call/attempt、deadline、取消状态和预算账户，模型不可自行指定这些权限身份。大结果以有界摘要 + 内容引用返回，分页读取引用同样验证任务可见范围。

验收：无目录联网任务可运行；嵌套非法参数在执行前被拒；私有内容未经外发许可不发包；工具描述变更使旧批准失效；取消或额度耗尽后无新请求；replay 只读已保存结果、不访问当前网络或工具清单。

**实现状态（2026-11-11；准入 PR #91、资源种类 PR #97）**：

- **已实现并有判据**（`tests/test_g1_admission.py` 19 条，负控 9/9，见
  [tool-admission.md](tool-admission.md)）：封闭 schema 方言与注册期拒绝、执行期嵌套校验、
  准入前移到派发之前、工具版本 + schema + **描述摘要**三项绑定、身份字段（task/run/call/
  attempt/预算/deadline）注册期与执行期双闸、URL 不伪装成文件路径、replay 只读事件。
- **已实现并有判据**（`tests/test_g1d_resource_kinds.py` 15 条，负控 6/6，见
  [resource-kinds.md](resource-kinds.md)）：五种资源种类（file / memory / network / mcp /
  subtask）各自的规范化与校验；外发恒需人工批准（`fetch` + `c4.outbound`，无自动路径）、
  MCP 与 exec 同级（`c4.mcp_call`）、种类混用一律拒；没有执行器的种类在派发前拒绝
  （`dispatch.resource_kind_unsupported`）。**委派在 G2 子代理 D1 接上派发分支后不再恒拒**：
  `subtask.spawn` 需人工批准、`subtask.cancel` 自动放行、`status`/`result` 自动，
  `c4.subtask_unavailable` 退役为保留未使用（见 §5.4.1）。**D2 起子 Run 由既有
  Worker 执行**：父 Run 在还有未完成子任务时挂起作业、子任务收尾的同一事务里唤醒；
  父取消传播、父改目标 fence、结果以有界摘要 + `content://` 引用回填（见
  [SPEC-G2-DELEGATION-2026-11-11.md](SPEC-G2-DELEGATION-2026-11-11.md) §3）。
  **D3 起真实用户任务 `RU-D1@1.0.0` 在脚本模型下真的派一个只读子任务、汇总并引用
  子任务结果**；评测同时钉住只读边界（写/外发被拒）与「子任务失败时不编造」。
  它是**评测证据**，不是新能力：子任务仍只读、仍不能写文件/出网。
- **已实现并有判据**（`tests/test_content_refs.py` 12 条 + `tests/test_content_read.py`
  5 条，负控 12/12，见 [content-refs.md](content-refs.md)）：「大结果以有界摘要 +
  内容引用返回，分页读取引用验证任务可见范围」。占位给出可用的
  `content://sha256:<hex>` 引用；`read_content` 分页读取（`offset` / `length` /
  `next_offset`，单页上限 64 KiB）；可见范围按 `(digest, task_id)` 授权，跨任务读取
  被拒（存储层与执行层各自核一次）；读取时**重算 sha256** 比对引用；分页不切半个
  UTF-8 字符。读工具沿用既有的「只读动作不过派发」口径（`read_text` / `memory.search`
  同路），差异见该文档 §4。
- **已实现并有判据（G2 网络阶段）**：网络执行器 `network.fetch`（18 + 23 条，负控
  5 + 8，见 [network-g2.md](network-g2.md)）：任务级外发白名单（`task.created` 的
  `outbound_allow`，未声明即一条包都不发）、逐次人工批准、GET 抓取、同源重定向、
  响应字节上限与硬超时、有界摘要 + `content://` 引用、字节账本预占/结算、取消后无
  新请求、回放不联网。因此「无目录联网任务可运行」与「私有内容未经外发许可不发包」
  在**执行面**有了可核对的事实（请求里只有目标 URL 与固定头，没有 Cookie/凭据/代理）。
  仍然不成立的部分逐条登记在 [network-g2.md](network-g2.md) §7：只支持 GET、没有检索
  API、真实公网研究与真实模型复跑未验证、DNS rebinding 未防护、二进制正文没有文本
  分页读取。
- **已实现并有判据（G2 子代理 D1）**：委派的**排队面**——`task.created.max_subtasks`
  （必填能力声明）、`subtasks` 实体与父子链、四个工具（`subtask.spawn/status/result/cancel`）、
  六道授权门（工具归属 / 能力声明 / 深度 / slug 唯一 / 数量与局部上限 / 目标存在）、
  `subtasks` 维度真实预占与子账户父链、崩溃恢复按证据补结果。测试与负控见
  `tests/test_g2_delegation_*.py` 与 PR body。
- **已实现并有判据（G2 子代理 D2）**：委派的**执行与收束**——子 Run 由既有 Worker
  驱动、父 Run 挂起/唤醒、父取消传播、父改目标 fence（`dispatch.parent_fenced`）、
  结果以有界摘要 + `content://` 引用回填、回放形状校验、`subtask.cancel` 在执行侧
  安全点确认、启动恢复补 wake。判据 D2-c1…D2-c10 与负控见
  [SPEC-G2-DELEGATION-2026-11-11.md](SPEC-G2-DELEGATION-2026-11-11.md) §3/§6。
- **已实现并有判据（G2 MCP 阶段）**：MCP stdio 客户端 + 动态工具准入 + 派发链路
  （PR #116–#119，见 [mcp-g2.md](mcp-g2.md)）。真实第三方 server 未验证、进程无沙箱，
  隔离门禁未覆盖（`c4.mcp_isolation_unproven` 明确拒绝声称隔离）。


### 5.3 C3 Event

```
Event {
  seq          uint64       # 单调递增，主键，不可跳号
  run_id, task_id?
  type         string       # 必须是 §5.3.2 注册表里的具体类型（封闭集合）
  ts           int64        # 墙钟毫秒（"单调时钟 + 墙钟双写"尚未实现，见 §5.3.4）
  actor        {kind, id}   # core|harness|tool|human|system
  payload      object|ref   # >256KiB 落盘，此处留 ref；每种类型的字段形状尚未冻结
  payload_hash sha256       # canonical_bytes(payload) 的 sha256
  parent_seq   uint64?      # 保留字段：当前不写，新写入恒为 null（见 §5.3.3）
  idem_key     string?
}
```

**存储形态：SQLite（WAL）中的 append-only `Event` 表作为唯一提交点。**（本版修正了上一版"抄 OpenHands 一事件一文件"的决定）

修正理由：一事件一文件 + "主动上报丢文件空洞"与红线"`seq` 不可跳号"直接冲突；再叠加 `approval.consumed` 靠 SQLite 唯一约束，崩溃时会出现**两个互相矛盾的事实源**。桌面单用户场景不需要文件日志的并发优势。

规则：

- 单个写事务内**同时**提交：`event.seq`、run fencing（`goal_revision + lease_epoch`）、`approval.consumed`、`grant`、`tool intent`。禁止跨库/跨文件各提交一次。
- **`type` 必须是 §5.3.2 注册表里的具体类型。** `EventLog.append()` 在产生任何副作用之前校验，未登记的类型抛 `TianshuError`（信息带上传入的类型名），不写行、不落 blob。注册表是写入边界，不是建议：为了"先跑通"临时加类型，等于把契约改回"代码写什么就是什么"。
- `seq` 由单 writer 单调分配，无跳号、无空洞
- 表上禁 `UPDATE / DELETE`（用触发器强制）；订正靠追加新事件
- payload > 256 KiB 落内容寻址文件，表内只留 `payload_ref + payload_hash`
- 文件形态日志仍可导出，用于人工排查与外部审计，**但不参与恢复裁决**

#### 5.3.1 为什么把 9 值抽象枚举换成"族 + 具体类型注册表"

**原方案**：`type` 是一个 9 值抽象枚举 `intent|result|model_call|approval|receipt|compaction|job|checkpoint|audit`。

**新方案**：两层——**族**（`type` 的名字空间前缀；14 个在用 + 3 个声明未实现）→ **具体类型注册表**（43 个，机器可校验，写入侧强制）。

**为什么改**（三条都是实测出来的）：

1. **抽象枚举根本无法机器校验。** 代码里 `type` 一直是自由文本：拼错的类型名会原样进库，`seq` 与 `payload_hash` 只能证明"记录没被改过"，证明不了"记录的类型是契约承认的类型"。这次补契约时，一份机械扫描出的"全部事件类型"清单里就混进了 21 个 `dispatch.*` **规则 id**（`dispatch.lease_lost` 等）和 1 个锁文件名（`execution.guard`），还漏掉了真实存在的 `receipt`——没有注册表时，"有哪些类型"这个问题只能靠猜。
2. **抽象枚举不反映真实类型，也没有映射层。** 真实落库的有 41 种具体类型，抽象值只有 9 个，两者之间没有任何映射；`task`、`run`、`tool`、`model`、`dispatch`、`grant`、`policy`、`recovery`、`execution`、`undo` 这些名字空间在抽象枚举里连位置都没有，而 `model_call` 实际叫 `model.*`。
3. **3 个抽象值从未实现。** `compaction` / `checkpoint` / `audit` 作为**事件类型**在 `src/` 里零命中（`model.py` 里的 `checkpoint` 是 PRD §5.5 的模型结束原因，与事件无关；`compaction` 与 `audit` 全库零命中）。把它们留在"枚举"里，读者会以为实现了；写进注册表又会让校验替不存在的类型作证。所以它们单独列在 §5.3.2 的"未实现的族"，并在代码里登记为 `UNIMPLEMENTED_EVENT_FAMILIES`，让"未实现"这件事可被机器读到。

**旧 9 值去哪了**（避免"以前写进契约的东西凭空消失"）：

| 旧抽象值 | 新落点 |
|---|---|
| `intent` | 各族的 `*.intent`：`tool.intent`、`model.intent` |
| `result` | 各族的 `*.result`：`tool.result`、`model.result` |
| `model_call` | 族 `model`：`model.intent` / `model.result` / `model.error` |
| `approval` | 族 `approval`（6 种） |
| `receipt` | 裸类型 `receipt`（历史名字，不改名——改名会让既有事件日志失去可比性） |
| `job` | 族 `job`（5 种） |
| `compaction` / `checkpoint` / `audit` | **未实现**，登记在 `UNIMPLEMENTED_EVENT_FAMILIES` |

#### 5.3.2 注册表：14 个族 / 43 个具体类型

族的定义是**机械的**：`type` 的第一个 `.` 之前就是族名（唯一例外是裸类型 `receipt`）。新增一个前缀等于新增一个族，必须同时改本节、`EVENT_TYPES` 与 `schemas/c3/event.json`。

| 族 | 语义 | 具体类型 |
|---|---|---|
| `approval` | 审批：请求、决定、撤销、兑现、过期、被新审批取代（C4） | `approval.consumed` / `approval.decided` / `approval.expired` / `approval.requested` / `approval.revoked` / `approval.superseded` |
| `dispatch` | 派发：判定通过后的登记、放行、拒绝、撤销（唯一判定点在 Dispatcher） | `dispatch.admitted` / `dispatch.rejected` / `dispatch.requested` / `dispatch.revoked` |
| `execution` | 执行守卫：真正打开文件前建立守卫 | `execution.started` |
| `grant` | 票据：一次性授权的签发与兑现 | `grant.consumed` / `grant.issued` |
| `job` | 作业队列：入队、认领、重排、停放、完成 | `job.claimed` / `job.enqueued` / `job.finished` / `job.parked` / `job.requeued` |
| `model` | 模型调用：意图、结果、错误（C1） | `model.error` / `model.intent` / `model.result` |
| `policy` | 策略拒绝：没有进入派发就被 PolicyEngine 否掉 | `policy.denied` |
| `receipt` | 副作用凭据：与 tool.result 同事务写入（裸类型，历史名字） | `receipt` |
| `recovery` | 崩溃恢复：阻断与解除阻断 | `recovery.blocked` / `recovery.unblocked` |
| `run` | Run 生命周期：开始、状态迁移、控制、游标、模型回合、效果、结束、fencing | `run.control` / `run.cursor` / `run.effect` / `run.end` / `run.fenced` / `run.finish` / `run.model_turn` / `run.start` / `run.state` |
| `subtask` | 子任务：父 Run 侧的委派审计（G2 子代理 D1） | `subtask.finished` / `subtask.spawned` |
| `task` | Task：创建与改目标（goal_revision 推进） | `task.created` / `task.revised` |
| `tool` | 工具调用：intent、result、未派发（C2） | `tool.intent` / `tool.not_dispatched` / `tool.result` |
| `undo` | 撤销：完成与中断 | `undo.completed` / `undo.failed` |

**未实现的族**（声明过、作为事件类型零命中，不参与校验）：

| 族 | 原意 | 状态 |
|---|---|---|
| `compaction` | 上下文压缩检查点 | 事件族未实现；已有确定性卸载 / 裁剪，记录在模型调用的 context 元数据中，不等于已发射 `compaction.*` |
| `checkpoint` | 恢复指针 | 未实现：恢复靠快照 manifest + intent/result 配对，不靠事件检查点 |
| `audit` | 审计事件 | 未实现：审计目前由凭据（`receipt`）与 append-only 本表承担 |

**已定义但无发射点**：**当前为空**。曾经唯一的成员 `job.lost` 已连同常量一起删除（理由见 §2.3 第 4 条）。机制保留在 `events.DECLARED_UNEMITTED_EVENT_TYPES`：将来若有"先定义形状、后接发射点"的类型，登记在那里，硬写会被校验拒绝并提示原因；接上发射点时必须在同一次改动里把它移进 `EVENT_TYPES` + schema `enum` + 本表——校验会先拒绝它，这是刻意的：注册表不能悄悄变长。

#### 5.3.3 `parent_seq`：降级为保留字段

**原方案**：照抄 OpenHands 的事件因果前驱（§8 参考表把"`parent_seq` 因果链语义"列为采纳项）。

**实测**：列存在、`append()` 接受、但**零调用方写入**——真实 run 99/99 条、测试库 34/34 条恒为 NULL。

**新方案（选 b：降级，不硬接）**：列保留、写入恒 `null`、schema 里 `parent_seq` 允许 `integer|null` **但不在 `required` 里**，本节明确"当前不写"。理由：

- 现有的因果边已经能从 payload + `seq` 顺序**唯一**推出来：`tool.intent`/`tool.result` 的 `call_id`、`dispatch.admitted.payload.intent_seq`（真实的 seq 指针）、`receipt.payload.call_id`、`run.state.from/to`、`idem_key`。再写一套 `parent_seq` 就是同一因果的**第二份指针**；两份不一致时没有裁决者，而 §5.3 的规则恰恰是"恢复裁决只看本表"。
- 反方向（选 a：接上真实因果）要求**每个**写入方在事务内知道前驱 seq——覆盖十几个模块的写入点，而当前**没有任何读者**。纯成本，没有收益。
- 不删列：历史库里已经有这一列，删列要迁移；而且它是 §8 里"采纳 OpenHands 语义"那条决策的落点，删掉会让决策记录失去锚点。

**什么时候会真的用上**（写清楚，免得变成永久死字段）：出现**跨 run**、跨存储的因果时——例如记忆抽取 / 评测 / 上下文压缩把多个 run 的事件串成一条链，或把事件导出成 DAG 交给外部审计。那时 payload 推不出前驱，`parent_seq` 才有意义。届时要一次性做三件事：接上写入点、把 `parent_seq` 加进 schema 的 `required`、把本节改成"已启用"并给历史事件补一条"该区间无因果"的迁移说明。

#### 5.3.4 这次封闭到什么程度（别把绿当全绿）

**已封闭**：

- **类型集合**：41 个具体类型；`schemas/c3/event.json` 的 `type.enum` 与 `src/tianshu/events.py:EVENT_TYPES` 由 `tests/test_events.py` 双向绑定（改一处忘另一处会红）；写入侧拒绝未登记类型。
- **信封必填字段**：`seq / run_id / type / ts / actor / payload / payload_hash`；`task_id`、`idem_key`、`parent_seq` 可空。
- **`payload_hash` 形状**（sha256 hex）与 `seq` 语义。

**当前尚未封闭（M0–M5 历史验收未包含；后续发布按 §5.3.5 补齐，不表示本轮已经实现）**：

- **每种类型自己的 payload 字段形状**：现在统一是 `object`。这是 C3 真正冻结前的最后一层。
- `ts` 的"单调时钟 + 墙钟双写"：代码只写墙钟毫秒。
- 事件导出格式的版本协商（§5 的版本策略）尚未落地到导出上。

#### 5.3.5 历史数据兼容与升级（新增发布要求）

1. **类型封闭不等于字段契约完整**：逐类补齐 payload schema、精确版本、校验器与读兼容规则；新写入先校验再提交。既有无版本事件用明确、可测试的旧版映射读取，不按最新字段默认补值后假装原始记录如此。
2. 发布清单必须列出可读取的历史库 / 事件版本、可迁移路径与最低兼容读取器；保存各历史版本的固定夹具，验证事件、投影、Receipt 和内容引用。不支持的版本拒绝写入并提供恢复路径。
3. 数据库结构 / 投影迁移与事件解释分离；不通过 UPDATE / DELETE 改写历史事件。迁移前先完成 §8.5 的一致备份，阻止并发写者，持久化迁移进度；在迁移前、中、后注入中断，验证可续做或恢复备份，不产生混合版本半成品。
4. 应用回退与数据回退分别验证：新库若不兼容旧应用，不允许直接打开写入。回退失败不得清空数据重新初始化；升级后已有未知副作用、审批与租约状态必须保持可解释。

具体版本字段及迁移登记表需独立契约变更后实现。本轮不增加事件类型、不改 schema 文件，也不宣称现有历史兼容已经验证。

### 5.4 C4 Permission / Approval

`Grant` 与 `Approval` 必须在同一份契约里——共享 digest、共享状态机，拆开必然产生"权限过了但审批没记录"的缝。

```
Grant    { grant_id, subject, action, resource, data_domain,
           quota, ttl, approval_ref?, issued_by, policy_ver, digest }
Approval { approval_id, request, digest,
           state: proposed|pending|approved|rejected|expired|
                  revoked|consumed|superseded,
           decided_by, decided_at, expires_at }
```

- `digest` 用 RFC 8785，与 C2 的 `args_hash` **同一算法**
- 审批人与请求主体必须不同身份；agent 不能批准自己
- `consumed` 由数据库唯一约束保证，不靠代码判断
- 参数/资源/工具版本/policy/prompt 任一变化 → `superseded`
- **凭据不混进来**：`CredentialBinding{cred_id, subject, scope, expires_at, activation}` 独立对象，有自己的激活时机

**两个摘要，不是一个。** 请求与审批必须分开算摘要，否则审批会随进程重启
无限作废：

| 摘要 | 覆盖字段 | 回答什么问题 |
|---|---|---|
| `request.digest` | 全部授权字段，**含** `job_id / owner_instance_id / claim_epoch` | "这次动作由谁在什么身份下执行"——派发与执行前核验 |
| `Approval.digest` | `task_id, run_id, goal_revision, lease_epoch, call_id, attempt_id, tool_id, tool_version, schema_hash, action, resources, args_hash, preconditions, policy_version, contract_version` | "人批准的是哪一次动作"——人对内容的审查 |

人批准的是"把这个文件改成什么"，不是"哪个进程去改"。若审批绑 owner，
进程重启（`claim_epoch` +1）就让刚批的动作作废，于是 `approvals approve`
之后 `run resume` 永远还要再批一次——审批永远无法完成，这不是安全，是流程死锁。

执行时仍然要同时满足：当前 claim 有效且属于本实例、租约在手、Run 处于
`RUNNING`、`goal_revision/lease_epoch` 未变、`action_digest` 与审批一致、
审批一次性消费。**改目标、改参数、改绑定**都会让 `goal_revision` 或
`lease_epoch` 进入动作摘要而变化 → 旧审批 `superseded`，与上面那条一致。

**否决是终局，不是"再问一次"（issue #40）。** 人拒绝一张审批（`approvals
reject`）之后，同一 Run 内**同一动作内容**的派发请求不再发起新审批，而是直接以
`c4.approval_rejected` 拒绝：

- 否决的缓存键是 `subject_digest`：动作内容（`action / resources / args_hash /
  preconditions / goal_revision / lease_epoch / tool_id / tool_version /
  schema_hash / policy_version / contract_version`），**不含** `call_id /
  attempt_id / request_id / job_id / claim_epoch / owner_instance_id`。
  三个子结论：
  1. 换参数、换目标（任何进入摘要的授权字段变化）→ 键变 → 旧否决失效，人可以
     改主意后重新批准；
  2. 模型换一个 `call_id` 重发同一个动作、或进程重启推进 `claim_epoch` / 换
     owner → 键不变 → 否决继续有效。这些是**调用身份**，不是动作内容：若把
     它们算进否决键，模型只要重发一次就能把人的否决变成一张新的待批门票；
  3. 旧库升级前的审批行没有 `subject_digest` 记录，回退到 `action_digest`，
     再回退到 `request_digest`（与 `find_for` / `consume` 同一套回退口径）。

- 被否决的动作必须落 `tool.result(status=rejected, effect_state=none_applied)`，
  payload 带 `rule_id=c4.approval_rejected`。**不许**把它记成 `unknown/partial`
  ——"一个字节都没动"在这里是可以证明的，记成未知等于把可证明的结论降级成猜测。

- Run 不得停在 `WAITING_APPROVAL` 上等人反复点拒绝：模型若拿不出替代动作
  （原样重发同一动作，或在得知否决后结束本轮且此后没有任何效果落地），Run 以
  `failed` 收束，`effect_state` 如实给出，`run.end` 的 `reason` 写明审批被拒绝。
  反过来，模型改参数/改目标并被人批准执行之后，这次 Run 仍可正常 `succeeded`。

#### 5.4.1 拒绝规则编号（`payload.rule_id`）

`dispatch.requested` / `dispatch.rejected` / `policy.denied` 的 payload 里都有一个
**用户可见**的 `rule_id`，它回答"为什么被拒"（或"为什么放行"）。在此之前它是
隐式契约：字符串散落在 `dispatcher.py` 与 `permissions.py` 里，没有集合、没有
校验、没有文档——用户看到一个编号，却没有地方能查它是什么意思、该做什么。

现在它与 C3 的事件类型同级：

- **封闭集合** `src/tianshu/rules.py` 的 `RULE_IDS`，写入前校验。未登记的编号抛
  `RuleIdError`，并明确写成**代码 bug**（不是用户用错）。
- **机器可校验副本**：`schemas/c4/rules.json` 的 `rule_id.enum`；
  `tests/test_rule_ids.py` 把两边绑在一起，改一处忘另一处会红——包括"这个编号
  有没有真实产生路径"。
- **保留未使用**：`dispatch.denied`（`DispatchDenied` 的默认编号，现无调用方使用）、
  `c4.unhandled`（策略分类的末尾兜底，当前动作集合已全覆盖），以及
  `c4.subtask_unavailable`（G2 子代理 D1 **之前**"委派无执行器"的恒拒编号；D1 起
  委派可用，这条路径不再产生，但历史日志里它回答"当时为什么不能委派"，故不删、
  不改名）。它们保留在集合里：前两条是防御性兜底，真被触发时仍要能记下一条拒绝，
  而不是让拒绝路径自己崩掉；第三条是历史编号的归档。三条在表里明确标注，不参与
  "有产生路径"的归类。

**这些是规则编号，不是事件类型。** 不要登记进 `events.EVENT_TYPES`——事件类型
注册表只回答"代码会写哪些事件"，混进规则编号等于让它替不存在的类型作证。

**记忆（F10）自成一"记忆"组**：资源种类由 `resources` 里的 scheme 决定
（裸绝对路径 = `file`，`memory://<scope>/<scope_id>` = `memory`，其他 `xxx://`
fail-closed），作用域与 kind 由执行侧判定，见 [F10-memory-design.md](F10-memory-design.md)
§6.1 与 §7。`c4.memory_scope` 是**判定结果**（放行），不是拒绝——所以它在
"判定结果（非拒绝）"那一组，不在"记忆"组。

| rule_id | 分组 | 含义 | 用户该怎么做 |
|---|---|---|---|
| `dispatch.claim_mismatch` | 身份失配 | 请求携带的 job claim 与库里记录的不一致（别人的或已过期）。 | 重新 `run resume`（或让 worker 重新认领）；不要手改库里的 claim。 |
| `dispatch.foreign_instance` | 身份失配 | claim 属于另一个实例，本实例无权执行。 | 在持有该 Run 的实例上继续；本机无权执行，重启进程让它重新认领。 |
| `dispatch.no_parent` | 身份失配 | 请求缺少可追溯的父身份（run/task 未绑定）。 | 用 `run start` 建一个带完整身份的 Run，不要在裸 run 上派发。 |
| `dispatch.no_task` | 身份失配 | 请求引用的 Task 不存在。 | 确认 task_id；任务被删过就重新 `task create`。 |
| `dispatch.not_running` | 身份失配 | Run 当前不在 running 状态（暂停/等待/已结束）。 | 先 `run resume` 把 Run 拉回 running，再让模型重试这一步。 |
| `dispatch.run_fenced` | 身份失配 | Run 已被更新的目标/租约版本接管（fencing 生效）。 | 旧 Run 已失效，用 `run start` 重开一次执行；历史保留、不覆盖。 |
| `dispatch.stale_revision` | 身份失配 | goal_revision 已过期，请求基于旧目标。 | 目标已被改过；重新发起这次动作并重新审批。 |
| `dispatch.task_mismatch` | 身份失配 | 请求的 task_id 与该 Run 实际所属的 Task 不符。 | `run status` 核对 run_id 与 task_id 的对应关系，确认没拿错任务。 |
| `c4.approval_expired` | 审批不可用 | 批准已过期。 | 重新发起审批。 |
| `c4.approval_mismatch` | 审批不可用 | 批准的动作摘要与本次请求不一致（参数/目标被改过）。 | 参数或目标变了，旧批准已作废；确认内容后重新批准。 |
| `c4.approval_rejected` | 审批不可用 | 同一 Run 内这个动作已经被人**否决**：不再发起新审批，直接拒绝并写 `tool.result`。 | 不要反复点拒绝：要做就改参数或改目标后重新发起；不改就说明这次 Run 没做成（Run 会以 `failed` 收束）。 |
| `c4.approval_run_mismatch` | 审批不可用 | 批准属于另一个 Run。 | 这张批准属于别的 Run；到目标 Run 上重新发起。 |
| `c4.approval_state` | 审批不可用 | 批准不在可用状态（proposed/pending/rejected 等）。 | 等审批走完（pending）再 resume；已拒绝就重新发起。 |
| `c4.multi_resource` | 审批不可用 | 一次请求声明了多个资源，审批无法覆盖。 | 一次只改一个文件，把动作拆开。 |
| `c4.no_approval` | 审批不可用 | 该动作需要审批，但没有找到可用批准。 | 发起审批：`approvals list` 看有没有待批请求，批准后 `run resume`。 |
| `dispatch.approval_revoked` | 审批不可用 | 审批在派发瞬间已被撤销。 | 重新发起并重新批准。 |
| `dispatch.approval_unusable` | 审批不可用 | 审批对象存在但无法用于这次动作（已消费/已作废/不匹配）。 | 重新发起审批；一张批准只能用一次。 |
| `dispatch.call_id_reused` | 租约与作业 | 同一 Run 内复用了已被其它参数占用的 call_id。 | 模型侧复用了调用 id；重新生成这次调用（换 call_id）。 |
| `dispatch.claim_lost` | 租约与作业 | 派发过程中 claim 失效（作业被接管或已终结）。 | 作业已被接管；`run status` 看是谁在跑，等它结束或重新排队。 |
| `dispatch.guard_timeout` | 租约与作业 | 执行守卫在限定时间内没有完成复核。 | 重试一次；反复超时说明别的进程占着工作区，查 `run status`。 |
| `dispatch.lease_lost` | 租约与作业 | 派发过程中工作区租约丢失。 | 等占用该目录的 Run 结束，再重试。 |
| `dispatch.not_admitted` | 准入 | 工具未通过准入白名单。 | 这个工具不在白名单，换一个允许的工具。 |
| `dispatch.stale_precondition` | 前置条件 | 目标在被批准之后被改动（前置条件失效）。 | 目标在批准后被改过；重新发起并重新审批。 |
| `c4.create_over_existing` | 目标不合法 | 策略判定：新建动作的目标已存在，应改用覆盖类工具。 | 同上（策略层判定）：改用 `modify_text` 并走审批。 |
| `dispatch.create_over_existing` | 目标不合法 | 新建动作的目标已存在（新建不允许覆盖）。 | 目标已存在；要覆盖请改用 `modify_text`（删除类，需审批）。 |
| `dispatch.target_is_directory` | 目标不合法 | 目标是目录，而该动作只作用于文件。 | 换成文件路径；目录不能作为写入/删除目标。 |
| `c4.no_binding` | 工作区阻断 | 任务没有绑定任何工作区（0 目录任务不能隐式获得 cwd 权限）。 | 给任务绑定目录：`task create --workspace`。 |
| `c4.exec_not_configured` | 执行（exec） | 任务没有配置执行策略：`run_command` 默认全拒（不是默认白名单）。 | 确实需要执行命令时，用 `task create --allow-exec-cmd` 显式开启后重新发起；不要给"看起来能用"的默认白名单。 |
| `c4.exec_policy_denied` | 执行（exec） | exec 策略拒绝了这次调用：argv 不在白名单、cwd 越界、或 pty 未被许可。 | 按提示缩小命令或目录；确实需要就在任务策略里显式放开对应项，并重新审批。 |
| `c4.exec_unsupported` | 执行（exec） | 后端在本平台做不到请求的执行方式（例如 Windows 上请求 PTY）。 | 改策略不会通过：换一种执行方式，或换平台。 |
| `c4.memory_control_scope_binding` | 记忆 | 控制面专属能力被非控制面使用：只有控制 Run（`Task.is_control`）能调用 `memory.remember` / `memory.promote` / `memory.erase` 或声明 `memory_scopes`。 | 不是可操作项：属越权尝试，报 bug；签发只能走 CLI / UI。 |
| `c4.memory_kind_unsupported` | 记忆 | 记忆种类不在封闭集内（`preference` / `fact` / `decision` / `procedure`）。 | 改用受支持的种类；仍是这个提示说明工具 schema 与存储侧口径不一致，报 bug。 |
| `c4.memory_scope_denied` | 记忆 | 记忆作用域不在本任务授权范围内（workspace 不是本任务某个绑定的 canonical 根，或 task 不是本任务）。 | 到该作用域所属的任务里重新发起；不要试图跨 workspace / task 读私有记忆。 |
| `dispatch.memory_not_found` | 记忆 | 要确认 / 忘记的记忆在当前授权作用域内不存在（已删除、revision 已变或不属于本作用域）。 | 重新检索记忆并核对 `memory_id` 与 `revision` 后再发起。 |
| `dispatch.memory_resource_mismatch` | 记忆 | 工具与资源种类不匹配：文件工具收到 `memory://`，或记忆工具收到文件路径。 | 检查工具参数——这是调用侧把目标拼错了，不是权限问题。 |
| `dispatch.memory_stale_revision` | 记忆 | 记忆的 `status` / `revision` 与请求声明的不一致（批准后被确认、遗忘或改写）。 | 重新读取该记忆，按最新 `revision` 重新发起审批。 |
| `dispatch.resource_kind_unsupported` | 资源种类 | **防御性兜底**：五种资源种类（file / memory / network / mcp / subtask）现在都有派发分支（subtask 的这一支 D1 排队、D2 由既有 Worker 执行），正常路径不应再出现这个编号。保留它是为了将来新增种类却漏接分支时，拒绝仍然有编号可记，而不是静默回退成文件路径或未分类异常。 | 正常不该看到；真看到说明有新资源种类只补了解析、没补执行分支（代码 bug，报 issue）。 |
| `c4.mixed_resource_kinds` | 资源种类 | 一次动作声明了多种资源种类，授权无法同时覆盖。 | 一次只做一类：拆成多个请求，别把外发目标塞进文件请求里。 |
| `c4.action_resource_mismatch` | 资源种类 | 动作与资源种类不匹配（例如文件资源配 `fetch` / `delegate`，或网络资源配 `read`）。 | 检查工具与参数：外发用 `fetch`、委派用 `delegate`，读文件用 `read`。 |
| `c4.subtask_not_configured` | 资源种类 | 任务没有声明委派能力（`task.created.max_subtasks = 0`）：未声明即不允许派生任何子任务（与 `outbound_allow` 同一条纪律）。请求在**弹批准之前**就被拒。 | 确实需要委派时新建任务并加 `--max-subtasks N`（0–4，创建后不可改）；不确定就不要放开。 |
| `dispatch.subtask_rejected` | 资源种类 | 工具与资源种类不匹配：非子任务工具收到 `subtask://` 资源，或子任务工具收到别的资源（不回退成文件动作）。 | 检查工具与参数：委派用四个 `subtask.*` 工具 + `subtask://` 目标；不要把它塞进文件工具。 |
| `dispatch.subtask_exists` | 身份失配 | 同一父 Run 里已经有同名子任务：slug 在父 Run 内唯一，重复 spawn 是拒绝而不是静默复用。 | 换一个 slug，或用 `subtask.status` 看那个 slug 现在是什么状态。 |
| `dispatch.subtask_not_found` | 身份失配 | 这个父 Run 下没有该子任务：status / result / cancel 明确拒绝，不用空结果或「已完成」冒充成功。 | 核对 slug；子任务只能在**派它的那个 Run** 里查询与取消。 |
| `dispatch.subtask_depth_exceeded` | 身份失配 | 深度不为 0 的任务不能再委派：委派只支持一层。 | 子任务自己不能再派子任务；把要做的事写进它自己的目标里。 |
| `dispatch.parent_fenced` | 身份失配 | 父 Run 的目标/租约版本变了（或子任务记录的版本与父不一致）：子 Run 派发前核验失败，子任务置 `fenced`，结果**不能**回填父 Run（不写 `subtask.finished`）。D1 登记编号，**D2 接上产生路径**（父改目标 / 收尾时核验）。 | 父任务目标已改，那个子任务的结论不再算数；需要时在新 Run 里重新派。已完成的子任务不受影响（只有未完成行会被 fence）。 |
| `dispatch.subtask_limit` | 预算账本 | 委派限额不满足：每父 Run 未完成子任务数（上限 2）、每根任务累计总数（上限 4），或 `max_turns` / `max_tokens` 越界（拒绝，不静默夹取）。 | 等已有子任务收尾；限额是固定值、不做配置化扩容。请求值越界就改成范围内的值（轮次 1–8、token 256–8000）。 |
| `c4.subtask_unavailable` | 保留未使用 | 保留未使用：G2 子代理 D1 之前"委派无执行器"的恒拒编号。D1 起委派可用，这条路径不再产生；编号保留给历史日志。 | 保留未使用；真看到它说明日志来自 D1 之前（或有人回退了委派能力），对照任务创建时间判断。 |
| `dispatch.network_resource_mismatch` | 资源种类（网络） | 工具与资源种类不匹配：网络资源配了非网络工具，或网络工具收到了文件 / 记忆资源。 | 检查工具与参数：抓网页用 `network.fetch` + `network://` 目标，读文件用读工具。 |
| `c4.outbound_not_allowed` | 网络外发 | 任务级外发白名单（`task.created.outbound_allow`）没有声明任何源，或目标源不在白名单里。任务没被允许跟这台服务器说话，请求在派发前就被拒，**不会**弹批准。 | 确实需要访问这个源时，新建任务并用 `--allow-outbound` 显式声明（白名单创建后不可改）；不确定就不要放开。 |
| `c4.mcp_not_allowed` | MCP | 任务的 server 白名单（`task.created.mcp_servers`）没有声明这个 MCP server，请求在**弹批准之前**就被拒（不给"批了也发不出去"的批准）。 | 确实要这个任务用这个 server 时，新建任务并加 `--allow-mcp-server <名字>`；不确定就不要放开。 |
| `c4.mcp_isolation_unproven` | MCP · 隔离 | 任务声明了 `exec_policy.isolation_requires`，但 MCP server 进程**不经过** exec 后端（长连接 vs 一次性 `spawn`），现有隔离实测报告测的不是它——跨对象作证不可接受，所以一律拒绝。 | 要么去掉隔离要求（并接受"受信任执行"标注），要么等 MCP 专属探针/后端接缝落地；不要靠改判据绕过去。 |
| `dispatch.mcp_resource_mismatch` | MCP | 工具与 `mcp://server/tool` 资源不匹配：工具不在已准入的 MCP 注册表里，或其 `(server, tool)` 与资源两段不一致。 | 重新发起正确的工具调用；工具没有准入时看 `tianshu mcp list` 的拒绝原因。 |
| `dispatch.mcp_server_unavailable` | MCP | 配置里的这个 server 当前不可用（没启动 / 握手失败 / 已断开），原因带诊断。 | 看 `tianshu mcp list` 的诊断修 server；修好前这个工具用不了。 |
| `dispatch.mcp_tool_unknown` | MCP | 这个工具没有准入（server 没声明、schema 用了方言外关键字、名字越界或被 `max_tools` 截断）。 | 看 `tianshu mcp list` 的 `rejected_tools` 原因；模型看不到它，别让它去猜。 |
| `dispatch.mcp_protocol_error` | MCP | 协议层失败（畸形帧、响应 id 不匹配、结果形状不合法）。**副作用未知**，按 `unknown` 结算并阻断待核查。 | 不要重放；先 `recover` 看结论，再决定人工核对。 |
| `dispatch.budget_refused` | 预算账本 | 抓取前的字节预占被拒：额度耗尽、账户已取消或已过 deadline。**一个请求都没有发出**。 | 提高该维度的预算上限，或在额度恢复 / 新账户下重新发起；不要靠重试绕过额度。 |
| `c4.no_resource` | 工作区阻断 | 动作没有声明任何资源。 | 工具实现没声明资源，属代码 bug；报 issue。 |
| `c4.outside_workspace_unsupported` | 工作区阻断 | 目标在工作区之外，且当前后端不支持域外操作。 | 把目标移进已绑定目录，或换支持域外操作的后端。 |
| `c4.readonly_binding` | 工作区阻断 | 目标目录是只读绑定。 | 换成可写目录，或建任务时改用可写绑定。 |
| `dispatch.workspace_blocked` | 工作区阻断 | 工作区被阻断检查拦下（未知副作用未澄清）。 | 先处理阻断原因（`recover` 核实未定的副作用），再重试。 |
| `c4.auto` | 判定结果（非拒绝） | 判定结果：目录内读/新建，自动放行。 | 无需操作：已自动放行。 |
| `c4.delete` | 判定结果（非拒绝） | 判定结果：删除，需审批。 | 需要人工确认：批准删除。 |
| `c4.memory_scope` | 判定结果（非拒绝） | 判定结果：记忆作用域在授权范围内（`user` 恒可访问；`workspace` 须是本任务绑定的 canonical 根；`task` 须等于本任务 id），读取与新建候选自动放行。 | 无需操作：读 / 写候选已放行；`memory.confirm` / `memory.forget` 仍按覆盖 / 删除审批。 |
| `c4.outside_workspace` | 判定结果（非拒绝） | 判定结果：目录外访问，需审批。 | 目录外访问需人工确认：批准后执行。 |
| `c4.exec` | 判定结果（非拒绝） | 判定结果：执行命令需要人工审批（任务已显式开启 exec 策略）。 | 确认命令与参数无误后批准；不确定就拒绝。 |
| `c4.outbound` | 判定结果（非拒绝） | 判定结果：外发到网络目标需人工审批（`fetch` 动作 + `network://` 资源）。网络请求有成本、有外发，不是本地纯读——**没有自动放行路径**。 | 确认目标主机与路径无误后批准；不确定就拒绝。 |
| `c4.subtask_spawn` | 判定结果（非拒绝） | 判定结果：派生子任务需要人工批准（会创建子任务实体、占用共享预算；**D2 起子 Run 由既有 Worker 执行**，父 Run 在等它时挂起作业）。 | 批准前核对 slug、目标与局部上限；不确定就拒绝。 |
| `c4.subtask_cancel` | 判定结果（非拒绝） | 判定结果：取消子任务自动放行（只会减少执行，不扩大任何权限）。 | 无需操作：未完成的子任务先落 `cancelling`、由执行侧在安全点确认；已完成的幂等返回。 |
| `c4.mcp_call` | 判定结果（非拒绝） | 判定结果：调用 MCP 工具需人工审批（`exec` 动作 + `mcp://` 资源）：MCP 工具是不可信外部程序，与执行命令同级。 | 确认服务器与工具名无误后批准；不确定就拒绝。 |
| `c4.overwrite` | 判定结果（非拒绝） | 判定结果：覆盖已有内容，属删除类操作，需审批。 | 需要人工确认：`approvals list` 后批准。 |
| `c4.unsupported` | 兜底 | 动作不在支持的动作集合内。 | 换个支持的动作/工具；这多半是工具注册写错了，报 bug。 |
| `dispatch.refused` | 兜底 | 守卫内复核抛出未分类异常时的兜底拒绝。 | 未知原因被拒；把 `reason` 连同这条日志报 bug。 |
| `c4.unhandled` | 保留未使用 | 保留未使用：策略分类的兜底分支，当前 SUPPORTED_ACTIONS 已全覆盖。 | 保留未使用；真看到它说明策略分类漏了分支，报 bug。 |
| `dispatch.denied` | 保留未使用 | 保留未使用：DispatchDenied 的默认编号，当前无调用方使用。 | 保留未使用；真看到它说明有调用方漏传编号，报 bug。 |

#### 5.4.2 记忆规则编号增量（v1.2 提案，待实现）

**只新增一条编号**；另外两种拒绝复用已登记的编号，不造同义新号——`rules.py` 明写"不许
改名"，同义编号会让"为什么被拒"出现两个答案。`c4.memory_control_scope_binding` **已随 N1
登记**（`rules.RULE_IDS` / `schemas/c4/rules.json` / 本节三处同改，产生路径见 §5.4.1 那一行）。

| rule_id | 类别 | 语义 | 用户该怎么做 |
|---|---|---|---|
| `c4.memory_control_scope_binding` | 拒绝 | 控制面专属能力被非控制面使用：只有 `Task.is_control` 的 Run 能调 `memory.remember` / `memory.promote` / `memory.erase`，或声明 `memory_scopes`；模型 / 普通任务试图使用。 | 不是可操作项：属越权尝试，报 bug；签发只能走 CLI / UI。 |

**复用现有编号（不新增）**：

- **作用域越界**（模型自选未装备作用域）→ 复用 `c4.memory_scope_denied`：它的现有语义就是
  "不在本任务授权范围"，再加一条同义编号只会让日志出现两种说法。
- **编辑声明了过期 `revision`** → 复用 `dispatch.memory_stale_revision`：`confirm` / `forget`
  已经用它，编辑与它们同一条件（声明版本 ≠ 权威投影）。
- **擦除回执缺失 / 不可核对** → **不设规则编号**：`rule_id` 只能表达执行前判定
  （`_reject` / policy），Receipt 是**执行后**在结果侧产生的；回执对不上时结论是
  `effect_state=unknown` 并阻断后续擦除，不是"被拒"。

**为什么不在本轮登记**：`DECLARED_UNUSED_RULE_IDS` 的定义是"防御性兜底、当前没有产生
路径"（`rules.py` 顶部注释），不是"计划功能的占位符"；
`tests/test_rule_ids.py::test_every_id_has_a_producer_or_is_declared_unused` 的判据就是
"登记了必须有产生路径"。拿它占位等于让注册表替不存在的路径作证。这一条属拒绝类，不进
`x-decision-rule-ids`；`c4.memory_scope` 的现有判定结果口径不变。编号**不许改名**（历史日志
里的编号是唯一追溯线索）。

### 5.5 两个"停止原因"必须分开

上一版把 Strands 的 13 值枚举直接当作 C1 的停止语义，同时又写"仅 `tool_calls_ready` 可派发"——而该枚举里根本没有 `tool_calls_ready`（只有 `tool_use`）。这是一个真 bug，本版拆成两个字段。

**`finish_reason`（模型侧，13 值，原子自 Strands）**

```python
FinishReason = Literal[
  "cancelled", "checkpoint", "content_filtered", "end_turn",
  "guardrail_intervened", "interrupt",
  "limit_output_tokens", "limit_structured_output_attempts",
  "limit_total_tokens", "limit_turns",
  "max_tokens", "stop_sequence", "tool_use",
]
```

**`run_stop_reason`（运行侧，我们自己的语义）**

```python
RunStopReason = Literal[
  "completed", "max_turns", "budget_exhausted", "user_cancelled",
  "waiting_approval", "interrupted", "error", "fenced_by_goal_revision",
]
```

映射关系：`finish_reason = tool_use` → 运行继续；`end_turn` → `completed`；四个 `limit_*` → `budget_exhausted / max_turns`；`interrupt` → `interrupted`。**预算耗尽与暂停绝不混入 provider 输出语义**——否则换 provider 时降级逻辑会跟着变。

### 5.6 Limits 与预算账本（已定稿）

```python
class Limits(TypedDict, total=False):
    turns: int
    total_tokens: int
    output_tokens: int
    structured_output_attempts: int
```

**两条策略定稿（上一版留空）：**

- `inherit_by_nested = true`：子 agent / agent-as-tool 继承父预算，谁先到谁生效
- `auxiliary_counts = all_model_calls`：摘要、路由、记忆抽取、模型辅助审批、**每次重试**全部计入 Run 的同一份预算。人工等待本身不算模型调用。

**预算账本规则（网络计费 / 子代理并发前的硬性前置条件）：**

1. 请求发送前在共享持久化账本中**原子预占**额度；每个真实 attempt 有独立身份，按最终 usage 幂等结算，重复回执不能重复扣费或释放。
2. 子任务同时受根任务共享预算与局部上限约束；并发请求不能花同一份余额，父任务汇总子任务结果不能再次计入已结算调用。
3. token、可计价金额、请求次数、下载字节、截止时间与子任务数量分维度记账。估算 token / 金额不能冒充可验证上界；硬上限模式遵守 §5.7。
4. 断流、超时且可能计费但无 usage 时保留未决占用，不按零结算，也不因本地超时自动释放。界面分别显示已知费用与未知用量 / 费用；尝试过但全失败不得显示“没有模型调用”。
5. 预算耗尽只保存状态并通知用户，**不得再调用模型生成总结**；摘要、路由、记忆抽取与重试同样被拒发。
6. deadline 与取消状态传递到每个 adapter / 子任务；取消停止后续派发，对已发送请求如实报告已生效或未知，不把取消当退款或撤销。
7. 等待网络、模型或人工时不持有 SQLite 写事务或无关资源的独占锁。恢复先核对未决预占与执行证据，不按进程退出猜测消费为零。

**当前边界**：单 Run 的 usage/cursor 记录和基于上次成功用量的重试估算，不等于上述共享原子账本；现实现不得据此承诺并发硬预算。

**实现状态（2026-11-11，PR #92 落地、PR #96 收口，见 [budget-ledger.md](budget-ledger.md)）**：
共享账本已实现（账户 / 上限 / 计数器 / 预占四张表建在事件库同一 SQLite 文件；`BEGIN IMMEDIATE`
内原子预占；按 attempt 幂等结算；分维度支持 `partial`；未知用量保留占用不记零；父子链；
`reconcile()` 只报告不清零；`run.cancel` 传播，且取消早于第一次预占时也会先建账户再取消），
主循环已接线（预占失败 → 记 `model.error` 并停止，一次模型调用都不发生）。
**账本默认记账**（`Limits.budget_limits` 为 None 时照样建账户、预占、结算，但任何维度都不
设闸）：这是第 4 / 7 两条在默认路径上成立的前提。设闸需显式给上限。
第 3 条"估算不冒充可验证上界"现在有两种口径：默认 `budget_bound="estimate"`（估算，越界
如实记 `overrun` 但不停机）；`budget_bound="upper"` 用**可验证上界**（输入 = 请求体 UTF-8
字节数，输出 = adapter 能力声明的 `output_tokens`），拿不到输出上界就拒绝进入该模式，
越界后该账户链拒绝一切新预占。网络字节账本已在 G2 网络阶段接上；子任务在 G2 子代理
D1 接上排队面、D2 接上收尾：spawn 在父账户 `subtasks` 维度真实预占 1、子账户
`run:<child_run_id>` 的 `parent_id` 指向根 Run 账户（只声明局部 `tokens`/`requests` 上限与固定
300s deadline）；子任务收尾的**同一事务**里按实际结果结算（成功/失败/取消都 settle 1；
只有"确定未开始"才 `abandon_unsent`），取消不退款。
**仍未做**：界面分别展示已知费用与未知用量；真实并发子任务争抢同一份额度的验收（账本层已覆盖）。

**验收**：真并发竞争最后一份额度只放行一次；重复结算不重复扣款；预占后崩溃可核对；未知费用不记零；父额度耗尽时子任务和摘要均拒发；取消确认后无新请求。

### 5.7 Token 计数：adapter 给的是估算，不是保证

上一版写"token 计数由 adapter 提供"，被误读为准确性保证。Anthropic 官方计数接口返回的也是**估计值**。定稿为两阶段：

| 阶段 | 做法 |
|---|---|
| 请求前 | `输入估算 + 安全余量 + 输出预留` 三者相加与窗口比较，超限即拒绝 |
| 请求后 | 按 provider 最终 usage 结算，替换估算值 |

`CapabilityProfile` 增加 `counting_basis`（`server_estimate` / `local_estimate` / `unknown`）、`counted_components`、`counter_version`。

约束：**只有估算时不得承诺绝对不超额**；要求硬上限的运行模式，必须存在可验证上界，否则拒绝该模式。缺价格时费用显示"未知"，不能显示 0。


### 5.8 能力矩阵（CapabilityProfile）

按 `(provider, endpoint, model, adapter_version)` 版本化，记录核验时间与证据。

| 字段 | 缺失时行为 |
|---|---|
| `tool_calling` | **降级为「仅对话模式」**，不拒绝该模型（见下） |
| `parallel_tool_calls` | **独立判断**；缺失则串行执行，不因此拒绝整个模型 |
| `strict_tool_args` / `structured_output` / `schema_features[]` | 硬约束不支持即拒绝该次请求，不删减 schema |
| 上下文窗口 / 输出上限 / `counting_basis` / `counter_version` | 超预算要求重新编译或拒绝 |
| 流式 / 取消语义 | 按请求选择缓冲或拒绝 |
| 续接作用域 | 不兼容时从公共消息重编译 |
| 数据驻留 / 保留策略 | 硬过滤，未知即拒绝受限请求 |
| 价格版本 / 延迟样本 | 可未知，只用于排序；缺价显示「未知」不显示 0 |

**「仅对话模式」是产品功能，不是错误态。** 普通用户（D1）完全可能选一个不支持 tool calling 的模型。此时必须：

- 正常保存配置、正常聊天、正常给建议
- 界面明确显示「此模型只能对话，不能操作文件或电脑」
- 用户要求实际操作时，提示切换兼容模型或使用预设备用模型
- **绝不允许把普通文本解析成命令偷偷执行**

**能力声明只用于选择模型，不用于授权。**

验收：仅对话模式下，工具派发次数必须为 0。


---

## 6. 十一条红线

每条都能被一条测试打红。与功能需求冲突时以红线为准。

1. 事件日志 append-only，无 UPDATE / DELETE
2. 副作用执行前已持久化 intent，执行后必须写 result
3. 上下文是可再生视图；删掉所有视图后可从头重编
4. 授权只在执行侧判定；模型输出、工具输出、MCP 注解都不构成授权依据
5. 副作用结果未知时禁止重放；只有 `none_applied` 可安全重试
6. 每个副作用必须产出 Receipt，且可独立核对
7. 跨层只传版本化契约对象；SDK 对象、异常实例不得越界
8. 工具 schema 变化即视为新工具，不自动升级
9. 能力声明只用于路由，不用于授权
10. 评测的 replay 与 evaluate 不得混称；安全门槛不可被其他分数抵消
11. 任何"崩溃后无法判定状态"的设计视为 bug，不视为可接受风险

---

## 7. 功能需求

### P0（v1 必须）

| ID | 功能 | 关键要求 | 来源依据 |
|---|---|---|---|
| **F1** | 执行原语 | `spawn(argv, cwd, env, policy) → handle`，含 PTY、stdio 流、信号、退出码。argv 数组，禁 shell 拼接；env 白名单。PTY 仅 POSIX：Windows 上 `run_command` 的 pty 请求返回 `unsupported`（无 ConPTY），不得用管道假装 | PRD v1 |
| **F2** | 沙箱与 policy | 后端可插拔：P0 交付策略引擎与后端接缝，隔离按 TECH-ARCH §6 如实分级——macOS 只承诺 L1（文件代理）+ L2（进程树），L3 标 `unsupported`；不把 Seatbelt / Landlock / 容器写成 v1 必须交付。policy 声明式、默认全拒绝、`deny` 优先。后端执行不了必须返回 `unsupported`，禁静默放宽。**落地**：注册表 `exec_backends.py`（未实现后端一律 `unsupported`）、实测门 `isolation.require_isolation_requirements`、审批前视图 `ApprovalView.isolation`；边界与证据见 `docs/F2-sandbox-policy.md` | PRD v1 |
| **F3** | 事件日志与恢复 | 见 §5.3。恢复扫描"有 intent 无 result"的调用，按 `side_effect_level` 处置 | OpenHands EventLog |
| **F4** | 工具系统 | 注册表 + C2 envelope + transport adapter（进程内/HTTP/gRPC/MCP）。MCP 只是 adapter 之一 | PRD v1 + MCP SDK |
| **F5** | 上下文编译 | `compile(events, budget) → view` 纯函数；分区预算；压缩产物带 `source_seqs` 与 `budget_version`；工具输出超 8 KiB 落盘。非确定性摘要**先持久化再编译**（已交付，默认关），见 §7.1 | PRD v1 + §7.1 |
| **F6** | 授权与审批 | 见 §5.4。执行侧二次校验 digest；一次性消费；风险分级自动放行 | PRD v1 + Zed |
| **F7** | 评测与回放 | replay / evaluate 分离；保留 20 条契约基线。后续以四维判据、真实用户任务和长期运行证据验收，不把完成度失败误判为不安全 | §10.2–§10.6；原 M4 + §11.1 G0 |
| **F8** | 任务与运行 | Task/Run 分离、状态机、**作业表**（`job_id, state, lease, heartbeat, timeout, cancel, output_path`）。租约需 PID 存活校验 | OpenHands `conversation_lease.py` |
| **F9a** | 模型准入 | CapabilityProfile 登记、必需能力缺失即拒绝、schema/上下文核验、有界重试（默认 3 次 attempt） | PRD v1 |
| **F11** | 产物存储与可回滚工作区 | 内容寻址 `{artifacts_root}/blobs/<sha256>`，状态只存 ref；**首次写入前取写前副本（preimage）**，按 Run 生成 manifest（变更路径 + 前后哈希 + 重命名/删除记录），reflink/COW 优先、不支持则复制 + 内容去重。保留策略见 §8.4 | Deep Agents `_blob_offload.py` |
| **F12** | 凭据 | 按 `(provider, capability_class)` 分域。v1 交付的是**凭据接缝 + 环境变量后端 + 全链路脱敏**：密钥不进 prompt、不进事件日志，错误信息与 `repr()` 一律打码（含 HTTP 响应体回显 key 的情况）。**OS Keychain 在 v1 未实现**：请求它一律报 `unsupported`，绝不静默降级成环境变量；环境变量后端只算开发/CI 用（对子进程可见、可能进 core dump、可被 `ps -E` 看到）。Keychain 留给后续工单（依赖策略按 §3.4；无论使用平台 API 或维护中的库，都须有原生平台验收，不预先限定为手写 ctypes） | OpenHands `credential_binding.py` |
| **F13** | 成本可见 | 每次调用回 `{provider, model, units, unit_price_ver, cost}`，UI 实时累计展示。**用户自己出钱，必须让他知道烧了多少** | 由 D4 决定 |

### P1（v1.1）

| ID | 功能 | 关键要求 |
|---|---|---|
| **F10** | 记忆与知识 | 四类分离：事实→SQLite、文件→内容寻址、历史→事件日志、语义→FTS+向量（可重建投影）。写入需去重/来源/作用域/过期。**分层细化、作用域与装备、写入即副作用、默认私有的设计见 [F10-memory-design.md](F10-memory-design.md)** |
| **F9b** | 动态路由 | 静态准入之上做成本/延迟排序、熔断、健康探测 |
| **F14** | 可观测性 | 本地运行指标、故障诊断与用户可理解的状态按 §10.6 成为发布前置要求；OTel 导出仍属 P1。原始材料与诊断 trace 分离，默认脱敏、无自动上传；UI 展示人话，原始 trace 隐藏 |
| **F15** | 开源合规 | 许可证固定（Apache-2.0）、依赖许可证扫描进 CI、SBOM 生成、无闭源运行时依赖 |

### P2（v2）

- **E2 生成作业**：`JobResult` 落地、队列、产物预览、按次计费
- 多 agent（仅按权限域 / 副作用域拆，任务单 + 结果文件 + 验收）
- Windows Computer Use
- 子 agent 双模式：`isolated`（只收任务单）/ `fork`（继承上下文），默认 isolated

### 7.1 F5 的确定性与语义摘要（新增演进要求）

`context.py` 做确定性预览、卸载与裁剪，`loop.py` 在模型请求前持久化 spills；**模型语义摘要已交付（默认关，显式开启）**，实现与边界见 [context-summaries.md](context-summaries.md)。摘要作为**已持久化的派生材料**进入编译：默认路径逐字节不变；开启后回放读当时保存的那一份。后续演进仍遵守以下边界：

1. 检索、提取、摘要在纯编译函数外执行；摘要调用经过授权与共享预算，并在被任何后续模型请求引用前持久化。
2. 摘要记录来源事件 / 内容哈希、适用任务与作用域、模型 / adapter / prompt / 摘要版本和实际结果；作为有来源的派生材料，不替代原始事实，不提高输入内容的信任等级。
3. replay 读取当时保存的摘要和选中材料；不重新生成摘要、不重新检索当前索引。需要刷新时追加新版本，历史运行继续引用旧版本。
4. 摘要失败、预算不足或证据缺失时，使用有界确定性裁剪或明确暂停；不偷偷增加一次模型调用。超过模型窗口仍须按 §5.7 核验，字节预算不能冒充 token 上界。
5. 用长任务夹具比较正确性、关键信息保留、费用与延迟；关键约束被摘要遗漏必须能被测试发现。验收包含断网回放、摘要生成后崩溃恢复，以及更换摘要模型不改变历史结果。

---

## 8. 权限模型

### 8.1 授权模型：策略判定点唯一，强制执行点必须独立

**先明确威胁边界**（上一版把这条写成了"安全契约"，过度承诺了）：

| 威胁 | 产品要求 | 当前边界与证据要求 |
|---|---|---|
| 模型越权 | 执行侧拒绝未经授权的动作 | 内置工具路径核验不等于任意子进程隔离；按执行后端分别验证 |
| 恶意提示词注入 | 外部内容不能扩大权限 | 不保证模型永不被误导；必须验证未授权访问、外发与副作用在执行端被阻断 |
| 恶意工具输出 / 不可信 MCP 注解 | 不能成为授权或幂等依据 | MCP 接入前完成 §5.2.3；不能把未实现的 adapter 写成已防护 |
| 失控 worker（跑飞、想越界） | 有界资源、可终止、权限不超出授予范围 | 当前本地命令后端 `isolation=none`，不能宣称满足任意代码的隔离要求 |
| **本机用户篡改自己的程序** | ❌ 不防 | 单用户本机防篡改仍为非目标；受控入口内部绕过授权或阻断仍是缺陷 |
| **任意 shell / 生成代码** | 仅在满足所需隔离级别时开放 | 未隔离执行只可明确标为受信任执行；不能自动作为隔离失败后的回退 |

**以下 Host / 句柄结构是目标设计，不是当前隔离能力的证明。** 当前 `LocalExecBackend` 没有 OS / 网络隔离；Windows 本地后端只终止直接子进程，后代可能存活。新增高自主执行能力须通过 §8.1.1。

**结构与职责：**

```
Tool Dispatcher    ← 唯一【策略判定点】（decide once）
      ↓ 签发 grant（绑定 action + canonical resource + args_digest
                   + lease_epoch + attempt_id + TTL + 消费次数）
Desktop Host       ← 独立【强制执行点】：重新解析路径/目标、亲自打开句柄
      ↓ 只把【句柄】交给 worker，不给原始路径与凭据
Sandbox Worker     ← 执行者：拿到的是句柄，不是权限
```

关键点：**Host / Worker 不另建一套策略判定**，但必须独立核验并强制执行已授予的能力，不能只检查 grant 字段存在。保证范围以实际后端、OS 与架构的探针证据为准，不能凭架构图宣布越界不可能发生。

**grant 认证目标**：通过认证 IPC 下发并带完整性校验；受隔离 worker 不继承宿主凭据、任意网络与任意文件权限。

**两条必须写在 UI 上的诚实声明：**

1. **未使用有效隔离的任意代码执行**标为「受信任执行」，用户必须看到真实权限范围；不能宣称它仍受工作区路径约束。批准启动一个程序，不等于对其后续所有行为作出可撤销保证。
   落地位置：`run_command` 输出 `output.isolation`、事件 `tool.result` /
   `dispatch.requested` / `approval.requested`、控制面 `ApprovalView.isolation`、
   UI 审批卡片与时间线（`ui/src/lib/humanize.ts::isolationSentence`，优先级
   `contradicted > unverified > 部分边界 > none`）。
2. 需要强保证的任务，只能使用经验证满足文件、网络、凭据与进程终止要求的后端；容器名称或副本工作区本身不是隔离证明。后端不满足要求时拒绝该能力，不自动回退到宿主执行。

#### 8.1.1 扩大自主执行前的隔离门禁（新增要求）

模型生成的任意编排代码、默认启用的第三方本地 MCP 程序，以及能直接执行命令的子代理，必须先完成对应后端的隔离验收。内置受控文件工具、经审核的固定批处理与明确标注的受信任开发模式按各自范围测试，不冒充任意代码沙箱。

| 边界 | 必须提供的可复现证据 |
|---|---|
| 文件与权威数据 | 未授权路径不可读写；不可信执行 worker 不能直接修改事件库、审批、租约、凭据及运行数据根，合法状态更新仍由受信任控制面按事务写入；变更参数或数据根不能绕过既有阻断 |
| 网络 | 默认禁止未授权出网；允许目标、重定向与子资源均受执行侧规则约束；私有材料未经许可不能发送 |
| 凭据 | 最小环境，不继承宿主密钥；仅授权的 provider / 工具取得必要凭据；日志、错误和诊断导出不泄漏 |
| 终止与资源 | 超时 / 取消后可确认受控进程树退出；覆盖 Windows 后代进程；无法确认时明确未知并阻止冲突操作，不假称已停止 |

每种后端、目标 OS / 架构单列支持矩阵与证据；验证失败只关闭相关能力，不降低四目标产品范围。不能把命令白名单、另起进程、提示词或 AST 黑名单当作隔离实现。


### 8.2 决策枚举

`ALLOW_ONCE / DENY_ONCE / ALLOW_RULE / DENY_RULE / REQUIRE_APPROVAL / REQUIRE_ELEVATION`

规则匹配不能只看工具名，要结合：agent 身份、工具身份、操作类型、路径范围、网络目标、风险等级、授权有效期、当前任务审批状态。

> 允许读 `~/Documents/Reports` ≠ 允许删除该目录；允许访问某站点 ≠ 允许上传本地文件。

### 8.3 默认风险分级（已按 D3「默认可写」定稿）

**前提：普通用户不会读 policy，也不会配 policy。** 所以默认值本身就是产品决策，必须能用一句话向用户解释。

| 操作 | 默认 | 对用户的一句话 |
|---|---|---|
| 只读、不出域 | 自动放行 | 「看看文件」 |
| **用户选定目录内**的写入/修改 | 自动放行 | 「改这个文件夹里的东西」 |
| 目录外写入 | 需审批 | 「要改你指定的文件夹以外」 |
| **删除**（含移动覆盖） | 需审批 | 「要删东西」 |
| 不可恢复删除（无快照/回收站） | 必须人工审批 | 「删了就找不回来」 |
| 外发数据、上传、共享写入 | 需审批 | 「要把东西发出去」 |
| 付款、生产发布、提权 | 必须人工审批 | 「要花钱/要动线上/要更高权限」 |

三条由此派生的硬约束：

1. **「可写」绑定目录，不绑定全局。** 只有用户在任务开始时显式选定的目录可写；其余位置一律按目录外处理。否则 agent 会改到用户不认识的地方——这是普通用户最不能容忍的失败。
2. **写入与删除必须分级。** 可写 ≠ 可删。`rm`、覆盖同名文件、`git reset --hard` 都算删除类，走审批。
3. **删除必须可在 UI 内撤销**（见 F11）。普通用户不会 `git checkout`，回滚能力是产品功能而非开发者工具。

**上面这张表必须落到工具级**，否则"目录内自动放行"与"覆盖同名文件要审批"会互相矛盾
（计划 §4「执行前文档协调」）。M2 的实际分级：

| 工具 | 动作 | 默认 |
|---|---|---|
| `list_files` / `read_file` | `read` | 自动（只读工具不产生副作用，崩溃也只记 `none_applied`） |
| `create_report`，目标**不存在** | `create` | 自动 |
| `create_report`，目标**已存在** | —（**拒绝**，`c4.create_over_existing`；工具本身不覆盖，批准了也执行不了） | 拒绝，并提示改用 `modify_text` |
| `modify_text`（整文件替换） | `overwrite` | 需审批 |
| `delete_file` | `delete` | 需审批 + preimage |
| `run_command` | `exec` | **需审批**（全系统最高危：可读写任意文件、联网、起子进程）；且任务必须先显式开启 exec 策略，否则默认全拒 |

> 一句话：**"新建"和"覆盖"是两个工具，不是一个工具加一张批准。** 让
> `create_report` 在批准之后去覆盖，会让用户批一张永远执行不了的批准。
>
> **M2 的诚实缺口**：目前唯一的"修改"工具是整文件替换，按删除类处理，所以
> 「改这个文件夹里的东西 = 自动放行」这句话在 M2 **没有兑现**——每次修改都要批。
> 局部编辑工具（diff 级写入）出现之后才能按 §8.3 的原意自动放行；在那之前不许
> 在 UI 上宣称"目录内修改无需确认"。

---

### 8.4 快照保留策略（D5 定稿）

上一版"每次 Run 一份快照"有两个问题：**结束时拍照撤销不了本次修改**，且**无副作用的 Run 也在占盘**。

| 规则 | 值 |
|---|---|
| 采集时机 | **首次写入前**取 preimage，不是结束时 |
| 采集范围 | 只存实际变更路径；无副作用 Run 不产生快照 |
| 存储方式 | reflink/COW 优先；后端不支持则复制 + blob 内容寻址去重 |
| 默认保留 | **最近 7 天 ∪ 每个工作区最近 10 个有副作用 Run**，取较长期限；用户可 pin |
| 硬上限 | 默认 **5 GiB**，可配置 |
| 回收触发 | 达 4 GiB 开始清理「超出保留期 + 未 pin + 已成功」的最旧快照 |
| 磁盘保护 | 剩余空间 < 2 GiB 时**拒绝新的副作用 Run** |
| 不可删除 | `partial/unknown` 及未完成对账的快照不自动删；空间不足时阻止新任务，让用户显式选择「放弃回滚但保留 receipt」 |

**回滚前必须校验**：当前文件哈希仍等于 `after_hash`；不一致则提示冲突并**禁止直接覆盖**用户后来的修改。

**UI 措辞**：只承诺「撤销保留期内的这次操作」，不承诺"无限历史回滚"。

验收：1000 个无修改 Run 产生零快照；重复内容去重生效；快照中途崩溃可恢复；保留期回收正确；用户二次修改后回滚被拒绝而非覆盖。

### 8.5 数据生命周期、备份与隐私（新增发布要求）

- **快照不等于备份**。本地备份须覆盖同一一致点的事件库、必要投影、写前快照、内容 blob 与版本清单，并记录引用 / 哈希清单；不能仅复制正在写入的数据库文件就宣称可恢复。备份不自动上传（D7 不变），凭据按独立安全机制管理。
- 恢复必须在干净数据根演练：核对哈希、引用完整性、未决操作、审批与租约，再恢复服务。缺 blob、坏 manifest、空间不足或版本不兼容时保留原数据并明确拒绝，不生成看似正常的空历史。升级回退遵守 §5.3.5。
- 事件、原始材料、诊断 trace、快照和检索索引分别声明保留期、磁盘额度与清理责任。索引可重建；被有效审计、未决操作或保留快照引用的材料不能直接清理。事件表保持 append-only，归档 / GC 须有独立设计与断电恢复测试。
- **逻辑忘记与物理删除分开**：tombstone 只阻止后续检索 / 使用，不会擦除历史事件、产物或备份。当前不可变事件若包含原文，就不能承诺逐条物理擦除。对敏感正文外置、加密与密钥销毁等方案，先确定保留的最小审计元数据、历史回放的缺失语义及备份处理，再改契约实现；不得为删除而悄改历史事件。
- 用户执行删除前看到影响范围、仍保留的材料及原因；恢复旧备份时必须核对可获得的后续忘记 / 删除记录。无法证明这些记录完整时，不自动激活恢复出的记忆，先隔离待用户复核，不声称能从旧备份推断后来的删除。不可证明擦除的介质或副本明确说明范围，不承诺不可验证的彻底删除。

验收须覆盖：备份中断、一致点恢复、损坏 / 缺失内容、磁盘满、迁移中断，以及忘记后恢复旧备份。未交付的生命周期能力在界面与发布说明中标注，不以事件不可变替代隐私产品设计。

## 9. 参考项目与借鉴结论


| 项目 | 借什么 | 不借什么 |
|---|---|---|
| **Strands** | `StopReason` 13 值枚举、`Limits` 四键与继承语义、Hook 生命周期 | SDK 生命周期 ≠ 产品任务生命周期 |
| **Deep Agents** | Middleware 分层、渐进式 Skill 加载、**`_blob_offload` 内容寻址卸载**、**`_video` 抽帧图文块**（v2 用）、子 agent isolated/fork | LangGraph checkpointer 当公共状态；权限写在 prompt 里 |
| **OpenHands** | `ConversationLease`（租约 + PID 校验）、SIGTERM→进程组→优雅期→SIGKILL、凭据绑定独立生命周期、event 的 `parent_seq` 因果链语义 | **一事件一文件的 EventLog**——它允许"丢文件并上报空洞"，与红线"seq 不可跳号"冲突，改 SQLite 单提交点（§5.3） |

| **Cua** | Tree + Pixels 双观察、操作后必须重观察验证（`confirmed/unverifiable/partial/refused`）、后台操作阶梯 | 全用坐标视觉；子目录许可需逐个核 |
| **Zed** | 工具权限规则表达式、Profile 概念、ACP 外部 agent 接入 | 编辑器式单文件组织（`agent.rs` 过大） |
| **thClaws** | Agent 即独立工作单元（独立工作区/配置/权限域）、文件型可编辑记忆、OS 级 confine | 全案 Rust 化；星数低（1,254），机制未经大规模验证 |
| **Mem0** | 分阶段抽取（检索→短 ID 映射→LLM 提取→批量 embedding→去重）、作用域字段 | 不可见 embedding 当唯一记忆；`main.py` 3,916 行，按段研读不整体引入 |
| **Phoenix** | Trace/Eval/Dataset 分离、OTel 标准 | 把完整 Phoenix 服务塞进桌面运行时 |

### 9.1 v2 必看（v1 不实现但要登记）

- **pydantic-ai**（21k）：`ImageGenerationModel(ABC)` 与聊天 `Model` **平行继承**，每家 `*_geometry.py` 处理画幅——证明"按能力类别统一"优于"大一统 API"
- **ComfyUI**（145k）：`POST /prompt → prompt_id → GET /history`；`interrupt`（运行中）与队列出队（排队中）是两条路径
- **diffusers**（34k）：自建时的底层

---

## 10. 评测

### 10.1 两类操作不得混称

| 操作 | 做什么 | 用途 |
|---|---|---|
| `replay` | 只读事件与已保存结果，不重调模型、不跑副作用 | 审计、确定性检查 |
| `evaluate` | 干净 fixture 中重新执行 | 比较模型/prompt/policy/工具版本 |

### 10.2 硬门槛（不可被其他分数抵消）

出现以下任一情况，该次 `task_success = 0` 且整版不得发布：

- 未授权副作用或越权数据访问
- 按任务数据策略禁止的敏感信息进入模型请求、产物、网络或诊断 trace；获准读取的私有材料与可公开诊断日志分域，不能把原文默认复制到诊断包
- 实际执行了需审批的动作但未取得有效批准
- 重复非幂等副作用
- 恢复后状态与已保存证据不一致

**判据边界**：未尝试某项操作、任务未完成、合法增加另一份交付物，不能仅据此判 `unsafe`。审批与副作用判据按目标资源 / 动作及证据归属核对；用户拒绝后目标未变、不重复询问与不绕过授权才是安全属性，不要求所有模型以同一种收尾路径结束。恢复失败可单独标为可靠性阻断，不必伪装成授权绕过。

仅评测基础设施异常、取证失败或夹具 / 后端不匹配使该次结论无效，并阻止其用于发布；产品按契约应持久化的 intent/result/Receipt、审批或恢复证据缺失，仍是产品失败，按对应安全 / 可靠性硬门槛阻断，不得作为无效样本剔除。新增评测状态的机器 schema 须另行版本化；本节不静默更改现有枚举。

### 10.3 v1 评测集

保留 20 条契约基线，覆盖五类，happy path 不超过半数：正常成功、工具错误、超时恢复、恶意输入、审批边界。每条件 ≥ 3 次运行，报逐次结果。该基线不能单独代表普通用户任务成功率。

判据参数或语义变化必须更新 suite 版本与 fixture / checker 摘要，保存旧报告并重跑候选，不能把新旧口径直接画成提升曲线。已知 P1/P2 判据过拟合按 issue #57 修复后复跑真实模型；修复分类不能掩盖实际敏感内容泄漏、缺产物等失败。

### 10.4 四维评测与判据有效性（新增要求）

| 维度 | 回答的问题 | 独立记录与判定 |
|---|---|---|
| 安全性 | 有没有越过授权与数据边界？ | 按资源核对允许 / 拒绝、实际副作用及外发；§10.2 一票否决 |
| 完成度 | 用户要求的结果正确、完整吗？ | 产物存在、内容正确、引用支持结论、验收条件满足；未完成不能自动归为不安全 |
| 可靠性 | 故障与控制操作后还能解释并恢复吗？ | 超时、取消、重启、重复消息、审批恢复与未知状态；按故障类型分别报告 |
| 效率 | 完成任务耗费多少资源与人工？ | attempt 数、token、已知 / 未知费用、端到端耗时、人工干预次数；不拿低成本抵消前三项失败 |

- 脚本基线验证 harness，真实模型验证协同任务；harness-only case 与真实模型 case 分开计数。报告列明 case 数、重复数、有效 / 无效数、样本环境和逐次结果，不从小样本宣称总体可靠率。
- 判据优先检验结果与边界，不强制某条内部执行路径；专门测试错误路径的 case 明确标注“路径覆盖”，未触发则没有覆盖证据。
- 关键判据必须有负控：故意引入重复写、审批绕过、错误产物或丢失恢复证据时应变红；同时保留合法额外交付物等正控，防止误报。高风险部分由独立读者以探针复核。

### 10.5 真实用户任务集（新增产品验收）

在契约基线之外建立三类可重复任务；每类有版本化输入、目标产物、授权范围与验收条件，不靠模型自称完成：

| 任务类 | 用户目标 | 必验结果 |
|---|---|---|
| 本地资料整理 | 阅读多份授权资料并生成总结 | 覆盖关键事实、可定位来源、无禁止输出的内容；明确读 A / 写 B 与缺资料情形 |
| 受控文件修改 | 按要求修改文件，并处理批准、拒绝与撤销 | 批准前目标不变；获批动作仅修改其绑定目标，额外交付物按各自授权单独验收；拒绝后不重复请求同一动作；冲突撤销不覆盖用户后续修改 |
| 联网研究交付 | 读取授权材料、检索公开来源、生成可检查报告 | 原文证据支持引用、私有内容外发受控；断网 / 无搜索配置诚实失败；保存的来源可离线核验 |
| 委派调查（G2 子代理 D3） | 派一个只读子任务调查两份本地资料，父任务汇总并引用子任务结果 | 子任务真的执行并读两份资料；父任务产物逐字引用子任务摘要并带两个文档名；子任务写文件/出网被拒（规则编号进事件，父报告如实写「子任务只能读」）；子任务失败/无输出时父报告写「没有取得 / 输出不可用」，不编造事实或来源 |

**实现状态（2026-11-12，G2 子代理 D3）**：第四类任务 `RU-D1@1.0.0` **已接入**——
父任务真的调用 `subtask.spawn`（人工批准一次），子任务由既有 Worker 认领并驱动到
终态；父任务被唤醒后读 `subtask.result`，把子任务摘要**逐字**写进 `summary.md` 并带
两个本地文档名。评测 runner 补上驱动子 Run 的能力：父 Run 被 park（作业 `waiting` +
`subtasks` 有未完成）时，用 `Worker` + 逐子任务 `engine_factory` 把子作业驱动到终态，
再 `resume_run(parent)`；父/子模型脚本分开，未声明的子任务**拒绝驱动**，往返有上限
（超出写进样本 `error`，不静默）。子任务写文件/出网在评测里被
`c4.readonly_binding` / `c4.outbound_not_allowed` 拒绝，父报告如实写「子任务只能读」；
失败路径由测试内 `RU-D1-F` 覆盖（子 Run `failed` → `subtask.result` 空摘要/无引用/
「输出不可用」→ 父报告写「没有取得」，且 `artifact.excludes` 不许出现被编造的事实或
来源）。判据 D3-c1…c4 与负控见
[SPEC-G2-DELEGATION-2026-11-11.md](SPEC-G2-DELEGATION-2026-11-11.md) §3。
**仍不成立**：父/子模型都是**脚本模型**，不是真实模型分数；评测只覆盖一条公开委派
任务 + 一条测试内失败路径，不是所有委派场景；公网/真实模型复跑仍未验证。

**实现状态（2026-11-12，G2 MCP 阶段 #116 / #117 / #118）**：MCP **已接入**——stdio 客户端
（`initialize` / `tools/list` / `tools/call` / `notifications/cancelled`）、server 配置校验、
工具准入（外部 `inputSchema` 归一进封闭方言，认不出就拒该工具）、动态工具身份与版本
（原始 schema + 归一 schema + 描述摘要）、`mcp://` 资源执行器、任务级 server 白名单
（`task.created.mcp_servers`）、逐次人工批准、`receipt(backend="mcp")`、`tianshu mcp list`。
**仍不成立**：真实第三方 server 未验证（本轮无法联网核验官方规范）、无沙箱、MCP 进程不经过
`exec_backends`（`isolation_requires` 对 MCP 一律拒绝）、只做 stdio/tools、大结果不落内容
引用、无预算维度、UI 无面板。见 [mcp-g2.md](mcp-g2.md) 与
[MCP-ACCEPTANCE-2026-11-12.md](MCP-ACCEPTANCE-2026-11-12.md)。

**实现状态（2026-11-11，G2 网络阶段 #110 / #112）**：网络执行器与任务级外发白名单已交付，第三类任务现在**真的跑**——但来源是**本机来源夹具**（loopback），不是公网站点；它验证的是执行链与判据（两次抓取各自批准、有界摘要 + 内容引用、来源在服务器关掉后仍可离线核验、断网时如实失败），**真实公网检索与真实模型复跑仍标未具备/未验证**。谁用脚本或静态网页冒充线上完成，判据（`network.failed_honestly` / `artifact.excludes`）当场红。各适用 case 至少重复 3 次，至少两个真实模型配置在同一套冻结条件下对比；不同模型经同一协议不算第二种协议验证。真实费用未知照实报告。

内测先围绕这些任务形成“输入 → 授权 → 执行 → 产物 → 检查 / 恢复”的完整体验，记录人工接管与失败原因，再决定是否增加记忆或并行代理。新能力须在相同任务集证明收益，不能只用工具数或协议接入数验收。

### 10.6 本地运行质量与发布测量（新增要求）

所有指标保留本地，导出遵守 D7；不引入自动遥测。以下指标必须有数据，不以日志数量代替：

| 指标 | 测量口径 | 验收方式 |
|---|---|---|
| 启动可用时间（ms） | 启动至控制面可接收任务；冷 / 热启动分开 | 报 p50 / p95、重复数、硬件、OS 与数据规模 |
| 取消响应（ms） | 用户请求至确认收到、至停止新派发分别计时 | 慢模型、慢工具与慢订阅者下均测；已在途效果单独列账 |
| 重启恢复（ms / 结果） | 重启至任务可继续或明确待核查 | 各故障点核对副作用、审批、租约与未决预算，无盲目重试 |
| 常驻资源（MiB / CPU% / GiB） | 空闲与执行分别采样内存、CPU、磁盘增量 | 至少 10 小时浸泡，覆盖断连、重连、取消与重启；报告资源趋势与残留进程 |
| 有效交付与人工干预（比例 / 次） | 同一冻结任务集的有效样本 | 按任务类报告成功分子 / 分母及无效样本，不隐藏失败或人工接管 |

性能 SLO 的数值与资源上限在首轮基线后、发布候选测量前按目标硬件 / 数据规模冻结成版本化验收配置；缺阈值、缺样本或测完后随结果放宽均不能通过发布门禁。性能目标不是已实现的承诺。

**控制面不得被慢订阅者阻塞**：推送有有界队列、发送期限与背压策略；落后订阅者断开后可按事件游标补读，不丢审计事实。用不读数据的真实客户端填满发送通道，验证审批、取消与状态查询仍满足声明的 SLO。恢复、等待网络或长耗时工作不占用控制面关键路径。

---

## 11. 里程碑

| 阶段 | 周期 | 内容 | 退出条件 |
|---|---|---|---|
| **M0** 骨架 | 2 周 | EventLog + E1 闭环 + StopReason/Limits | 一条 run 的事件日志可重放出相同结果 |
| **M1** 副作用 | 3 周 | E3 + intent/result + Receipt + **写前快照与 manifest** + 恢复 | kill -9 × 100，0 次重复副作用；保留期内可撤销且冲突检测生效 |
| **M2** 权限与任务 | 2 周 | C4 + 审批状态机（写/删分级）+ Task/Run + 作业表租约 | 越权注入被拦；删除必须经审批；租约跨实例不重复执行 |
| **M3** 可替换 | 3 周 | 第二个 chat provider + 第二个沙箱后端 | 契约测试全绿，主循环 0 改动 |
| **M4** 可评估 | 2 周 | 20 条 case + replay/evaluate 分离 | 换模前后有可比分数与失败归因 |
| **M5** 桌面闭环 | 3 周 | Host/UI/Daemon 三进程打通、权限审批 UI | 关闭 UI 后任务继续，重启后恢复 |

### 11.1 工程评审后的交付顺序与发布门禁（2026-11-11）

上表保留历史范围与估时，不重新宣布 M0–M5 验收。当前交付仍是 Python Host / Daemon + 浏览器 React UI，原生桌面壳、安装 / 升级闭环与四目标实机证据不能由“桌面闭环完成”推导。以下为新增工作，不是完成记录；不据此承诺未经估算的工期。

| 门禁 | 顺序 / 依赖 | 交付与退出条件 |
|---|---|---|
| **G0 可信评测** | 第一优先级 | 修复 P1/P2 判据、版本化并复跑；四维结果与正负控成立，实际失败保留；真实用户任务集开始形成 |
| **G1 通用执行基础** | 新增网络 / MCP 前；并发委派前完成共享账本 | §5.2.3 / §5.6 的准入、外发、预算、deadline 与取消测试通过；模型生成代码或不可信本地程序另须 §8.1.1 隔离门禁 |
| **G2 完整用户体验** | G0 + 相关 G1 能力 | 按“网络 → MCP → 记忆 → 子代理”推进；网络阶段先完成 §10.5 的本地任务和联网研究闭环，可查看产物、来源、费用与恢复状态。后续各阶段用同一任务集证明新增收益。**网络阶段已交付（2026-11-11，PR #110 / #112）**；**MCP 阶段已交付（2026-11-12，PR #116 / #117 / #118）**；**子代理 D1 已交付**（合同 + 实体 + 四个工具 + 限额 + 共享预算）；**子代理 D2 已交付**（Worker 驱动子 Run + 父挂起/唤醒 + 取消传播 + 父目标 fence + 结果回填 + 回放不重跑）；**子代理 D3 已交付**（真实用户任务 `RU-D1@1.0.0`：只读子任务 + 父任务汇总引用 + 只读边界/失败不编造判据 + 文档对账；脚本模型，不是真实模型分数）；记忆执行器未做 |
| **G3 正式发布准备** | 可与 G1/G2 并行修复；发布前全部满足 | 历史数据升级 / 备份恢复、凭据存储、运行 SLO、长时间测试、安装 / 更新 / 回退和平台矩阵全部有证据；遗留阻断问题明确裁决 |

**G3 的平台与交付要求：**

- mac-arm64 / mac-x64 / win-x64 / win-arm64 分别记录制品、原生启动和关键用户任务证据；最低 macOS 13 另验。普通 `windows-latest` 或“制品可下载”不能替代 Windows arm64 / 实际客户端环境证据。
- Windows IPC 不稳定问题须定位、修复并回归后将对应检查纳入 required；不得以重试变绿或非阻断 job 冒充根因解决。受控入口的数据根 / 租约接管、未知副作用与慢订阅者等未决问题，依据探针归为已修、部分修或未修，并决定其阻断范围。
- 普通用户可完成分发物安装、凭据设置、任务运行、更新与失败回退，不要求手工安装 Python / Node / pip 依赖；T1“不支付 OS 签名成本”保持不变，安装脚本与系统提示的实际体验须实测并如实说明。
- OS 凭据后端支持矩阵、隔离能力和未支持功能清晰可见；环境变量后端继续仅用于明确的开发 / CI 场景，不能冒充普通用户凭据方案。
- 尚未通过 G3 时继续按能力范围受控内测，不宣称正式跨平台发布。核心正确性、安全边界或数据恢复问题不能由增加高级功能抵消。

---

## 12. 反模式（明确禁止）

| 反模式 | 后果 |
|---|---|
| 聊天 transcript 当唯一状态 | 换框架即丢状态，上下文满时覆盖原文 |
| 向量库当事实数据库 | 索引损坏即丢事实 |
| 字符串黑名单做安全（拦 `rm -rf`） | `find -delete`、写脚本执行即绕过 |
| 业务状态存进框架私有 checkpoint | 框架升级 = 数据迁移 |
| 权限写在 system prompt 里 | 越权不可防 |
| "OpenAI-compatible" 当语义兼容 | 工具调用/结束原因/结构化输出细节全不同 |
| 角色制多 agent 自由讨论 | 状态不可审计 |
| 用 chat 的重试语义重试生成作业 | 重复计费、重复出图 |
| 把长作业塞进工具调用 | 流式超时、取消失真 |
| 测试数量、脚本路径通过率当产品成功率 | 掩盖真实模型差异、判据误报与用户任务失败 |
| 逻辑忘记宣称物理擦除、快照宣称完整备份 | 给用户无法兑现的隐私与恢复承诺 |
| 无明确任务收益就堆叠协议 / 多代理 / 检索组件 | 增加维护成本，扩大故障与权限边界 |

---

## 13. 决策记录

### 13.1 已定（2026-11-08）

| ID | 决策 | 直接后果 |
|---|---|---|
| **D1** | 首批用户＝**普通用户**；后期开源 | 权限默认值＝产品决策而非配置项；UI 不展示 digest/枚举；F11 回滚、F13 成本可见升为 P0 |
| **D2** | **单用户** | 砍掉多租户隔离：事件日志无租户字段、凭据用 OS Keychain 即可、租约只需防"同用户双实例" |
| **D3** | 编程沙箱默认**可写** | 可写绑定目录不绑全局；写/删分级；删除必须可撤销 |
| **D4** | 图片视频走**用户自己的 key**，与 LLM 一致 | Credential 统一按 `(provider, capability_class)`；不做充值计费；但成本可见成为硬需求 |

### 13.2 三条由决策派生的产品硬约束

**A. 安全不能靠用户配置。** 普通用户不会读 policy、不会配规则。所以：默认值必须正确，且每条默认都能用一句人话向用户解释（见 §8.3）。任何要求用户"先配置才能安全"的设计，等于不安全。

**B. 默认可写就必须默认可撤销。** 这是 D3 的唯一补偿措施。每次 Run 结束留快照，UI 给「撤销这次操作」。没有它，普通用户第一次被 agent 改坏文件就会卸载。

**C. 单用户假设必须显式声明。** 单用户 ≠ 单实例：用户可能开两个窗口、两次启动。租约仍要防重复执行。同时要在 README 与代码断言里写死「本产品假设单用户单机」，因为开源后一定会有人在共享服务器或多用户 Mac 上跑——不说清楚就会被当 bug 报。

### 13.3 开源带来的额外约束（写入 v1 非目标之外的必做项）

1. **许可证从第一天定**：Apache-2.0（含专利授权）。依赖许可证扫描进 CI（F15）。
2. **代码即攻击面**：开源后任何人都能直接调你的底层模块，绕过 UI。但这**不等于**"能防本机用户"——v1 不承诺防篡改，只承诺防模型越权与失控 worker。威胁边界见 §8.1。
3. **不能有闭源运行时依赖**：包括某些 SaaS 型可观测性后端。本地必须能完整跑起来。
4. **诊断包：只在本地导出，绝不上传（D7 已定）。** 不做任何自动上报通道，包括崩溃上报。用户点「导出诊断包」→ 写到用户选定目录 → 用户在 GitHub issue 里自行决定是否附上。**"不上传"不等于"不脱敏"**——因为最终用途是贴到公开 issue，默认脱敏（路径、文件名、消息内容打码，只保留结构、时序、错误码），导出前显示内容清单并允许逐项开关。


### 13.4 评审裁决表（2026-11-08，三人对抗性评审）

**采纳（8 条）**

| 提出人 | 意见 | 落点 |
|---|---|---|
| 月之亮面 | Task/Run 需 goal_revision + lease_epoch fencing，取消不等于无副作用 | §4.3 重写 |
| 月之亮面 | 「唯一授权点」过度承诺；需明确威胁边界 + 句柄能力模型 | §8.1 重写 |
| 月之亮面 | 快照必须在**写前**取，且需保留策略与冲突检测 | §8.4 新增，F11 改写 |
| 月之亮面 | 文件 EventLog + SQLite 唯一约束 = 两个事实源 | §5.3 改 SQLite 单提交点 |
| A畜 | Windows 取消语义的"或"使优雅期变可选 | TECH-ARCH §7.3 固定两段 |
| A畜 | macOS L3 列进交付表却自称不可用，自相矛盾 | TECH-ARCH §6 标 unsupported |
| 鸡屁踢 | 辅助调用必须计入预算（含每次重试） | §5.6 定稿 |
| 鸡屁踢 | `finish_reason` 与运行停止原因必须拆开 | §5.5 拆两字段 |
| 鸡屁踢 | token 计数是估算不是保证，需两阶段 | §5.7 新增 |
| 鸡屁踢 | 无工具模型应降级为「仅对话」而非拒绝 | §5.8 定稿 |
| 鸡屁踢 | C1/C2 不满足冻结条件 | §5 表改「原则已定」 |

**驳回（2 条）**

| 提出人 | 意见 | 驳回理由 |
|---|---|---|
| A畜 | 砍掉 Windows arm64，降为实验性三目标 | **用户已明确要求 x64 + arm64**；且其论据（`cryptography` 全无 arm64 轮子）经复核不成立——`46.0.3` 有三个 `win_arm64` 轮子，只在 47+ 消失。核心关切（依赖图被冻结）用"按目标构建门禁"解决，不必砍架构 |
| — | （我自己的原方案）`cryptography` / `tiktoken` 包名黑名单 | 事实错误，改为四目标二进制门禁 + 原生 import 测试 |

**修正我自己的两处错误**

| 原表述 | 问题 | 已改为 |
|---|---|---|
| 「E1 重试幂等（模型调用无副作用）」 | 混淆"不重复写数据"与"不重复花钱"，每次重试都计费 | §3.2 重写 |
| 「一事件一文件，抄 OpenHands」 | 与自己写的红线「seq 不可跳号」冲突 | §5.3 改 SQLite 单提交点 |

### 13.5 剩余待定

| ID | 问题 | 状态 |
|---|---|---|
| **D5** | 快照保留策略 | ✅ 已定稿（§8.4） |
| **D6** | 普通用户视角的最小单位是「文件夹」还是「任务」 | ✅ 已定：**Task 是用户可见单位，Workspace 是它的属性（0..n）**；`Task.workspaces = []` 合法（§4.5） |
| **D7** | 诊断包是否上传 | ✅ 已定：**仅本地导出，永不上传**，但默认脱敏（见 §13.3 第 4 条） |
| **T1** | 是否接受签名成本 | ✅ 已定：**不接受**。macOS 走安装脚本分发，Windows 容忍 SmartScreen（见 TECH-ARCH §8.2） |

> T1 已定但需记录其代价：不签名 + 自动更新 = **每次更新后 macOS 可能重新索要辅助功能与屏幕录制权限**。这是 CU 功能最持续的体验成本，会直接变成 issue。落地时要先实测这一现象是否真的发生，再决定是否在更新流程里加「权限检查与引导」步骤。



### 13.6 工程评审采纳记录（2026-11-11）

用户要求将本轮工程评价并入 PRD。采纳的是可验证的工程要求，不作任何公司背书，也不把设计评审写成实现验收。当前实现基线为 `540c543`；本轮未运行产品测试、未改变运行时依赖或机器契约。

| 原方案 / 当前缺口 | 纳入的新要求 | 为什么 | 影响范围 |
|---|---|---|---|
| 核心恢复与授权已有基础，能力层继续扩展 | 保留事件 / 事务、Task/Run、fencing、执行侧授权、凭据与纯编译器 | 避免换模型或框架时重建事实源 | §1.4；events/tasks/jobs/dispatcher/context 的既有边界 |
| 20 条契约用例与部分脚本化安全判据 | 四维结果、目标作用域判据、真实用户任务、正负控与套件版本 | 使分数可解释，防止未完成冒充不安全或假绿 | §10.2–§10.5；eval/checks、cases、报告 schema |
| 文件中心工具、顶层参数校验、单 Run 预算记录 | 通用准入、完整 schema、外发许可、共享预占与取消传播 | 网络 / MCP / 并发不能沿用本地纯读和估算余额语义 | §5.2.3 / §5.6；tools/dispatcher/permissions/loop、C2/C4 |
| 命令策略可用但无实际隔离 | 按文件、网络、凭据、进程树逐项验证，能力不符拒绝 | 审批启动不等于约束后续全部行为 | §8.1.1；exec/execpolicy/credentials、平台后端 |
| 事件信封已定义，历史兼容与数据生命周期证据不足 | payload 版本、迁移中断、备份恢复、忘记 / 删除边界 | 让长期演进不丢数据、不作虚假隐私承诺 | §5.3.5 / §8.5；events/snapshot/replay/recovery、未来 memory |
| 零依赖及确定性裁剪被当作长期能力上限 | 集成依赖经独立 ADR 准入；模型摘要先保存再编译 | 保留可审计性，允许采用维护中的组件与语义压缩 | §3.4 / §7.1；依赖规则、adapter/context/loop；尚未启用新依赖 |
| 运行指标、平台门禁与分发闭环不完整 | 本地 SLO、慢订阅者隔离、四目标与安装升级证据 | 用户体验须由真实运行和交付验证 | §10.6 / §11.1；daemon/host/ipc/UI、CI / 发布 |
| 当前状态与历史记录混写 | 单一现状入口、历史标日期与替代关系、可生成内容校验漂移 | 降低 AI 开发读到矛盾材料的风险 | 本节文档治理规则；KNOWLEDGE / 验收报告 / AGENTS |

**文档治理与实施规则：**

1. PRD 只规定要求与明确的适用阶段；当前状态集中维护在 KNOWLEDGE §2 / §7；历史决策和验收报告保留日期、commit、环境与证据，新结论注明替代关系，不覆盖旧实验事实。
2. 工具清单、事件集合、平台矩阵等可从权威代码 / 配置导出的内容，逐步生成或增加漂移校验；仍须独立核对 CI / 实测证据，不能从“配置了”推导“通过了”。
3. 本轮只纳入文档基线。具体字段、schema、事件集合和依赖变更按 `docs(contract):` 单独提交 / PR 评审；实现同步代码、schema、PRD 与测试，高风险修改仍需独立探针复审。不得据本文自动接受历史未知状态、解除阻断或将未验证事项标完成。

---

## 附录 A：本文与既有 59 项 ADR 的关系

- 凡"选了什么"类 ADR → 降级为技术选型附录，不参与架构约束
- 凡能被 §6 十一条红线覆盖的 → 合并进红线，原 ADR 标 `superseded-by: redline-N`
- 其余（目录结构、CI 流程）→ 移入工程规范，不进架构文档

**目标：架构正文不超过 20 页，红线 11 条，契约 5 个。** 超过就是又回去了。

## 附录 B：契约测试清单（摘要）

1. 各 provider 映射角色/内容块/结束原因/错误；**未知枚举必须报错，不得存 `unknown`**（结束原因、内容块类型等未映射值一律抛 `ModelError`；历史事件里若存过非法 `finish_reason`，读取时抛错而不是洗白成合法值）
2. 按 §5.1 的组合条件派发：规范化 `finish_reason=tool_use`、完整参数、固定版 C2 校验与 assistant turn 持久化全部成立；其他收尾、错误或未知枚举不得触发工具执行
3. 半截 JSON、重复 delta、断流、重复结束事件：不执行
4. 崩溃覆盖四窗口：intent 前、intent 后派发前、派发后 result 前、result 落盘后
5. 幂等键：同键同参重试、同键异参拒绝、同键并发、键过期、旧执行仍在运行
6. 撤销 grant 后拒绝执行；依赖 MCP 注解授权必须失败
7. 换 provider 不改变执行侧授权结果
8. 上下文编译同输入必得同输出（纯函数性）；持久化语义摘要回放不重新生成（新增要求）
9. 通用工具的嵌套参数、资源域、外发许可与固定版本在执行侧核验（§5.2.3）
10. 共享预算并发预占、attempt 幂等结算、未知用量、取消与父子限额（§5.6）
11. 历史版本夹具、迁移中断、完整备份恢复、忘记后恢复旧备份（§5.3.5 / §8.5）
12. 隔离能力逐平台探针，含凭据不继承、未授权出网与后代进程终止（§8.1.1）
13. 判据正负控与四维分离；真实任务和脚本 / harness 样本不可混称（§10.4 / §10.5）
14. 慢订阅者下控制面仍可用、长时间运行与四目标发布证据（§10.6 / §11.1）

新增项是待实现 / 验收的清单，不表示已有测试覆盖。
