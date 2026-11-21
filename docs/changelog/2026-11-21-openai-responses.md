# OpenAI Responses 实施记录

## 范围与基线

- 基线：`71eb57e`，包含已合入的独立 Responses 与模型用量 v2 契约。
- 实现分支：`codex/openai-responses`；本记录不表示已合入 `main` 或已完成线上验收。
- Python 3.12、标准库 HTTP/SSE，未新增运行时依赖，不涉及发行流程。

## 实现

- 独立 `OpenAIResponsesModel`，支持公开文本、函数工具与加密 reasoning。
  使用 `store:false`、完整历史和 `reasoning.encrypted_content`；工具结果按 `call_id`
  配对，保留 item id、phase、有序输出和原始参数字符串。
- HTTP 与 SSE 共用结果校验；SSE 核对序号、item/content 身份、增量与 done/final 快照，
  拒绝断流、未知事件与冲突终态。reasoning 不进入瞬态预览，密文不进入 UI 详情或默认诊断包。
- `ContinuationRef` 1.0 按 adapter/规范化 endpoint/model 绑定；scope 不兼容时拒绝调用，
  提示新建 Run。`ctx-responses-v1` 从首轮起完整保留上下文，超预算明确失败。
- `model.result` / `run.model_turn` 精确 v2 保存可选缓存用量；固定其历史 v0/v1 schema，
  不改历史 payload/hash。缓存与 reasoning tokens 不重复累加，未知缓存计费保持未知。
- result/turn/cursor 在同一事务提交；在分离的内存进度上暂存回执，提交失败不发布回滚结果。
  保留已提交终态证人，崩溃/暂停恢复不重复生成；Responses 投影丢失时从事件重建消息与提案。
- 每步固定运行时模型配置，保持请求、scope、预占与结算身份一致。
  崩溃重试从持久 intent/预占取得新 attempt，不能复用已消费请求额度。
- CLI 在线 provider、设置中心与快速切模均增加独立模式，必须显式填写 Model ID，
  复用 `openai` 凭据域；旧模式与旧配置保持原含义。

## 验证状态

定向红测与原件保存于本地 ignored `.omc/artifacts/openai-responses-20261120/`；
这些本地材料不是 hosted CI 或发布证明。

- 已执行 Responses 离线契约、交错 SSE、真实回环 HTTP/SSE、工具往返、审批、
  删除 cursor/turn 后重建、子进程 crashpoint、缓存用量、事务回滚与诊断脱敏测试。
- 受影响域回归发现旧测试将当前模型事件版本写死为 v1；仅更新新写入断言，
  历史 v0/v1 schema 摘要和原始 fixture 保持固定。
- 最终全量、lock/ruff、UI 构建、独立第二读者与 required CI 待冻结后填写。

## 未验证范围

- 官方真实付费 smoke：`NOT_RUN`。
- 标准兼容网关真实付费 smoke：`NOT_RUN`。
- 没有隐式读取密钥执行付费探针；本机回环服务与离线夹具不构成线上兼容证明。
- 多模态、供应商内置工具、后台任务、WebSocket 均不在支持范围。
- 有效续接链超预算会失败；不自动迁移旧 Run，不提供跨 scope 续接。
- 模型响应已发送但尚未提交时进程退出，Run 账本如实保留 unbilled/unknown；
  已消费或无法核实的预算占用不退款。未扩大原有计费保证。
