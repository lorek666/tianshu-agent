# OpenAI Responses 续接契约

状态：2026-11-20 由独立契约 PR 批准；2026-11-21 实现分支已接入运行时代码，
最终独立复审、required CI 与合入状态见[实施记录](changelog/2026-11-21-openai-responses.md)。
权威上位规则为 [PRD §5/§6](PRD-v1.md)。本次不改变 C2/C4 授权或既有结束原因集合。

## 1. 协议边界

增加独立 `openai-responses` 模式，保留 Chat Completions、Anthropic 和 Scripted。
只支持公开 Responses HTTP/SSE 的文本、函数工具和加密 reasoning；不支持多模态、
供应商内置工具、后台任务或 WebSocket，不做网关私有兼容或协议降级。
`{base_url}/responses` 请求使用 `store:false` 和 `include:["reasoning.encrypted_content"]`，
不使用 `previous_response_id` 或托管 conversation；默认不发 temperature/reasoning 调节项。
工具从真实 `tool_manifest()` 翻译为扁平 function schema，显式 `strict:false`。
函数结果通过 `call_id` 配对，不使用 output item `id` 代替。

## 2. C1 ContinuationRef 1.0

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
`output_items` 是有序、可回传的 JSON 对象数组，只接受 reasoning、assistant message、
function_call；保留 item IDs、`phase`、reasoning `encrypted_content` 与顺序，不解析或伪造密文。
缺少续接所需字段、未知 output/content 类型、未完成调用不得成为可派发的工具。
正常终态的完整函数提案映射 `tool_use`，纯文本映射 `end_turn`，拒绝与输出限制映射现有值；
failed、未知/缺失 status、流终态缺失或冲突抛 `ModelError`，不猜成功。
usage 缺失为未知，仅使用最终快照；reasoning token 已计入 output totals，不重复累加。

没有 continuation 的旧 payload/message 不新增空字段，保持历史 canonical bytes 与 hash。
有效续接消息只能传给 scope 一致的模型；改变 adapter、endpoint 或 model 必须拒绝下一次调用，
提示创建新 Run。ContinuationRef 不是普通文本，不进入默认诊断包或 UI 文本。

## 3. C3 model.result v2 与恢复

在已批准的[模型用量 v2 契约](model-usage-v2-contract.md)上，`model.result` payload v2
额外可带上述 continuation；`model.result` / `run.model_turn` 的 v2 usage 均保留 C1 缓存字段。
v0/v1 按原 schema 解释，禁止默认补 continuation 或把旧事件升级为 v2。
`run.model_turn` 事件不新增 continuation，完整续接保存在模型结果、turn 投影与 cursor。
事件版本旁表记录精确版本；除上述两个已定稿的模型事件外，其他事件不随之升版。
模型结果、续接、模型 turn 投影与 cursor 必须在既有同一事务提交；崩溃时全有或全无。
loop、投影重建与 replay 共用 assistant 消息构造，replay 不联网，工具仍在完整结果持久化后派发。
审批恢复复用保存的提案和续接，不能重新生成已经持久化的模型回合。

## 4. 上下文与兼容

新增 `ctx-responses-v1` 编译策略：第一轮起保留完整输入，后续有效续接链的既有输入前缀
不得被摘要、spill、裁剪或其他动态上下文注入改写。完整消息超过字节预算抛 `ContextOverflow`。
模型调用意图保存策略版本，replay 按当时版本重编；历史 `ctx-v1` 算法与含义不变。
其他协议继续原有上下文策略。没有自动迁移已有 Run 或设置。

## 5. 配置与验收

设置 mode 和所有在线 CLI provider 新增 `openai-responses`；显式指定 Model ID；
凭据域仍是 `openai`，复用 Keychain/env，不新建 credential provider。
官方价格只对既有可信 host/模型匹配生效，网关或缓存计价不能准确判断时保持未知。

验收包括真实工具清单投影、交错函数调用、畸形参数、未知枚举/类型、SSE断流与冲突、
两轮 reasoning 工具往返、审批与真实子进程 crashpoint 恢复、投影/replay hash、scope隔离、
预算溢出、旧日志兼容、费用与诊断脱敏。官方与网关真实 smoke 各自显式启用、分别留证；
未提供凭据时记 `NOT_RUN`，不以离线夹具宣称线上兼容。
