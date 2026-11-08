# 对「8 个项目源码级研究」的独立核对报告

> 方法差异：**你的报告是文档级**（自述未克隆、未运行）；**本报告是代码级**——本地已克隆 strands、deepagents、OpenHands（app + software-agent-sdk）、mcp-python-sdk、pydantic-ai 共 6 个仓库，另通过 GitHub API/raw 核验了 Zed、Mem0、Cua、thClaws。
> 抓取时间：2026-11-08。所有行号、枚举、文件名均为实测。

---

## 0. 先说结论

**你那份报告的事实准确率很高，8 条里 7 条可以确认属实**，不需要推翻。真正的问题不在准确性，在**覆盖面**：

1. **8 个项目里，0 个覆盖你的产品需求中「图像 / 视频生成」这一半。** 这不是遗漏一两个项目，是整个样本集选偏了——它们全是桌面/编码/记忆类，没有一个是媒体生成引擎。你的需求里有一条是硬需求，样本集里没有对应参照物，于是你的架构里也不会出现它。
2. **Deep Agents 里恰好有两个媒体机制，你的报告没提。** 一个处理「模型怎么读视频」，一个处理「大二进制产物怎么不进上下文」。这两个是你做媒体生成时最先要用到的。
3. **有三处设计判断我认为要改**，其中「权限在三层分别检查」这条会埋下长期不一致。

---

## 1. 逐条核对

| 项目 | 你的结论 | 核对 | 依据 |
|---|---|---|---|
| Strands | Agent Loop / Hook / 预算 / 取消 / 重试 | **属实**，且比你说的更细 | 仓库确为 monorepo，`harness-py` / `harness-ts` / `strands-py` / `strands-ts` 四目录并存 |
| Deep Agents | Middleware / Backend / 子 Agent / 渐进 Skill | **属实** | `libs/deepagents/deepagents/middleware/` 下确实有 `permissions.py` `filesystem.py` `memory.py` `skills.py` `summarization.py` `subagents.py` `patch_tool_calls.py`；`backends/` 有 `sandbox.py` `local_shell.py` `protocol.py` `state.py` |
| OpenHands | 已多仓库化，runtime 在 software-agent-sdk | **属实**，且很关键 | `OpenHands/OpenHands` 主仓库只有 **17 个 .py**；`software-agent-sdk` 里才有 `openhands-agent-server` 与 `openhands-sdk` |
| Cua | Computer Use Driver，Tree + Pixels | **属实（仓库层）** | `trycua/cua` 31,973 星，顶层 MIT；子目录许可差异需逐个核 |
| Zed | 工具权限模型、ACP 外部 Agent | **属实** | `crates/agent/src/tool_permissions.rs`、`sandboxing.rs`、`agent.rs`、`thread.rs` 四个路径全部存在（HTTP 200） |
| thClaws | Rust Harness，多 Agent 桌面 | **属实，但你的报告写成「待核实」** | `thClaws/thClaws` 存在，**1,254 星**，非幽灵项目。但其 `crates/core/src/*` 路径我未逐条核，仍属待核 |
| Mem0 | 分阶段抽取、作用域、去重 | **属实** | `mem0/memory/main.py` 实测 **3,916 行**（你说「超过 3000 行」，对） |
| Phoenix | Trace / Eval / Dataset | **未核**（本轮未拉源码） | — |

**没有一条是编的。** 这一点值得肯定——很多 AI 生成的仓库路径清单会凭空捏造文件。

---

## 2. 五处需要精确化的地方（真实代码）

### 2.1 StopReason 是 13 值封闭枚举，不是「结构化数据」四个字

你写「Stop Reason 必须是结构化数据」。实际是（`strands-py/src/strands/types/event_loop.py:50-63`）：

```python
StopReason = Literal[
    "cancelled", "checkpoint", "content_filtered", "end_turn",
    "guardrail_intervened", "interrupt",
    "limit_output_tokens", "limit_structured_output_attempts",
    "limit_total_tokens", "limit_turns",
    "max_tokens", "stop_sequence", "tool_use",
]
```

