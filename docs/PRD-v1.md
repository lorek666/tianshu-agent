# Desktop Agent OS — PRD v1.0

> 本文合并自：`convergence-v1.md`（架构收敛）、`general-agent-v2.md`（通用 agent 与执行模型）、`review-8-projects.md`（8 项目核对）、`agent-core-v1-prd.md`（首版 PRD）。
> 本文取代以上四份，作为唯一有效版本。
> 范围变更：**v1 不做图片与视频生成**，但 E2 作业模型的协议与接口预留在 v1 定义、不实现。
> 决策状态：D1–D7、T1 全部已定（D6 于 §4.5 定稿）。见 §13。

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
2. `Artifact` 表与内容寻址存储（表见 §4.1，v1 要求见 §7 F11）——**独立的产物表与截图存储尚未实现**；大工具输出的 8 KiB 落盘是 §7 F5 的要求，也尚未实现（工具输出现在只做截断并打 `truncated` 标记）。v1 已有的内容寻址只有两处：事件 payload 超过 256 KiB 落盘（`EventLog.blob_dir`）、写前副本（`SnapshotStore.blob_dir`）
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
| `Memory` | 长期记忆（**v1.1**，见 §7 的 F10） | `id, scope, owner_id, content, source, confidence` |
| `Event` | 事件日志 | `seq, run_id, type, actor, payload_ref, payload_hash, parent_seq` |
| `Checkpoint` | 恢复指针（**v2**，见下方说明） | `id, run_id, seq, state_digest, pending_intents[]` |

**三行 `v1 不建表` 怎么读**：`Agent` / `AgentSession` / `Conversation` 记录的是概念模型，不是待建的表——`Agent` 的职责由 `Task` 承担（Task 才是用户可见单位），`AgentSession` / `Conversation` 的职责由 `Run` + 运行游标（`RunCursor`）承担。

**`Artifact` 与 `Checkpoint` 属 v2**：v1 的内容寻址只有两处——事件 payload 超过 256 KiB 落盘（`EventLog.blob_dir`）、写前副本（`SnapshotStore.blob_dir`）；**没有**独立产物表、没有截图存储，工具输出现在是截断并打 `truncated` 标记，超 8 KiB 落盘仍是 §7 F5 的要求、尚未实现。`Checkpoint` 在 v1 的等价物是「事件日志 + 游标」（`run.cursor` 事件 + `RunCursor`），所以 v1 不需要独立的 checkpoint 表。

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

**版本策略（修正自 MCP 的做法）**：握手用**区间协商**——每端声明 `supported: [1.0, 1.3]`，取交集最高版。但**每条消息与每条持久化事件必须记录协商后选定的精确版本**；历史回放时直接使用记录值，不得重新协商（否则回放结果会随实现版本漂移）。

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

**新方案**：两层——**族**（`type` 的名字空间前缀；13 个在用 + 3 个声明未实现）→ **具体类型注册表**（41 个，机器可校验，写入侧强制）。

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

#### 5.3.2 注册表：13 个族 / 41 个具体类型

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
| `task` | Task：创建与改目标（goal_revision 推进） | `task.created` / `task.revised` |
| `tool` | 工具调用：intent、result、未派发（C2） | `tool.intent` / `tool.not_dispatched` / `tool.result` |
| `undo` | 撤销：完成与中断 | `undo.completed` / `undo.failed` |

**未实现的族**（声明过、作为事件类型零命中，不参与校验）：

| 族 | 原意 | 状态 |
|---|---|---|
| `compaction` | 上下文压缩检查点 | 未实现：当前没有压缩机制，也没有对应事件 |
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

**未封闭**（本版不做，不能假装做了）：

