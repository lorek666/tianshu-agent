# OpenAI Responses 续接契约

状态：基础 Responses 由 PR #209 合入，验证范围见
[实施记录](changelog/2026-11-21-openai-responses.md)。下文注明的自动压缩和附件输入
扩展与[桌面会话输入契约](agent-conversation-contract.md)一致；已在候选代码中接入，尚未合入。
上位规则为 [PRD §5/§6](PRD-v1.md)。扩展不改变 C2/C4 授权或既有结束原因集合。

## 1. 协议边界

增加独立 `openai-responses` 模式，保留 Chat Completions、Anthropic 和 Scripted。
基础模式支持公开 Responses HTTP/SSE 的文本、函数工具和加密 reasoning。
附件扩展只增加有界的 UTF-8 文本和可选 PNG/JPEG 输入（见 §5）；图片输出、音频、视频、
供应商内置工具、后台任务和 WebSocket 均不在范围内。不做网关私有兼容或协议降级。
`{base_url}/responses` 请求使用 `store:false` 和 `include:["reasoning.encrypted_content"]`，
不使用 `previous_response_id` 或托管 conversation；默认不发 temperature/reasoning 调节项。
工具从真实 `tool_manifest()` 翻译为扁平 function schema，显式 `strict:false`。
函数结果通过 `call_id` 配对，不使用 output item `id` 代替。

JSON 和 SSE 共用同一重试边界。客户端按当前 Run 的尝试上限重试完整收到的 HTTP 429。
传输状态不明、正文截断、超时和 HTTP 5xx 结束本次调用并保留未决用量。
HTTP 重定向直接拒绝，使重试依据始终来自配置的 POST 端点。
接收侧要求 HTTP 头部完整，拒绝冲突的长度声明；按 `Content-Length` 读满，或校验分块及尾部闭合。
允许以连接关闭定界的完整响应。正文上限为 64 MiB。SSE 事件须以空行结束；即使已经解析到
`response.completed`，也须等传输完整收束后才持久化结果与结算用量。

## 2. C1 ContinuationRef

`ModelResponse` 与内部 assistant 消息可选增加 `continuation`：

```json
{
  "schema_version": "1.0",
  "adapter": "openai-responses",
  "endpoint": "https://api.openai.com/v1/responses",
  "model": "explicit-model-id",
  "output_items": []
}
```

`adapter` 为封闭集，初始仅 `openai-responses`；未知版本/adapter 拒绝。
`endpoint` 是规范化完整请求地址（scheme/host 小写、去默认端口和 base 尾斜杠，保留路径）；
禁止 userinfo、query、fragment。scope 为 adapter + endpoint + model。
`1.0` 的 `output_items` 是有序、可回传的 JSON 对象数组，只接受 reasoning、assistant message、
function_call；保留 item IDs、`phase`、reasoning `encrypted_content` 与顺序，不解析或伪造密文。
缺少续接所需字段、未知 output/content 类型、未完成调用不得成为可派发的工具。
正常终态的完整函数提案映射 `tool_use`，纯文本映射 `end_turn`，拒绝与输出限制映射现有值；
failed、未知/缺失 status、流终态缺失或冲突抛 `ModelError`，不猜成功。
usage 缺失为未知，仅使用最终快照；reasoning token 已计入 output totals，不重复累加。

没有 continuation 的旧 payload/message 不新增空字段，保持历史 canonical bytes 与 hash。
有效续接消息只能传给 scope 一致的模型；改变 adapter、endpoint 或 model 必须拒绝下一次调用，
提示创建新 Run。ContinuationRef 不是普通文本，不进入默认诊断包或 UI 文本。

**自动压缩扩展**：显式启用后，完整结束的回合可返回一个 `compaction`，必须是 `output_items`
首项，具有唯一、非空的 `id` 和非空 `encrypted_content`；若带 item `status`，须为 `completed`。
只把该回合封装为 `ContinuationRef` `1.1`，普通回合继续用 `1.0`。压缩未启用、压缩项重复或
错位、响应未完整结束、拒绝或流式终态矛盾时，不能接纳该边界。密文保持原样，
只在同一 Run 内由原 adapter、规范化端点和模型用于续接；不会进入跨 Run 追问或手动压缩摘要。

