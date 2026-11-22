# Prompt Cache：配置与观测

缓存策略位于三个模型 adapter 的请求边界；不会改变工具授权、任务目标、上下文预算、
重试次数或 Responses 的 `store:false`。当前交付的是可配置策略和观测链路，
尚未执行真实付费端点的命中率及任务质量对照实验。

## 选择策略

环境变量 `TIANSHU_PROMPT_CACHE_POLICY` 在 adapter 构造时读取。程序调用可以传
`cache_policy=` 覆盖环境变量。当前没有设置面板里的持久化缓存开关。

- `auto`：默认。官方 HTTPS OpenAI 端点使用稳定前缀 key 和固定工具投影；
  Anthropic、非官方端点关闭显式缓存。
- `off`：不添加缓存参数，保留原请求形状。供应商自身的隐式缓存未必能由客户端关闭。
- `prefix-v1`：主动确认端点支持相应协议。Anthropic 添加最多四个 ephemeral 断点，
  使用其默认短 TTL；OpenAI Chat/Responses 添加稳定的 `prompt_cache_key`。
- `responses-explicit-v1`：仅用于已核实支持新版显式缓存字段的 Responses 模型。
  发送显式模式与 `30m` TTL，在普通输入文本处放断点，不修改 opaque reasoning/
  compaction/output items。不能用于 Chat 或 Anthropic，也不会按模型名称猜兼容性。

例如，在已确认兼容的 Anthropic 端点上主动启用：

```bash
TIANSHU_PROMPT_CACHE_POLICY=prefix-v1 \
  uv run --frozen tianshu run --help
```

上例只显示帮助，不会发起模型调用。实际运行 `run` / `worker` / `daemon` 时，
让对应进程继承该环境变量即可；已运行的桌面进程不会因终端变量变化而更新配置。
非官方端点需要自己的真实验证，启用策略不构成兼容性证明。
显式缓存可能产生写入费用；不自动 prewarm、不增加试探调用、不在参数拒绝后偷偷重发。

## 稳定区与易变区

开头连续 system、工具定义及其对象键顺序保持稳定。工具投影按内置目录顺序，
再按动态工具名排序；schema 内的数组、消息、工具往返和提示正文不重排。
路由 key 只含策略/端点/模型/稳定材料的摘要，不放明文任务、路径或凭据。

Anthropic 断点位于工具定义末尾、system 末尾、初始用户任务块和动态尾部之前的历史块。
原始记忆检索结果仍以工具结果追加；不在每轮把它们重新插进 system。时间和流式预览
也不会新加入稳定区。Responses 隐式策略继续发送完整历史前缀；显式策略只标记
普通输入文本，完整保留原来的加密续接。

`/compact`、语义摘要和 Responses 压缩都可能改变历史前缀。这些操作有信息损失，
缓存策略不会为了命中率而自动关掉摘要、扩大预算或保留无关历史。

## 保存真实缓存用量

Chat 的 `prompt_tokens_details` 与 Responses 的 `input_tokens_details` 中：

- `cached_tokens` → `cache_read_input_tokens`
- `cache_write_tokens` → `cache_creation_input_tokens`

Anthropic 使用其原生同名字段。最终 JSON/SSE usage 沿既有
`model.result` / `run.model_turn` 精确 payload v2 保存；重放不重新调用供应商。
缺失仍为未知，显式 null 保留；SSE 累计值只取最终快照，不逐帧相加。
非法缓存计数或原生读写之和超过已知 OpenAI 总输入时拒绝该回执。

OpenAI 输入总数已经包含缓存读写；Anthropic 总输入是原生输入加缓存读取和创建。
这是观测分母，不改变既有预算语义。当前价目表没有缓存倍率，缓存读写为正值或未知时，
成本继续如实显示未知，不用普通输入单价冒充缓存费用。

## 与任务质量一起查看

`evaluate --cache-report PATH` 在原有 C5 报告之外另存一份
`tianshu.prompt-cache.v1` 报告。它复用同一次 `SuiteRun` 的观察与判据，
不增加模型调用，也不改 C5 的安全否决或可比性规则。

离线冒烟例子：

```bash
uv run --frozen tianshu evaluate \
  --model scripted --case N1 --repeat 3 \
  --workdir .omc/artifacts/cache-smoke \
  --cache-report .omc/artifacts/cache-smoke-report.json --json
```

工作根必须为空；重复执行需换一个新目录。子集只证明冒烟，不是完整评测。
脚本模型报告标记为 `offline`，缓存命中率为未知，不能据此声称真实缓存改善。
真实模型仍通过原有 `--model openai-compatible --provider ... --model-name ...`
选择；只有实际执行这些线上评测才会产生供应商调用和费用。

报告分别提供：

- 每次请求的 usage、provider/adapter/model、来源 seq/hash、上下文条件；
- 按 provider 和条件分组的 token 加权命中率与请求命中率，连同可观测/未知分母；
- 每项任务的 C5 成功、失败、安全失败、无效、未具备能力与耗时/成本原事实；
- 未配对的 intent、重复 result 和缺少缓存遥测的辅助调用；
- suite、fixture、主报告 SHA256 与本次主模型策略配置。

仅统计 `model.result`，不再把同一调用的 `run.model_turn` 算一次。
未知厂商的兼容端点保留原始计数，分母未知；没有回执的调用不算零用量或未命中。
策略配置不是历史事件中逐请求的策略证明。

上下文分组包括 `memory`、`semantic_summary`、`trimmed`、`compact`、
`compact_generation`、`responses_compact`，多种变换并存时保留组合标签。
无已记录变换标为 `baseline_or_spilled`：现有 intent 没有独立 spill 标记，
不能由此断言发出的是完全原始上下文。一个任务跨多种条件时标 `mixed`，
不会把最终成功归因于其中某一次请求。

这是一份观测报告，不能自动证明因果。真实对照需固定模型/端点/任务/工具/判据，
分别收集原始、压缩、记忆和语义摘要条件，每条件至少三次，并保留冷/热顺序及间隔。
语义摘要辅助调用尚未保存缓存明细，报告单列缺口；总调用、总费用以 C5 和账本为准，
不得只报压缩后请求的命中率来掩盖压缩开销。

设计与验收依据见[spec](superpowers/specs/2026-11-22-prompt-cache-design.md)，
实施状态见[变更记录](changelog/2026-11-22-prompt-cache.md)。