- **每种类型自己的 payload 字段形状**：现在统一是 `object`。这是 C3 真正冻结前的最后一层。
- `ts` 的"单调时钟 + 墙钟双写"：代码只写墙钟毫秒。
- 事件导出格式的版本协商（§5 的版本策略）尚未落地到导出上。


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
- **保留未使用**：`dispatch.denied`（`DispatchDenied` 的默认编号，现无调用方使用）
  与 `c4.unhandled`（策略分类的末尾兜底，当前动作集合已全覆盖）。它们保留在集合里，
  因为都是防御性兜底：真被触发时仍要能记下一条拒绝，而不是让拒绝路径自己崩掉。
  这两条在表里明确标注，不参与"有产生路径"的归类。

**这些是规则编号，不是事件类型。** 不要登记进 `events.EVENT_TYPES`——事件类型
注册表只回答"代码会写哪些事件"，混进规则编号等于让它替不存在的类型作证。

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
| `c4.no_resource` | 工作区阻断 | 动作没有声明任何资源。 | 工具实现没声明资源，属代码 bug；报 issue。 |
| `c4.outside_workspace_unsupported` | 工作区阻断 | 目标在工作区之外，且当前后端不支持域外操作。 | 把目标移进已绑定目录，或换支持域外操作的后端。 |
| `c4.readonly_binding` | 工作区阻断 | 目标目录是只读绑定。 | 换成可写目录，或建任务时改用可写绑定。 |
| `dispatch.workspace_blocked` | 工作区阻断 | 工作区被阻断检查拦下（未知副作用未澄清）。 | 先处理阻断原因（`recover` 核实未定的副作用），再重试。 |
| `c4.auto` | 判定结果（非拒绝） | 判定结果：目录内读/新建，自动放行。 | 无需操作：已自动放行。 |
| `c4.delete` | 判定结果（非拒绝） | 判定结果：删除，需审批。 | 需要人工确认：批准删除。 |
| `c4.outside_workspace` | 判定结果（非拒绝） | 判定结果：目录外访问，需审批。 | 目录外访问需人工确认：批准后执行。 |
| `c4.exec` | 判定结果（非拒绝） | 判定结果：执行命令需要人工审批（任务已显式开启 exec 策略）。 | 确认命令与参数无误后批准；不确定就拒绝。 |
| `c4.overwrite` | 判定结果（非拒绝） | 判定结果：覆盖已有内容，属删除类操作，需审批。 | 需要人工确认：`approvals list` 后批准。 |
| `c4.unsupported` | 兜底 | 动作不在支持的动作集合内。 | 换个支持的动作/工具；这多半是工具注册写错了，报 bug。 |
| `dispatch.refused` | 兜底 | 守卫内复核抛出未分类异常时的兜底拒绝。 | 未知原因被拒；把 `reason` 连同这条日志报 bug。 |
| `c4.unhandled` | 保留未使用 | 保留未使用：策略分类的兜底分支，当前 SUPPORTED_ACTIONS 已全覆盖。 | 保留未使用；真看到它说明策略分类漏了分支，报 bug。 |
| `dispatch.denied` | 保留未使用 | 保留未使用：DispatchDenied 的默认编号，当前无调用方使用。 | 保留未使用；真看到它说明有调用方漏传编号，报 bug。 |

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

**预算账本规则（否则并发会把同一份余额花两次）：**

1. 派发前**原子预占**额度，结束后按最终 usage 结算
2. 并发请求不能花同一份余额
3. 断流且无 usage 时，保留"未决占用"而不是按零结算
4. 预算耗尽只保存状态并通知用户，**不得再调用模型生成"总结"**
5. 辅助调用额度耗尽后同样禁止发出

