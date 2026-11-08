# 通用 Agent 架构 v2：编程 + 图像视频生成 + 用户自选模型

> 修订依据：需求从「编码 agent」扩展为「通用 agent，含编程与图片视频生成，模型不锁定，用户可自选」。
> 本文独立完成，附 GitHub 实测数据（2026-11-08 抓取）与源码证据。

---

## 1. 一句话结论

**聊天/工具循环只能覆盖三种执行模型中的两种。** 上一版 PRD 把「编程 agent」当基准，隐含假设是「所有动作都是毫秒级的工具调用」。加入图像和视频生成后，这个假设失效——生成是**异步作业**，不是工具调用。架构必须先把执行模型拆开，再谈模块。

---

## 2. GitHub 实测：哪些能学，哪些只是热闹

### 2.1 星数分布（真实抓取）

| 项目 | Stars | 它真正解决什么 |
|---|---:|---|
| n8n-io/n8n | 209,803 | 工作流自动化，不是 agent |
| anomalyco/opencode | 225,378 | 编码 agent（客户端形态） |
| langgenius/dify | 160,650 | 模型供应商抽象 + 工作流 + RAG |
| open-webui/open-webui | 158,080 | 多模型前端 |
| Comfy-Org/ComfyUI | 145,674 | **媒体生成的执行引擎（图/视频/音频）** |
| lobehub/lobehub | 84,372 | 多模型客户端 + 插件 |
| cline/cline | 71,457 | 编码 agent |
| openinterpreter/openinterpreter | 68,834 | 本地代码执行 agent |
| BerriAI/litellm | 62,896 | **100+ LLM 统一成 OpenAI 格式** |
| microsoft/autogen | 61,747 | 多 agent 编排 |
| crewAIInc/crewAI | 60,636 | 角色制编排 |
| FoundationAgents/OpenManus | 58,922 | 通用 agent（模型无关） |
| aaif-goose/goose | 56,346 | 可扩展 agent |
| Aider-AI/aider | 50,019 | 编码 agent（edit format） |
| agno-agi/agno | 43,102 | agent 平台 |
| huggingface/diffusers | 34,888 | 扩散模型库（图/视频/音频） |
| mastra-ai/mastra | 29,405 | TS agent 框架 |
| vercel/ai | 27,806 | TS 多供应商 SDK |
| pydantic/pydantic-ai | 21,086 | **模型无关 agent，含图像生成** |
| kortix-ai/CompanyOS | 20,548 | 通用 agent + 沙箱 |
| agent0ai/agent-zero | 19,794 | 通用 agent |

**读法提醒：星数在这里几乎没用。** 200k 星的 n8n 是工作流工具，跟你的架构没关系；21k 星的 pydantic-ai 反而是最值得抄的。以下按机制价值排序，不按星数。

### 2.2 只有 pydantic-ai 把「生成」做对了

我拉了 pydantic-ai 源码（`pydantic_ai_slim/pydantic_ai/`），它是目前唯一同时提供 agent 与图像生成的主流 Python 框架。关键实现：

```
models/          # 聊天 LLM 抽象（Model ABC）
  openai.py anthropic/ google.py bedrock.py ollama.py openrouter.py ...
images/          # 独立的生成抽象，不在 models/ 下
  base.py        # class ImageGenerationModel(ABC)
  openai.py google.py xai.py
  _openai_geometry.py _google_geometry.py _xai_geometry.py
  settings.py    # 每家一套 ImageGenerationSettings
```

`images/base.py:18` 的定义是**与 `Model` 平行的另一个 ABC**，接口是：

```python
async def generate(prompt, *, images=None, settings=None) -> ImageGenerationResult
```

**这是本次调研最重要的一条证据**：一个支持几十家供应商的框架，没有把图像生成塞进聊天模型接口，而是另建一套并行继承体系，连每家的画幅/尺寸换算（`*_geometry.py`）都单独成文件。

推论：**"统一所有模型的 API"这个目标本身是错的。** 正确目标是「按能力类别统一」，类别之间不通用。

