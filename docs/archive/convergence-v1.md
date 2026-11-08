# Agent OS 架构收敛 v1

> 输入：15 个研究领域、59 项 ADR、参考项目清单。
> 结论：**停止增加模块。** 接下来只有三件事——冻结协议、分四象限、对 3 个模块做源码级审查。
> 本文档独立完成，不引用未经验证的外部结论。

---

## 0. 诊断：59 项 ADR 真正的问题

不是「ADR 太多」，而是**ADR 的粒度不可执行**。59 项里绝大多数在描述「我们打算分成这几层」「我们选了这个组件」，属于设计意图，不是可判定的约束。真正能防止系统腐坏的决策，特征是**可以被一条测试打红**：

- 「Memory 分四类」——不可判定，写多少 ADR 都不会有人违反。
- 「事件日志 append-only，不设 UPDATE」——可判定，代码评审和 schema 检查能打红。

所以收敛的第一动作不是砍模块，是**把 59 项 ADR 重写成「不可变内核 + 可演进外壳」两层**：

| 层 | 内容 | 变更成本 | 判定方式 |
|---|---|---|---|
| 内核（Immutable Core） | 事件语义、副作用语义、授权语义、协议版本 | 必须有迁移方案 | 契约测试 |
| 外壳（Evolvable Shell） | 模型、框架、检索算法、UI、部署形态 | 随时可换 | 替换不超过 N 人日 |

判据只有一句：**换掉它需不需要改事件日志的历史？** 需要 → 内核；不需要 → 外壳。

按这个尺子过一遍，15 个模块里真正的内核只有 3 个：**执行语义（Sandbox+Tool）、状态语义（Event+Checkpoint）、授权语义（Permission+Approval+Receipt）**。其余 12 个都是外壳。

---

## 1. 四象限：必须自研 / 应该复用 / 可以适配 / 暂时不做

象限划分的唯一依据是「**这东西坏了，我的架构会跟着坏吗**」。

### 1.1 必须自研（坏了架构就坏）

| 模块 | 自研的部分 | 不做什么 | 理由 |
|---|---|---|---|
| Agent Runtime | Kernel 主循环、Run 生命周期、Hooks 挂载点 | 不做通用工作流引擎，不做 DSL | 主循环是唯一能保证「模型只提案、代码做决定」的地方 |
| Tool System | 注册表、envelope、schema 版本绑定 | 不做工具市场 | 工具契约是跨层唯一通道，外包等于把架构交给别人 |
| Permission / Approval | 策略判定、capability 签发、审批状态机 | 不复用任何 SaaS 授权 | 授权必须在执行侧，第三方无法替你保证 INV-3 |
| Action Receipt | 副作用凭据格式与生成时机 | 不做审计平台 | 没有 receipt，副作用就不可对账，第 2 章的红线全失效 |
| Evaluation | 评测集、判定器、回放器 | 不用 LLM 当唯一裁判 | 评测集是唯一能跨模型换代存活的资产 |
| Event Log | 事件 schema、append-only 约束 | 不自研存储引擎（用 SQLite/PG） | 语义自研，存储复用 |

### 1.2 应该复用（直接用，别造）

| 模块 | 复用对象 | 边界 |
|---|---|---|
| MCP | 官方 SDK（client + server） | 只做 adapter 映射，不改协议语义 |
| Observability | OpenTelemetry + GenAI semconv | 自有 envelope 是主，OTel 是导出目标，不是事实源 |
| Credential 存储 | macOS Keychain / Windows CredMan / Linux Secret Service，或 Vault | 自研的只有 broker 的作用域与租约 |
| Sandbox 隔离能力 | Seatbelt / Landlock / seccomp / gVisor | 系统能力，自研隔离 = 自研内核漏洞 |
| Browser 驱动 | CDP（Playwright 或裸 CDP） | 不自研渲染引擎，不自研 DOM 快照算法 |
| Memory 存储 | PostgreSQL / SQLite + 一个向量索引 | 索引是可删可重建的投影 |
| 包管理 | 现有 lockfile 机制 | 不自研依赖解析 |

### 1.3 可以适配（包一层，随时可换）