**验收**：主任务额度耗尽后，摘要调用必须被拒发。

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
| **F2** | 沙箱与 policy | 后端可插拔：P0 交付策略引擎与后端接缝，隔离按 TECH-ARCH §6 如实分级——macOS 只承诺 L1（文件代理）+ L2（进程树），L3 标 `unsupported`；不把 Seatbelt / Landlock / 容器写成 v1 必须交付。policy 声明式、默认全拒绝、`deny` 优先。后端执行不了必须返回 `unsupported`，禁静默放宽 | PRD v1 |
| **F3** | 事件日志与恢复 | 见 §5.3。恢复扫描"有 intent 无 result"的调用，按 `side_effect_level` 处置 | OpenHands EventLog |
| **F4** | 工具系统 | 注册表 + C2 envelope + transport adapter（进程内/HTTP/gRPC/MCP）。MCP 只是 adapter 之一 | PRD v1 + MCP SDK |
| **F5** | 上下文编译 | `compile(events, budget) → view` 纯函数；分区预算；压缩产物带 `source_seqs` 与 `budget_version`；工具输出超 8 KiB 落盘 | PRD v1 |
| **F6** | 授权与审批 | 见 §5.4。执行侧二次校验 digest；一次性消费；风险分级自动放行 | PRD v1 + Zed |
| **F7** | 评测与回放 | 区分 `replay`（审计，不重调模型）与 `evaluate`（换模，干净 fixture）；20 条 case 覆盖正常/工具错误/超时恢复/恶意输入/审批边界；安全门槛为硬门槛。**由 §11 的 M4 承接** | PRD §10.3 + §11 M4 |
| **F8** | 任务与运行 | Task/Run 分离、状态机、**作业表**（`job_id, state, lease, heartbeat, timeout, cancel, output_path`）。租约需 PID 存活校验 | OpenHands `conversation_lease.py` |
| **F9a** | 模型准入 | CapabilityProfile 登记、必需能力缺失即拒绝、schema/上下文核验、有界重试（默认 3 次 attempt） | PRD v1 |
| **F11** | 产物存储与可回滚工作区 | 内容寻址 `{artifacts_root}/blobs/<sha256>`，状态只存 ref；**首次写入前取写前副本（preimage）**，按 Run 生成 manifest（变更路径 + 前后哈希 + 重命名/删除记录），reflink/COW 优先、不支持则复制 + 内容去重。保留策略见 §8.4 | Deep Agents `_blob_offload.py` |

| **F12** | 凭据 | 按 `(provider, capability_class)` 分域。v1 交付的是**凭据接缝 + 环境变量后端 + 全链路脱敏**：密钥不进 prompt、不进事件日志，错误信息与 `repr()` 一律打码（含 HTTP 响应体回显 key 的情况）。**OS Keychain 在 v1 未实现**：请求它一律报 `unsupported`，绝不静默降级成环境变量；环境变量后端只算开发/CI 用（对子进程可见、可能进 core dump、可被 `ps -E` 看到）。Keychain 留给后续工单（零依赖下需 ctypes 直调 Security.framework / `CredReadW`，且必须有可验证的平台） | OpenHands `credential_binding.py` |
| **F13** | 成本可见 | 每次调用回 `{provider, model, units, unit_price_ver, cost}`，UI 实时累计展示。**用户自己出钱，必须让他知道烧了多少** | 由 D4 决定 |

### P1（v1.1）

| ID | 功能 | 关键要求 |
|---|---|---|
| **F10** | 记忆与知识 | 四类分离：事实→SQLite、文件→内容寻址、历史→事件日志、语义→FTS+向量（可重建投影）。写入需去重/来源/作用域/过期 |
| **F9b** | 动态路由 | 静态准入之上做成本/延迟排序、熔断、健康探测 |
| **F14** | 可观测性 | 自有 envelope → 导出 OTel GenAI semconv；原始数据与 trace 分离。**普通用户界面只展示翻译后的人话，原始 trace 隐藏** |
| **F15** | 开源合规 | 许可证固定（Apache-2.0）、依赖许可证扫描进 CI、SBOM 生成、无闭源运行时依赖 |

### P2（v2）

- **E2 生成作业**：`JobResult` 落地、队列、产物预览、按次计费
- 多 agent（仅按权限域 / 副作用域拆，任务单 + 结果文件 + 验收）
- Windows Computer Use
- 子 agent 双模式：`isolated`（只收任务单）/ `fork`（继承上下文），默认 isolated