### 2.3 ComfyUI 暴露了「异步作业」的真实形状

`server.py` 的路由与队列（实测行号）：

```
POST /prompt        → 入队，拿 prompt_id
GET  /history       → 按 prompt_id 取产物
GET  /queue         → 队列状态
POST /interrupt     → interrupt_if_running(prompt_id)
                     # 1039 行注释：Running jobs are interrupted; pending jobs 走 /queue delete 出队
```

它把「运行中」和「排队中」的取消**分成两条路径**。这正好对应我们 PRD 里的 `effect_state`：排队中取消 = `none_applied`（可安全重试），运行中打断 = 可能已产生部分副作用（`partial` 或 `unknown`）。

**媒体生成的工业实现不是"调用一个函数"，是"提交作业 + 查询产物 + 队列位置 + 可中断"。**

### 2.4 LiteLLM 证明聊天侧已有事实标准，媒体侧没有

LiteLLM（62.9k）干的事只有一件：把 100+ 家 LLM 统一成 OpenAI 格式。它的成功说明——
**chat 侧不需要你自建统一层，OpenAI-compatible 已经是事实标准，直接复用即可。**

但 LiteLLM 一行 generation 代码都没有。同理 OpenRouter、vercel/ai、Dify 的模型抽象，全都只到 chat + embedding。**媒体生成这一层，全世界都还在各写各的。**

所以「用户自选模型」这件事的难度分布是极不均匀的：

| 能力 | 生态状态 | 我们的工作量 |
|---|---|---|
| 聊天 / 工具调用 | 事实标准已存在 | 低（接 LiteLLM / OpenRouter 或直连） |
| Embedding | 事实标准已存在 | 低 |
| 图像生成 | 每家 SDK 一套，参数不通用 | 中（自建 job 抽象 + 几何/参数映射） |
| 视频生成 | **无任何统一抽象**，异步接口、轮询协议、计费方式各不相同 | 高（自建，且要做超时/轮询/失败重试的账务语义） |
| 语音 | 半标准（各家 TTS 接口相似） | 中 |

---

## 3. 核心架构：三种执行模型，不能合并

这是本版对 v1 的最大结构修改。v1 只有「会话循环 + 沙箱进程」两种；现在必须加第三种。

```
E1  Conversation Loop     token 流、毫秒~秒级、可流式、可中途打断
                          典型：模型推理、工具参数生成、读文件

E2  Generation Job        异步、秒~分钟级、有队列位置、产物是二进制大件
                          典型：图像生成、视频生成、长音频合成

E3  Sandboxed Effect      有副作用、需 before/after 证据、要 receipt
                          典型：跑测试、git 提交、装依赖、发布
```

**三条必须遵守的边界规则：**

1. **E2 不得伪装成 E1 的工具调用。** 视频生成动辄 2–10 分钟，塞进工具循环会导致：流式超时、会话被阻塞、取消语义失真。正确做法是 E1 里发起一个 job，立即返回 `job_id`，由调度器推进。
2. **E2 的重试与 E1 不同。** E1 重试是幂等的（模型调用无副作用）；E2 重试可能重复计费、重复出图。必须有 `job_key` 去重 + 显式"是否允许重复扣费"策略。
3. **E3 的证据（receipt）在 E2 里不适用。** 生成作业的"副作用"是计费和产物文件，要对账的是账单和产物哈希，不是文件 diff。

对应到数据模型，`ToolResult` 不能同时表达这三类，必须并列三个结果类型：

```python
LoopResult       { turn_id, content[], finish_reason, usage }
JobResult        { job_id, state, queue_pos?, artifact_refs[], provider_cost, error? }
EffectResult     { call_id, status, effect_state, receipt_id, evidence[] }
```

> 这条改动的代价：PRD 里所有「工具」都要重新标注自己属于哪一类。回报：视频生成不会把整个会话拖死。

---

## 4. 「用户自选模型」的真正设计点

不是做一个下拉框。是下面四件事，缺一件就会崩。

### 4.1 能力画像必须是产品的一等公民