并且把 4 个 `limit_*` 单独聚成一个 `_LIMIT_STOP_REASONS` 集合。**这个分组才是它能正确归类错误的原因**——预算耗尽和模型主动停、和内容被过滤，是三件不同的事，混在一个 `finish_reason` 里就没法做降级和计费。

直接影响：我们的 `finish_reason` 必须至少区分这 13 类，不能只写 `done / error`。

### 2.2 Limits 不是「最大轮数 + token 预算」两个数，且有继承语义

真实定义（`strands-py/src/strands/types/agent.py:150`）：

```python
class Limits(TypedDict, total=False):
    turns; output_tokens; total_tokens; structured_output_attempts
```

**关键在注释里**（150-162 行）：limits 会**传播进嵌套调用**（agent-as-tool、Graph、Swarm），且「谁先到谁生效」；子调用也可以设自己的 limits，取先到者。但辅助调用（摘要、路由、记忆抽取、HITL、web-fetch）**只计 tokens、不受 limits 约束**。

这条比「设置 max_turns」重要得多：**做通用 agent 时，子 agent 和辅助调用最容易被做成预算黑洞。** 我们必须显式规定：辅助调用是否计入预算、子 agent 是否继承父预算。

### 2.3 Deep Agents 的 middleware 真实清单（含你没提的三个）

除你列的以外，还有：`_blob_offload.py`、`_video.py`、`_prompt_caching.py`、`_message_eviction.py`、`_overflow_clip.py`、`_fs_interrupt.py`、`_tool_exclusion.py`、`rubric.py`、`unsupported_content.py`、`async_subagents.py`。

**这十个文件里有两个是你做媒体产品必须抄的，见 §3。**

### 2.4 OpenHands 的 17 个 py 文件本身就是最强论据

你说「不能把旧版单体架构当现在的架构」——这句话的正确性不需要读文档，读文件数就够了：主仓库 17 个 `.py`，`src/api/runtime-service/agent-server-runtime-service.ts` 一个 TS 文件，其他全是 electron/前端。**runtime 早搬走了。**

### 2.5 thClaws 该从「待核实」升级为「已核实存在，机制待核」

1,254 星，MIT/Apache 双许可。但它的价值判断要降级：**1,254 星意味着它没有经过大规模生产验证**，把它和 OpenHands（事件流跑了多年）并列当「重要参考」是错的。建议：只借它的「Agent 即独立工作单元 + 文件型记忆 + OS 级 confine」三点，其余不看。

---

## 3. 你漏掉的两个机制（都在 Deep Agents 里，都直接关系媒体需求）

### 3.1 `_video.py`：模型怎么「看」视频

它的做法（文件头注释原文）：把视频读取**复用 `read_file` 的 `offset` / `limit` 参数，但按秒解释**；解码指定时间窗，按固定采样率抽帧，输出**交错的 text + image 内容块**，每帧带时间戳。

```python
"""Video frame extraction for filesystem reads.
... decodes a contiguous slice of the source (`offset`-seconds skip,
`limit`-seconds window) and emits sampled frames at the configured sampling
rate. The output is a list of interleaved text+image content blocks so the
model sees per-frame timestamps alongside the JPEGs. """
```

**这是「通用 agent 处理视频」的第一块砖。** 用户上传一段视频让你「按这个风格生成」，你必须先把视频变成模型能读的东西——不是把整个视频塞进上下文（不可能），是**按时间窗抽帧 + 带时间戳的图文交错块**。

采样率固定、窗口由 agent 用 `offset/limit` 控制——这个设计决定值得照抄：**不暴露采样率给模型，只暴露时间窗。**

### 3.2 `_blob_offload.py`：大产物如何不进上下文

```python
"""Content-addressed offload of inline human media and binary `read_file` blocks.
Binary blocks are written to `{artifacts_root}/blobs/<sha256>` and replaced in
state with a `deepagents_blob` reference. Model requests are rehydrated from the
backend."""
```

内容寻址（sha256）、状态里只留引用、模型请求时从 backend 还原。

**这就是我上一版说的 Artifact Store，人家已经写好了。** 你做图像/视频生成时，产物（几 MB 到几百 MB）绝对不能进事件日志和上下文——必须走这条路。