| 模块 | 适配对象 | 适配层必须提供的 |
|---|---|---|
| 模型 | 各家 API | C1 归一化、能力矩阵、错误映射、续接 opaque 透传 |
| Context Assembly | 策略插件 | `compile(events, budget) → view` 纯函数接口 |
| Compression | 摘要器 | 必须返回 `source_seqs`，可回溯原文 |
| 多 agent 编排框架 | LangGraph / AutoGen 等 | 只允许作为 L3 之上的执行策略，状态机不得存进框架对象 |
| Plugin 加载 | 现有 loader | manifest 校验 + 供应链检查（签名/来源/权限声明） |

### 1.4 暂时不做（v1 明确砍掉）

| 砍掉的东西 | 原因 | 什么时候重新考虑 |
|---|---|---|
| 多 agent 自由对话 / 角色制协作 | 状态不可审计、权限不可证明，收益主要来自 demo | 有了 receipt 和权限域之后，按「权限域」拆，不按角色 |
| Plugin 系统 | Skill + Tool 已覆盖 90% 场景，Plugin 只增加供应链攻击面 | 出现真实的第三方分发需求 |
| Windows Computer Use | 驱动成本高（UIA 不稳定），先做 macOS | macOS 版跑通并被真实使用 3 个月后 |
| 自研向量数据库 | 没有理由 | 永不 |
| 通用工作流 DSL | 会变成另一种「必须长期维护的语言」 | 永不 |
| 自研 sandbox 隔离实现 | 等于自研内核漏洞 | 永不 |
| 跨厂商 agent 互联协议（A2A 类） | v1 没有第二个 agent | 真有跨组织协作时 |

**四个象限的比例应该是 6 : 7 : 5 : 7，自研部分不超过 25%。** 超过这个比例，说明在重复造已有的东西；低于这个比例，说明架构不由自己掌握。

---

## 2. 十一条红线（替代 59 项 ADR 的可判定版本）

每条都能被一条测试打红。这才是 ADR 该有的粒度。

1. 事件日志 append-only，无 UPDATE / DELETE；订正靠追加。
2. 副作用执行前必须已持久化 intent，执行后必须写 result。
3. 上下文是可再生视图；删掉所有视图后必须能从事件重编。
4. 授权只在执行侧判定；任何模型输出、工具输出、MCP 注解都不构成授权依据。
5. 副作用结果未知时禁止重放；只有 `none_applied` 可安全重试。
6. 每个副作用必须产出 Action Receipt，且 receipt 可独立核对。
7. 跨层只传版本化契约对象；SDK 对象、异常实例不得越界。
8. 工具 schema 版本变化即视为新工具，不自动升级。
9. 能力声明只用于路由，不用于授权。
10. 评测的 replay 与 evaluate 不得混称；安全门槛不可被其他分数抵消。
11. 任何一处「崩溃后无法判定状态」的设计，视为 bug，不视为可接受风险。

---

## 3. 协议：把 11 个收敛成 5 个冻结 + 4 个稳定 + 2 个内部

**先说反对意见：把 11 个协议同时冻结是错的做法。** 协议的数量本身就是维护成本，而且它们之间必然交叉引用，一次冻结 11 个等于冻结一份 500 页的规格，没人读得完，改动时也没人敢动。

正确做法是**按「谁先需要它」冻结**，而不是按「谁最完整」冻结。

### 3.1 分级

| 级别 | 协议 | 冻结时机 | 依据 |
|---|---|---|---|
| T0 立即冻结 | Event、Tool、Permission+Approval（合并）、Action Receipt | M0 之前 | 没有它们，代码写完就要重构 |
| T1 稳定演进 | Agent Run、MCP Adapter、Checkpoint、Agent Message | M2 之前 | 有实现后才能定细节 |
| T2 内部约定 | Skill Manifest、Plugin Manifest | 随需求 | 格式问题，不是架构问题 |

### 3.2 T0-1 Event Protocol

```
Event {
  seq            uint64     // 单调递增，主键，不可跳号
  run_id         id
  task_id        id?
  type           enum       // intent|result|model_call|approval|receipt|compaction|job|checkpoint|audit
  ts             int64      // 单调时钟 + 墙钟双写
  contract_ver   semver
  actor          {kind, id}  // 谁产生的：core|harness|tool|human|system
  payload        object|ref  // >256KiB 落盘，此处留 ref
  payload_hash   sha256
  parent_seq     uint64?     // 因果前驱
  idem_key       string?
}
```

不可变约束：`seq` 连续、无 UPDATE/DELETE、`payload_hash` 必须可重算。**事件类型是封闭枚举**，新增类型必须升 `contract_ver` 并写迁移说明。

### 3.3 T0-2 Tool Protocol（含 Agent 侧收口）