用户在界面上选的不是「模型名」，是「能力组合」。`CapabilityProfile` 要先于 UI 存在：

```
ChatModel        { tool_calling, parallel_tool_calls, strict_schema,
                   structured_output, vision_in, context_window, audio_in }
GenerationModel  { media: image|video|audio, sizes[], aspect_ratios[],
                   reference_images?, video_duration_s?, async_model,
                   poll_interval, max_concurrent, unit_cost }
```

**必须做的检查：用户选了不支持 tool calling 的模型，agent 要能降级成"纯对话模式"而不是直接报错。** 这是「自选模型」最常见的翻车点：能力探测没做，用户选了个小模型，整个 agent 就瘫在那儿。

### 4.2 凭据按用户隔离，按能力分域

- 每个用户一份 `CredentialBinding`，域是 `(provider, capability_class)`，不是「一个 API key 通用」。
- 聊天 key、图像 key、视频 key 往往是不同账号/不同计费主体，**不能混在一个 secret 里**。
- 密钥永不出现在 prompt、日志、trace、工具参数里；只出现在 adapter 的出站请求构造处。

### 4.3 成本必须实时可见

用户自选模型 = 用户自选账单。没有 per-request 成本回传，用户会在一周内收到天价账单然后弃用。
每次 E1/E2 调用都要回 `{provider, model, tokens_or_units, unit_price_ver, cost}`，聚合成 `run_cost`。这是**产品需求，不是运维需求**。

### 4.4 降级链要显式声明

`primary → fallback_1 → fallback_2`，每一跳要说明「为什么可以降级」（同能力？同价位？同合规域？）。禁止隐式 fallback——悄悄换模型产生的内容差异，用户会视为 bug。

---

## 5. 借鉴 / 不借鉴清单

### 5.1 直接借鉴（附具体机制）

| 来源 | 抄什么 | 为什么 |
|---|---|---|
| **pydantic-ai** `images/base.py` | `GenerationModel` 与 `ChatModel` **平行继承**，各自 provider 实现；每家的尺寸/画幅换算单独成文件 | 唯一把生成做对的抽象；证明"分类统一"优于"大一统" |
| **pydantic-ai** `images/base.py:_validate_uploaded_file_provider` | 上传产物绑定 provider 作用域，跨 provider 复用直接报错 | 产物有来源系，换模型不能静默复用旧引用 |
| **ComfyUI** `server.py` | `POST /prompt → prompt_id → /history` 的作业模型；`interrupt` 与 `queue delete` 区分运行中/排队中 | 视频生成的正确形状；队列位置本身是产品信息 |
| **OpenHands** `event_store.py` | 一事件一文件的 EventLog，磁盘索引即 seq，`parent_id` 链，丢失空洞上报 | 状态外置的工业实现（v1 已记录） |
| **OpenHands** `conversation_lease.py` | 跨实例租约 + PID 存活校验 | 长生成任务的多实例协调 |
| **OpenHands** `bash_service.py:414-424` | SIGTERM → 进程组 → 优雅期 → SIGKILL | 编程侧取消的唯一正确写法 |
| **LiteLLM** | chat 层直接用 OpenAI-compatible 作为内部规范，别自建 | 62.9k 星验证过的事实标准，自建是浪费 |
| **Deep Agents** `subagents.py:111` | 子 agent 分 `isolated`（只收任务单）/ `fork`（继承上下文） | 通用 agent 里既要做隔离调研，也要做延续式深挖 |
| **MCP** `LATEST_PROTOCOL_VERSION` / `SUPPORTED_PROTOCOL_VERSIONS` | 版本区间协商而非单版本号 | 多实现并存必须协商 |

### 5.2 只可参考（形态可看，机制不抄）

| 来源 | 为什么不抄 |
|---|---|
| dify / n8n / LobeHub | 它们是「工作流/客户端产品」，状态在数据库表里，不是事件日志；套进 agent 会把运行时变成 CRUD |
| OpenManus | 通用 agent 的快速验证形态，但状态与工具全靠 prompt 约束，没有执行侧权限 |
| crewAI / autogen | 角色制编排，v1 明确不做；且 60k 星不代表机制先进，代表上手快 |
| vercel/ai | 思路好（provider registry），但 TS 生态，且同样只到 chat |