一条要注意的坑（注释里写了）：「Initial input checkpoints can still carry inline payloads」——**首次输入的 checkpoint 仍可能内联携带载荷**。也就是说，卸载逻辑有边界情况，不能假设「所有大对象都已引用化」。

---

## 4. 结构性缺口：0 / 8 覆盖媒体生成

你的 8 个项目全部属于四类：Agent Runtime（Strands / Deep Agents）、执行环境（OpenHands / Cua）、桌面产品（Zed / thClaws）、记忆与观测（Mem0 / Phoenix）。

**没有一个是媒体生成引擎。** 而你的产品需求里「图片视频生成」和「编程」是并列的两条主线。

后果不是「少看了一个项目」，是**架构里不会出现执行模型的分叉**。你的报告里 `Run` 状态机只有一条线（`RUNNING → WAITING_APPROVAL → PAUSED → ...`），但：

- **视频生成不能「PAUSED」。** 它要么在队列里、要么在跑、要么失败。你暂停一个 5 分钟的生成任务，GPU 那边还在烧钱。
- **视频生成不能「重试」得像工具调用一样。** 重试 = 重复计费 + 重复出图。
- **视频生成的「产物」不是文本返回，是几 MB 的文件 + 一个可预览的 URL。**

补三个必看项目（按优先级）：

| 项目 | Stars | 为什么必须看 |
|---|---:|---|
| **pydantic-ai**（`images/` 模块） | 21,086 | 唯一把「生成模型」与「聊天模型」做成两个平行继承体系的框架。`ImageGenerationModel(ABC)` 独立于 `Model`，每家一套 `*_geometry.py` 处理画幅 |
| **ComfyUI** | 145,674 | 媒体生成的工业执行形态：`POST /prompt → prompt_id → GET /history`，且 `interrupt`（运行中）与队列出队（排队中）是两条路径 |
| **diffusers** | 34,888 | 图/视频/音频统一模型库；决定了你自建时底层跑什么 |

> 一句话：**你的 8 个项目教你「怎么跑一个会说会写代码的 agent」，没教你「怎么交付一个 30 秒后才生成的视频」。** 后者是产品的一半。

---

## 5. 三处我认为要改的设计判断

### 5.1 「16 个核心模块」太多，应按「谁能独立崩溃 / 独立替换」切

你列的 16 个里，有三个是同一件事的切片：`Memory Engine` 与 `Knowledge Engine`、`MCP Engine` 与 `Tool Engine`、`Evaluation Engine` 与 `Observability Engine`。

合并建议 → **11 个**：

- `Knowledge Engine` 并入 `Memory Engine`（文件型 KB 只是记忆的一种载体，不是独立生命周期）
- `MCP Engine` 并入 `Tool Engine`（MCP 是 transport adapter，与 HTTP/gRPC 同级，不配独立模块）
- `Evaluation` + `Observability` 合并为 `Evaluation & Telemetry`（共用同一份事件流，拆开会出现两套 trace）

判据：**两个模块如果不能独立崩溃、也不能独立替换，就不该是两个模块。**

### 5.2 「权限在 Tool Dispatcher / Native System Bridge / Sandbox Worker 三层分别检查」——这条会埋雷

三个地方各自判定策略，必然出现「A 层允许、B 层拒绝」或规则漂移。

正确做法（也是 v1 PRD 的 INV-3）：

```
唯一授权点：Tool Dispatcher（执行侧）
  ↓ 签发 grant
Native System Bridge / Sandbox Worker
  ↓ 只做一件事：验证 grant 存在、未过期、作用域匹配
  ↓ 不做策略判定
```

**多一道检查不等于更安全，等于多一套实现要同步。** 需要防的是「绕过 dispatcher 直接调底层」——这靠**底层只接受带 grant 的调用**来防，而不是靠底层重新跑一遍策略。

### 5.3 「第一版就把 pause_run / resume_run 纳入协议」——方向对，但要按执行模型分叉

你的判断（先定协议再实现）是对的。但 `pause` 不是全局能力：