沿用 PRD 的 C2，这里补三条**之前没定死、但一定会咬人**的规则：

- `schema_hash` 必须覆盖整个 `input_schema` 的规范化 JSON，不只是字段名。否则「加了个 `additionalProperties: false`」不会让工具版本升级，但语义变了。
- `side_effect_level` 由**工具实现者**声明，由**执行侧**验证：执行侧必须抽查（同类工具的历史行为、是否真的幂等），不能全信声明。
- 工具结果必须是 `status` + `effect_state` 二元组，禁止用单个 `ok/error` 表达。

### 3.4 T0-3 Permission + Approval Protocol（合并）

原清单把它们分成两个协议，是错的：它们共享同一个 digest 和同一套状态机，拆开必然产生「权限通过了但审批没记录」的缝。

```
Grant {
  grant_id, subject, action, resource, data_domain,
  quota, ttl, approval_ref?, issued_by, issued_at,
  policy_ver, digest
}

Approval {
  approval_id, grant_request, digest,
  state: proposed|pending|approved|rejected|expired|revoked|consumed|superseded,
  decided_by, decided_at, expires_at, policy_ver, prompt_ver
}
```

约束：
- `digest` 用 RFC 8785 canonical JSON，与工具侧 `args_hash` 同一算法。
- 审批人与请求主体必须不同身份。
- `consumed` 只能发生一次，并发消费必须以数据库唯一约束保证，不靠代码判断。
- 参数、资源、工具版本、policy、prompt 任一变化 → `superseded`。

**凭据不混进这里**（借鉴 OpenHands）：`CredentialBinding{cred_id, subject, scope, expires_at, activation} ` 是独立对象，有自己的激活时机与过期，晚激活即报错。授权靠 Grant，取凭据靠 Binding，两件事不能合并，否则审批状态机会被凭据刷新污染。

### 3.5 T0-4 Action Receipt Protocol

**这是原清单最大的一处遗漏**——11 个协议里没有「副作用凭据」。没有它，前面所有「副作用可控」的说法都无法验证。

```
Receipt {
  receipt_id, run_id, task_id, call_id, attempt_id,
  action, normalized_args_hash, resource, effect_state,
  before_ref?, after_ref?,        // 可 diff 的前后状态引用
  backend, backend_execution_ref, // 后端自己的执行 ID
  evidence[],                     // 测试结果、diff、日志片段
  issued_at, issuer, sig?         // 同机场景可省，跨机必须有
}
```

生成时机：`result` 事件写入的同一事务内。约束：**receipt 缺失即视为副作用未确认**，恢复流程必须按 `effect_state = unknown` 处理。这样 receipt 就不是「审计锦上添花」，而是执行语义的一部分。

### 3.6 T1 协议（列出骨架，不展开）

- **Agent Run Protocol**：`Run{run_id, task, budget, policy_ref, state, started_at, ended_at}`；状态机 `created→running→waiting_approval→suspended→done|failed|cancelled`。**不定义「agent 之间如何协作」**，那是 Agent Message 的事。
- **MCP Adapter Contract**：只定义映射规则——MCP tool → `ToolDefinition`、`isError` → `status`、注解 → 不参与授权、取消 → 传到执行侧。不重新定义 MCP。
- **Checkpoint Protocol**：`Checkpoint{cp_id, seq, run_id, state_digest, pending_intents[]}`。**checkpoint 不是状态**，只是「从哪个 seq 重建」的指针；不存业务状态快照（否则又回到框架私有 checkpoint 的老路）。
- **Agent Message Protocol**：`Msg{from, to, task_id, kind, payload_ref, reply_to, budget}`；禁止自然语言自由讨论，`kind` 封闭枚举。子 agent 分两种模式（借鉴 Deep Agents）：`isolated` 只接收任务单、不继承父上下文；`fork` 延续父对话。默认 must be `isolated`——继承上下文是特例，不是常态。

### 3.7 T2（内部约定）

- **Skill Manifest**：`name, version, description, inputs, outputs, requires_tools[], requires_caps[], entry`。本质上是一个「给模型看的说明书 + 给调度器看的依赖声明」。
- **Plugin Manifest**：v1 不做，先只留字段占位：`name, version, source, sig, permissions[], entry`。

---

## 4. 源码级审查：先定方法，再动代码

### 4.1 为什么不能「审查 6 个项目」

