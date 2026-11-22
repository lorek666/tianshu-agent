# Prompt Cache 策略与观测 spec

状态：实施规范；真实供应商命中率及任务质量实验尚未执行。

## 目标与源码基线

以 `7665e8684f5a124fe789f21909ee54325edc556b` 为基线。本轮检查的是
`model.py`、`openai_responses.py`、`context.py`、`conversation.py`、
`tool_registry.py`、`_loop_memory.py`、`_loop_summary.py` 与评测运行器。

缓存用量并非从零补起：`model.result` / `run.model_turn` 精确 v2 已保存可选的
`cache_read_input_tokens` / `cache_creation_input_tokens`；Responses 已解析
`input_tokens_details.cached_tokens`，并保留 `store:false`、完整 input 和 opaque
续接。Chat Completions 尚未解析 `prompt_tokens_details.cached_tokens`。
内置 manifest 已有固定顺序，MCP 按 id 追加，但 adapter 尚无缓存策略。

现有动态记忆是追加的 `memory.search/get` 工具结果，不是每轮重写 system 的全库注入。
system 来自固定提示词，流式预览不进入持久化消息。保留这些属性；不新注入时间、
随机请求编号、权限或流式进度，也不把历史工具结果移到别处。

## 决策

采用 adapter 层的版本化 wire 策略与独立观测报告，不改变 C1–C5 对象、
持久化事件版本、历史消息摘要、工具身份、授权、预算或重试契约。
不重排消息、工具调用/结果对、schema 数组或 Responses output items。
内部 manifest 不变；启用策略时在 adapter 副本上按内置目录顺序、动态工具名固定顺序，
递归固定工具对象的 JSON 键顺序。提示正文的空白也不做规范化。
开启策略时也固定开头普通 system 对象的键顺序，保留正文与原输入；`off` 不改 wire 形状。

构造参数 `cache_policy` 优先于环境变量 `TIANSHU_PROMPT_CACHE_POLICY`。
策略在 adapter 构造时解析并固定，非法值或协议不匹配立即拒绝。

- `off`：不加缓存参数，保留既有 wire 形状。
- `auto`（默认）：仅官方 HTTPS OpenAI 端点采用 `prefix-v1`；
  Anthropic 和非官方端点关闭。不会仅凭 “compatible” 开启厂商特性。
- `prefix-v1`：主动声明所选端点支持对应协议。OpenAI Chat/Responses 使用稳定的
  `prompt_cache_key`；Anthropic 使用最多四个 `cache_control` ephemeral 断点。
- `responses-explicit-v1`：仅 Responses，主动声明模型支持
  `prompt_cache_options` / `prompt_cache_breakpoint`，使用显式模式、`30m` TTL。
  不根据模型名称猜支持；不用于 Chat 或 Anthropic。

不启用 prewarm、不自动增加模型调用、不自动回退后重发、不设置延长保留参数，
不引入服务端会话存储。显式缓存可能增加写入费用，故 Anthropic 与新版 Responses
显式策略须主动开启。策略不是能力或授权证明；网关真实行为仍需在线验证。

## 稳定前缀与动态尾部

稳定 key 绑定策略版本、adapter、端点、模型、开头连续 system 正文和完整工具定义，
使用 SHA256，不包含明文目标、路径或凭据。任务与历史变化不换 key，但仍由供应商
核对精确前缀；key 相同不是命中证明。

Anthropic 断点依次放在最后一个工具、system 末块、初始用户任务块、
最新消息之前的历史末块，去重后最多四个。转换在副本上完成，不拆工具往返。
任务变化仍可复用 system；尾部记忆/新工具结果变化仍可复用先前历史。
供应商最小缓存长度、lookback、驱逐和路由影响命中，程序不按字节长度宣称命中。

Responses 显式断点只加在可识别的输入文本块，覆盖开头最后一条 system、确知的初始任务及
动态末条消息之前最近两处用户历史文本；总数最多四个，并让相邻轮保留历史查找交集。
任务只取紧随开头 system（或位于起点）的 user，不把 opaque/压缩后的最新补充视作初始任务。
不修改 reasoning、compaction、output message、
function_call 或 function_call_output。没有合适历史文本时只复用稳定文本前缀。
原 `ctx-responses-v1` / `ctx-responses-compact-v1` 续接与压缩规则保持不变。

`/compact` 的新 Run、Responses 压缩项和语义摘要可能改变历史前缀。这是有损上下文
策略与缓存复用之间的真实取舍，不以关闭摘要、扩大窗口或保留无关内容作为默认优化。

## 真实用量与统计口径