| 执行模型 | 能否暂停 | 语义 |
|---|---|---|
| E1 会话循环 | 能 | 在回合边界暂停，恢复即重发请求 |
| E2 生成作业 | **不能** | 只能「取消并丢弃」；已计费不退 |
| E3 沙箱副作用 | 能 | 在工具边界暂停；但**运行中的进程不能冻结**，只能取消重跑 |

所以协议里应写成 `capabilities: {pause: bool, resume: bool}`，由执行模型声明，而不是给所有 Run 一个统一的 `pause_run()`。**假接口比没有接口更糟**——调用方会以为它有用。

---

## 6. 可直接采用的接口（把你报告的伪代码换成真实枚举）

### 6.1 Agent 执行引擎

```python
class RunLimits(TypedDict, total=False):
    turns: int
    total_tokens: int
    output_tokens: int
    structured_output_attempts: int
    # 必须显式决定两件事（Strands 的做法值得抄）：
    inherit_by_nested: bool      # 子 agent / agent-as-tool 是否继承
    auxiliary_counts: bool        # 摘要/路由/记忆抽取等辅助调用是否计入

class StopReason(StrEnum):        # 13 值，不是 done/error
    CANCELLED = "cancelled"; CHECKPOINT = "checkpoint"
    CONTENT_FILTERED = "content_filtered"; END_TURN = "end_turn"
    GUARDRAIL_INTERVENED = "guardrail_intervened"; INTERRUPT = "interrupt"
    LIMIT_OUTPUT_TOKENS = "limit_output_tokens"
    LIMIT_STRUCTURED_OUTPUT_ATTEMPTS = "limit_structured_output_attempts"
    LIMIT_TOTAL_TOKENS = "limit_total_tokens"; LIMIT_TURNS = "limit_turns"
    MAX_TOKENS = "max_tokens"; STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
```

### 6.2 三个执行模型的结果类型不能合并

```python
LoopResult   { turn_id, content[], stop_reason: StopReason, usage }
JobResult    { job_id, state: queued|running|succeeded|failed|cancelled,
               queue_pos, artifact_refs[], provider_cost, error }
EffectResult { call_id, status, effect_state: none|applied|partial|unknown,
               receipt_id, evidence[] }
```

### 6.3 Artifact（直接对应 `_blob_offload` 的实现）

```python
Artifact {
  artifact_id, sha256, mime, bytes, kind: image|video|audio|text|binary,
  source: generated|uploaded|derived,
  provider?: str,        # 生成产物记录来源，跨 provider 复用需校验
  run_id, created_at, expires_at?
}
# 存储：{artifacts_root}/blobs/<sha256>
# 状态：事件日志里只存 artifact_ref，绝不内联字节
```

---

## 7. 我建议的落地顺序（与你报告的不同）

你给的是「先定 16 模块 + 全量数据模型」，我建议**先做三件事的纵向切片**：

1. **一周内**：`Event {seq, run_id, type, actor, payload_ref, payload_hash, parent_seq}` + 一个能重放的 EventLog（抄 OpenHands `event_store.py`：一事件一文件、磁盘索引即 seq、空洞上报）。
2. **两周内**：E1 闭环 + `StopReason` 十三值 + `RunLimits` 四项与继承规则。**这一步就把媒体需求的钩子留出来**（`JobResult` 先定义、先不实现）。
3. **三周内**：E2 `JobResult` + Artifact Store（抄 `_blob_offload` 的 sha256 路径），接一家图像生成 provider 打通「提交→轮询→产物」。

理由是：**你报告里那 16 个模块里有 9 个可以晚做，但事件日志、执行模型分叉、产物存储这三个如果晚做，后面全是数据迁移。**

---

## 8. 待你确认

1. 视频生成：自建（diffusers + GPU）还是接服务（可灵 / Runway / Veo）？这决定要不要 `JobResult` 里的 `queue_pos` 和本地 GPU 配额。
2. 图像视频是否走用户自己的 key？决定 Artifact 是否要记 `billing_owner`。
3. 是否接受我把 16 模块收敛成 11 个？如果坚持 16，需要给出每个模块「能独立崩溃/独立替换」的理由。