用户列了 6 类、15 个以上项目。以「深度」为标准，一个仓库的机制级审查至少需要：读入口、读主循环、读状态持久化、读错误路径、跑一次最小复现。**按这个标准，一轮能做完的是 2–3 个，不是 15 个。**

所以先定：**第一轮只审 3 个，每个只回答 3 个问题。**

### 4.2 审查模板（每个项目只问这三个）

1. **状态放在哪？** 是持久化事件/数据库，还是内存对象 + 框架 checkpoint？→ 决定我们能不能复用它的恢复语义。
2. **权限在哪判？** 执行侧、工具内、还是 prompt 里？→ 决定它的安全模型是否可借用。
3. **取消真的到达执行侧了吗？** 还是会话层标记取消、子进程继续跑？→ 决定长任务是否能可靠中止。

这三个问题恰好对应我们的三条内核语义，答完就能给出「可借鉴 / 只可参考 / 不可借鉴」的结论。

### 4.3 审查对象与代码基线

拉取的 commit（`--depth 1`，2026-11-08）：

| 目录 | 仓库 | 说明 |
|---|---|---|
| `refs/strands` | strands-agents/sdk-python | 960 个 py，含 sandbox/session/middleware |
| `refs/deepagents` | langchain-ai/deepagents | 1235 个 py，monorepo |
| `refs/mcp-python-sdk` | modelcontextprotocol/python-sdk | 870 个 py |
| `refs/oh-sdk` | OpenHands/software-agent-sdk | 真 runtime 在这，**不在 OpenHands app 仓库** |

重要发现：`OpenHands/OpenHands` 主仓库现在只有 17 个 py，runtime 已拆到 `software-agent-sdk`（`openhands-agent-server` + `openhands-sdk`）。**审 OpenHands 必须审 oh-sdk，审主仓库是白干。**

### 4.4 审查结论

#### Strands：状态可外置，权限和取消都不达标

| 问题 | 实际实现 | 判定 |
|---|---|---|
| 状态放哪 | `session/` 有 `SessionRepository` 抽象，file / S3 / snapshot 三种后端；`SessionManager` 本身是 `HookProvider`，靠 `register_hooks` 在生命周期挂点上 `append_message` | **可借鉴**（状态外置成接口 + hook 挂点） |
| 权限在哪判 | 没有执行侧权限模型。只有 `interrupt()` 做 HITL——**工具主动申请确认**，不是执行前强制校验（`middleware/stages.py:190-213`） | **不可借鉴** |
| 取消到执行侧了吗 | `cancel_signal` 是 `threading.Event`，协作式；中间件只能观察，工具不自己检查就停不下来。只有进程型工具有真实终止：`sandbox/stream_process.py:34-40` 的 `os.killpg(pid, SIGKILL)`，**没有 SIGTERM 优雅期** | **部分可借鉴**（进程组 kill） |

补充：持久化粒度是「消息」不是「事件」，没有 intent/result 配对，所以它的恢复语义停在「对话能接着聊」，不是「副作用可对账」。这正是我们不必抄它的地方。

#### Deep Agents：三个问题全是负面样本，但有一个亮点

| 问题 | 实际实现 | 判定 |
|---|---|---|
| 状态放哪 | **LangGraph checkpointer**（`graph.py:304` 参数 `checkpointer`）。`libs/ARCHITECTURE.md` 自己写明「LangGraph is the runtime… saves checkpoints, pauses or resumes runs through interrupts」 | **不可借鉴**——这正是红线 11 禁止的「业务状态存进框架私有对象」 |
| 权限在哪判 | **写在 prompt 里的自然语言契约**：`_DELEGATION_CONTRACT` 写着「Check get_agent_tools for required capabilities; unknown inventories do not establish access」 | **不可借鉴**——能力清单是文本，不是执行侧强制 |
| 取消到执行侧了吗 | `HumanInTheLoopMiddleware` 明确标注 Requires a checkpointer（`middleware/subagents.py:181`），中断与框架状态强耦合 | **不可借鉴** |

**唯一亮点值得吸收**：`SubAgent(TypedDict)` 声明式定义（`middleware/subagents.py:111`），且区分两种模式——默认 `isolated`（子 agent 只收到任务单，不继承父对话）、`mode="fork"`（延续父对话）。这个二分比笼统的「子 agent」精确，建议写进 Agent Message Protocol。

#### MCP python-sdk：取消语义确认了我们的判断