---

## 8. 权限模型

### 8.1 授权模型：策略判定点唯一，强制执行点必须独立

**先明确威胁边界**（上一版把这条写成了"安全契约"，过度承诺了）：

| 威胁 | v1 是否防 | 说明 |
|---|---|---|
| 模型越权 | ✅ | 这是主要威胁 |
| 恶意提示词注入 | ✅ | |
| 恶意工具输出 / 不可信 MCP 注解 | ✅ | |
| 失控 worker（跑飞、想越界） | ✅ | |
| **本机用户篡改自己的程序** | ❌ 不防 | 开源后任何人都能改代码；这不是 v1 能解决的，也不该宣称能解决 |
| **任意 shell 代码** | ⚠️ 降级 | 见下 |

**结构与职责：**

```
Tool Dispatcher    ← 唯一【策略判定点】（decide once）
      ↓ 签发 grant（绑定 action + canonical resource + args_digest
                   + lease_epoch + attempt_id + TTL + 消费次数）
Desktop Host       ← 独立【强制执行点】：重新解析路径/目标、亲自打开句柄
      ↓ 只把【句柄】交给 worker，不给原始路径与凭据
Sandbox Worker     ← 执行者：拿到的是句柄，不是权限
```

关键点：**Host / Worker 不重新判定策略**（多一套实现就要同步，这是上一版反对的），但它们**必须独立强制执行**——不是只检查"grant 字段存在"，而是用句柄能力模型从物理上使越界不可能发生。

**grant 认证**：通过认证 IPC 下发并带完整性校验；worker 不继承 Keychain/DPAPI、网络与任意文件权限。

**两条必须写在 UI 上的诚实声明：**

1. **未使用有效 OS 沙箱的任意 shell 执行**，标为「受信任执行」，不得向用户宣称它仍受目录 capability 限制。macOS 上尤其如此（见 §6）。
2. 需要强保证的任务，改用**无凭据 + 副本工作区**的容器后端。

这样 M1 的 kill 测试才是在验证一套恢复协议，而不是碰运气。


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

- 未授权副作用
- 越权数据访问
- 敏感数据进入 trace
- 跳过必需审批
- 重复非幂等副作用
- 恢复后状态不一致

### 10.3 v1 评测集

20 条 case，覆盖五类，happy path 不超过半数：正常成功、工具错误、超时恢复、恶意输入、审批边界。每条件 ≥ 3 次运行，报逐次结果。

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



---

## 附录 A：本文与既有 59 项 ADR 的关系

- 凡"选了什么"类 ADR → 降级为技术选型附录，不参与架构约束
- 凡能被 §6 十一条红线覆盖的 → 合并进红线，原 ADR 标 `superseded-by: redline-N`
- 其余（目录结构、CI 流程）→ 移入工程规范，不进架构文档

**目标：架构正文不超过 20 页，红线 11 条，契约 5 个。** 超过就是又回去了。

## 附录 B：契约测试清单（摘要）

1. 各 provider 映射角色/内容块/结束原因/错误；**未知枚举必须报错，不得存 `unknown`**（结束原因、内容块类型等未映射值一律抛 `ModelError`；历史事件里若存过非法 `finish_reason`，读取时抛错而不是洗白成合法值）
2. 只有 `tool_calls_ready` 可提交调用；`length/refusal/content_filter/cancelled/error/unknown` 一律拒绝
3. 半截 JSON、重复 delta、断流、重复结束事件：不执行
4. 崩溃覆盖四窗口：intent 前、intent 后派发前、派发后 result 前、result 落盘后
5. 幂等键：同键同参重试、同键异参拒绝、同键并发、键过期、旧执行仍在运行
6. 撤销 grant 后拒绝执行；依赖 MCP 注解授权必须失败
7. 换 provider 不改变执行侧授权结果
8. 上下文编译同输入必得同输出（纯函数性）