补齐 Chat JSON/SSE 的嵌套 cached token 解析及 Chat/Responses 的
`cache_write_tokens` 创建量投影，启用缓存策略时在 Chat SSE
请求声明 `stream_options.include_usage=true`。累计 usage 是快照，不逐片相加。
缺字段或显式 null 仍为未知；非法 cache 数值或超过已知总输入时拒绝。
新事实沿用现有 `model.result` / `run.model_turn` v2 落库和重放。

独立统计不修改预算或价目表：

- OpenAI 原生输入包含缓存读取与写入，分母为 `input_tokens`，不得重复加缓存量。
- Anthropic 原生输入不含缓存读取/创建，分母为三者之和；缺任一项则分母未知。
- 非官方厂商、未知协议或不完整用量保留原始计数，命中比例未知，不套官方倍率。
- 只按 `model.result` 计成功回执，绝不再把 `run.model_turn` 算一次。
  有 intent 无 result 的 attempt 单列，不能算成零用量或缓存未命中。
- 同时报 token 加权命中率、可观测请求中的命中请求率、未知样本数与各自分母。
  可观测子集的比率必须明确标注，不能冒充所有请求的比率。

## 与任务质量联合评测

现有 `evaluate` 增加可选缓存旁路报告，复用同一 `SuiteRun.observations`、
真实事件与真实 C5 判据，不增加模型调用、不改既有 C5 报告 schema/判据/安全门槛。
按每个请求实际的 context 事实分组：无已记录变换、确定性裁剪、记忆、语义摘要、
`/compact`、Responses 压缩；组合条件保留组合标签，不能混算纯单变量实验。
每组同时报告 C5 任务成功、失败、安全失败、无效及不可用情况。
逐请求、逐任务和质量分组保留原 C5 `scope`，模型任务与脚本恢复任务的质量分母分开。
一次 Run 跨多条件时单独标识 mixed，不能把最终成功归因于某一个请求。
既有 intent 未单列 spill，因此无已记录变换用 `baseline_or_spilled` 标签，
不能据此认定字节完全原始。语义摘要辅助调用缺少缓存明细，报告单列遥测缺口，
不将可观测主调用命中率冒充全体调用命中率；总调用与成本保留 C5 原事实。

报告绑定 suite/fixture 身份、模型/厂商、策略配置、逐 Run、逐请求和真实 token；
没有线上模型证据时明确是离线夹具，不给出“真实命中率已改善”的结论。
后续在线 A/B 须同模型/端点/任务/判据/工具集、每条件至少三次、保留冷/热次序及间隔，
并计入压缩/摘要本身的模型调用及费用。不同任务集、混合条件或缺样本时不作因果比较。
本轮只交付采样和报告链路，不擅自调用付费端点，也不将简单文本匹配冒充任务质量。
旁路报告目标须是工作根外的新文件；在构造模型前拒绝与工作根、主报告和基线的同文件
（含硬链接）及父子路径冲突、非目录祖先。独占创建防止前置检查后出现的文件被截断；
不宣称能隔离外部进程替换整条父目录链的文件系统竞争。

## 验收

1. 三种 adapter 在工具输入顺序/对象键顺序变化后，稳定区实际请求序列化一致；
   schema 数组、消息顺序、输入对象不变；正文一个字节变化能改变相应身份。
2. 开关、协议不匹配、非官方 auto、显式 opt-in、JSON/SSE 同策略都有负向覆盖。
3. Anthropic 断点不超过四个；Responses opaque 续接逐值保持，store 仍 false。
4. 原生 Chat cached usage 穿过真实 adapter、主循环、事件、turn、replay；
   正值、零、null、缺失、非法及截断路径都覆盖。
5. 命中率分母按 provider 区分；未知不填零，重放/双事件不双算。
6. 旁路报告使用真实 C5 结果，质量失败和安全失败不能被缓存指标抵消。
7. 定向红绿、受影响回归、lock/Ruff、代码冻结后的本机全量与独立第二读者完成后，
   再进行本轮候选 CI；不为每步中间修改启动完整矩阵。

## 官方协议依据（2026-11-22 检索）

- OpenAI Prompt caching：<https://developers.openai.com/api/docs/guides/prompt-caching>
- OpenAI Responses create：
  <https://developers.openai.com/api/reference/resources/responses/methods/create>
- Anthropic Prompt caching：
  <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- Anthropic usage / rate limits：
  <https://platform.claude.com/docs/en/api/rate-limits>

官方文档当前已区分新版 Responses 显式断点与历史隐式缓存，不能把所有 OpenAI
模型视为支持同一组字段。上述在线文档会变化，策略版本固定本轮采用的字段子集。