## 3. C3 model.result 版本与恢复

在已批准的[模型用量 v2 契约](model-usage-v2-contract.md)上，`model.result` payload v2
额外可带上述 continuation；`model.result` / `run.model_turn` 的 v2 usage 均保留 C1 缓存字段。
v0/v1 按原 schema 解释，禁止默认补 continuation 或把旧事件升级为 v2。
`run.model_turn` 事件不新增 continuation，完整续接保存在模型结果、turn 投影与 cursor。
自动压缩扩展只在 `model.result` 携带 `ContinuationRef` `1.1` 时以精确 v3 写入，v3 必须携带
该续接；普通结果仍是 v2，`run.model_turn` 保持 v2。读取按事件版本旁表解释和校验，旧事件不改写。
模型结果、续接、模型 turn 投影与 cursor 必须在既有同一事务提交；崩溃时全有或全无。
loop、投影重建与 replay 共用 assistant 消息构造，replay 不联网，工具仍在完整结果持久化后派发。
审批恢复复用保存的提案和续接，不能重新生成已经持久化的模型回合。

## 4. 上下文与兼容

新增 `ctx-responses-v1` 编译策略：第一轮起保留完整输入，后续有效续接链的既有输入前缀
不得被摘要、spill、裁剪或其他动态上下文注入改写。完整消息超过字节预算抛 `ContextOverflow`。
模型调用意图保存策略版本，replay 按当时版本重编；历史 `ctx-v1` 算法与含义不变。
其他协议继续使用各自的上下文策略。已有 Run 或设置不会自动迁移到压缩策略。

自动压缩启用时使用 `ctx-responses-compact-v1`。没有已提交的压缩边界时仍用完整输入；
有边界时，保留开头的系统消息和从最新已提交边界回合起的消息后缀。这包括加密压缩项、
同回合其余输出和已经取得的实际工具结果，工具结果仍用 `call_id` 配对。
裁掉的只是发给该模型的请求前缀；完整事件和游标保留。若仍超过本地字节预算，
抛 `ContextOverflow`。回放按持久化策略重编并核对消息摘要与来源，不联网。

## 5. 配置与验收

设置 mode 和所有在线 CLI provider 新增 `openai-responses`；显式指定 Model ID；
凭据域仍是 `openai`，复用 Keychain/env，不新建 credential provider。
官方价格只对既有可信 host/模型匹配生效，网关或缓存计价不能准确判断时保持未知。

自动压缩默认关闭。显式设置 `compact_threshold`（令牌数，整数 1024–2000000）时，
在同一个 `POST /responses` 请求中加入
`context_management: [{"type":"compaction","compact_threshold":…}]`，保持 `store:false`，
不另发压缩请求。显式选档的 Run 将阈值和模型配置一起固定在路由事件中。

附件仅来自本轮用户提交的字节。每个 Run 最多 4 个，解码后合计最多 240 KiB；
适配前校验内容格式与记录的 SHA-256。UTF-8 文本按带文件名的 `input_text` 发送。
只有明确开启 `allow_image_attachments` 的 Responses 配置才会用 `input_image` 发送
PNG 或 JPEG 的 Base64 data URL。未开启时，在派发前拒绝图片，不丢弃附件或改用其他端点。
适配器不打开原始文件或远程图片 URL；上传不构成工具授权。

验收包括真实工具清单投影、交错函数调用、畸形参数、未知枚举/类型、SSE断流与冲突、
两轮 reasoning 工具往返、审批与真实子进程 crashpoint 恢复、投影/replay hash、scope隔离、
预算溢出、旧日志兼容、费用与诊断脱敏。扩展还需核对压缩边界和旧版写入、工具结果配对、
回放和投影重建、附件的请求形状及派发前拒绝路径。官方与网关真实 smoke 各自显式启用、
分别留证；官方及网关的真实付费模型仍未验证这些扩展（`NOT_RUN`）。