### 5.3 明确不抄

| 反模式 | 出处 | 后果 |
|---|---|---|
| 把图像/视频生成做成一个 tool | 多数 agent 项目 | 长任务阻塞循环、超时语义崩坏 |
| 状态存框架 checkpoint | Deep Agents / LangGraph 系 | 换框架即丢状态 |
| 授权写在 prompt 里 | Deep Agents `_DELEGATION_CONTRACT` | 越权不可防 |
| 单一 `contract_version` | 多数 SDK | 多实现并存即卡死 |
| 「OpenAI-compatible」当语义兼容 | 普遍误读 | 工具调用/结束原因/结构化输出细节全不同 |

---

## 6. 模块重排（相对 v1 的变化）

| 模块 | v1 状态 | v2 变化 |
|---|---|---|
| Agent Runtime | 单循环 | **三执行模型调度器**（E1/E2/E3），循环只管 E1 |
| Model Gateway | chat 抽象 | **分裂为两个注册表**：ChatModel / GenerationModel |
| Tool System | 统一 envelope | 工具必须声明 `exec_model ∈ {E1,E2,E3}`；E2 工具返回 job_id 而非结果 |
| Media Runtime | 无 | **新增**：作业队列、产物存储（内容寻址）、GPU/并发配额、轮询与回调 |
| Artifact Store | 隐含 | **新增独立模块**：大产物（图/视频）不进事件日志，只存 `artifact_ref + hash + mime` |
| Credential | 混在 grant | **独立**：按 `(provider, capability_class)` 分域 |
| Cost | 无 | **新增**：per-call 成本回传与聚合，用户自选模型的必需品 |
| Sandbox | 通用 | 只管 E3；媒体生成不需要进程沙箱，需要的是配额 |
| Permission | 统一 | E2 增加「允许重复扣费」这条独立策略 |
| Evaluation | 文本任务 | 增加生成类评测：产物可用性、成本、可复现性（seed/参数快照） |

---

## 7. 三个最容易做错的地方

1. **把 30 秒的生成塞进 tool call。** 表现是：流式连接超时、用户以为卡死、取消按钮无效、重试出两张图。正确做法是 E1 拿到 `job_id` 立即返回，进度走事件。
2. **用 chat 的重试语义重试生成。** 聊天重试免费，生成重试要钱。必须区分「重试安全」和「重试花钱」。
3. **「自选模型」只做了下拉框。** 没做能力探测 → 用户选了无 tool calling 的模型就瘫；没做成本回传 → 用户被账单吓走；没做降级声明 → 悄悄换模型被当 bug。

---

## 8. 里程碑调整

v1 的 M0–M3 保留，插入两段：

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| M0 骨架 | E1 闭环 + 事件日志 | 日志可重放 |
| M1 副作用 | E3 + receipt + 恢复 | kill 100 次零重复副作用 |
| **M1.5 作业模型** | E2：job 队列、状态机、产物存储 | 提交→轮询→产物 全链路；排队中取消 = `none_applied`；运行中取消 = `partial/unknown` |
| **M2 双注册表** | ChatModel / GenerationModel 分裂，接 2 家 chat + 2 家图像 | 换 provider 主循环零改动；能力缺失能降级不崩 |
| M3 可替换 | 第二沙箱后端 | 契约测试全绿 |
| M4 可评估 | 文本 + 生成双评测集 | 配对比较含成本与产物质量 |

---

## 9. 仍然悬着、需要你拍板的

1. **视频生成走自建还是接现成服务**（可灵/Runway/Veo）？自建意味着 GPU 采购与模型运维，量级完全不同。
2. **图像/视频生成是否也走「用户自己的 key」**，还是平台统一付费、按次计费？这决定 Credential 与 Cost 的架构。
3. **编程能力的沙箱边界**：只读工作区 / 可写工作区 / 可出网，三档的默认值。