- `cancel_requested` 是 `anyio.Event`（`src/mcp/shared/context.py:44-47`）。peer 发 `notifications/cancelled` 只是**置位**，handler 自己检查。**协议层面就承认「取消请求 ≠ 执行停止」**——这直接支持红线 5 和 `effect_state = unknown` 的设计。
- `isError=true`（工具执行失败）与 JSON-RPC 协议错误分开处理，见 `server/_otel.py:67`、`client/client.py:865`、`server/mcpserver/tools/base.py:202`。方向与我们的 `status` / `effect_state` 二分一致。
- 值得抄一条：`LATEST_PROTOCOL_VERSION` 与 `SUPPORTED_PROTOCOL_VERSIONS` **分离**，走协商。我们的 C1/C2 现在只写了单一 `contract_version`，建议改为「声明支持区间 + 协商」。

#### OpenHands agent-server：四家里唯一把「状态外置」做干净的

| 问题 | 实际实现 | 判定 |
|---|---|---|
| 状态放哪 | `EventLog`（`openhands-sdk/.../conversation/event_store.py:38`）：**一个事件一个文件**，磁盘索引即 durable seq，`parent_id` 链，flock 写锁，并且**主动上报丢文件空洞**（holes）。注释还写明 NFS 上 flock 不可靠 | **强可借鉴**——几乎就是 C3 的工业实现 |
| 并发/租约 | `ConversationLease`（`conversation_lease.py:101-174`）跨实例租约 + **PID 存活校验**；`RunSlot`/`_RunPermit` 按会话限并发，超限抛 `ConversationRunLimitExceeded` | **强可借鉴**——F8 作业表直接照这套 |
| 凭据 | `credential_binding.py` + `CredentialBindingActivationTooLate`：凭据绑定有**激活时机**约束，晚激活即报错；`_secrets_exposure.py` 与 `_secret_redaction.py` 分开 | **可借鉴**——凭据应有独立生命周期，不该塞进 grant |
| 取消到执行侧了吗 | `bash_service.py:414-424`：**SIGTERM → 整个进程组 → 优雅期 → SIGKILL**，超时路径复用同一函数（:479） | **强可借鉴**——与 PRD F1 一致，且说明这是踩过坑才这么写的 |

### 4.5 对现有设计的修正建议

审查改了我三处判断：

1. **协议版本改成区间协商**（来自 MCP）。单一 `contract_version` 在多实现并存时必然卡死，改成 `supported: [1.0, 1.3]` + 协商落定。
2. **子 agent 分 isolated / fork 两种模式**（来自 Deep Agents）。虽然它整体不可借鉴，这个二分是有效的：隔离型只要任务单，fork 型继承上下文。补进 Agent Message Protocol。
3. **凭据与授权分离**（来自 OpenHands）。`CredentialBinding` 独立于 `Grant`，有自己的激活时机和过期，不混进审批状态机。

另一条不改但值得记录的：**四家里只有 OpenHands 取消了 SIGTERM 优雅期之外的所有花活**——没有沙箱框架、没有编排图、没有角色系统，就是一个 HTTP 服务 + 事件目录 + 租约。这和 §1 的四象限判断一致：**越薄的 runtime 越活得久**。


---

## 5. 执行顺序（按依赖，不按模块编号）

```
第 1 步（1 周）   冻结 T0 四协议 + 写 11 条红线对应的测试骨架
第 2 步（2 周）   M0：单 run 闭环，事件可重放
第 3 步（2 周）   Receipt + intent/result，崩溃恢复
第 4 步（2 周）   权限与审批接入，越权测试
第 5 步（2 周）   接第二个 provider + 第二个沙箱后端（验证可替换）
第 6 步（2 周）   评测集 20 条 + replay/evaluate 分离
第 7 步（持续）   T1 协议随实现冻结；源码审查按 §4.3 滚动
```

**关键判断：协议冻结必须先于编码，但不能等所有协议都想清楚。** T0 四个不定，M0 写完就返工；T1 四个现在定，纯属猜。

---

## 6. 与既有 59 项 ADR 的关系

不是推翻，是分层：

- 59 项里凡是「选了什么」的，全部降级为 **附录：技术选型记录**，不参与架构约束。
- 凡是能被 §2 十一条红线覆盖的，合并进红线，原 ADR 标记 `superseded-by: redline-N`。
- 剩下的（例如具体目录结构、CI 流程）移到工程规范，不进架构文档。

目标：**架构文档正文不超过 20 页，红线 11 条，T0 协议 4 个。** 超过就是又回去了。
