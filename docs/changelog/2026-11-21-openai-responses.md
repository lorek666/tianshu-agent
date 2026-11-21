# OpenAI Responses 实施记录

## 范围与基线

- 基线：`71eb57e`，包含已合入的独立 Responses 与模型用量 v2 契约。
- 实现与合入状态以 [PR #209](https://github.com/lorek666/tianshu-agent/pull/209) 为准；
  本记录不表示已完成线上验收。
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
- 独立第二读者先后在 `8637a4f`、`6e9bf34` 返回 `blocker`；失败原件保留，
  两次旧候选全量均被主动中止，未计为通过。
- 剩余问题是等待子任务的游标/挂起事件顺序，以及首次保存点前崩溃的重复计数；
  作者移植探针先复现失败，再修复。最终独立复核对
  `b7c3773a7dcd310aafe765f2c456313a59742a2c`、
  tree `eed4d8fd25f9b25745cb8a2469fbc794a0faeaab` 给出 `PASS`，
  详见[三轮独立报告、探针与输出](../evidence/openai-responses-2026-11-21/README.md)。
- `uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 通过。
  UI 的 frozen install、typecheck、test、build 通过，dist 已提交；
  后续恢复修复没有改变 UI 文件。
- `b7c3773` 的全量实际退出 1：低磁盘测试仍将两个模型事件写死为 v1，
  与已合入的用量 v2 契约冲突。定向复现后只修改该测试，明确要求四个指定事件
  为 v2，其余事件仍为 v1；产品代码及历史 schema 未变，不把这轮红算作通过。
- 仅测试修正提交 `fccd469325558dae9bc176a88f766b777614f217`、
  tree `d6eb0cf9757ad07efa36771388b8e654f0c14eba` 已获独立 `PASS`；
  [附录](../evidence/openai-responses-2026-11-21/TEST-ONLY-ADDENDUM.md)
  保留定向红绿、错版负控及产品树零差异证明。
- `fccd469325558dae9bc176a88f766b777614f217` 的 `uv run --frozen pytest` 已完整退出 0。
  随后的纯文档收口 `445617df2bf7ee5b55a36e7b79537affb0141eeb` 不改变该测试树。
- `445617d` 的[候选 CI](https://github.com/lorek666/tianshu-agent/actions/runs/44868392549)
  实际失败。Linux、macOS、UI 和两个原生 Rust 作业通过；Windows 两个分片各有一条
  `UnicodeDecodeError`，分别来自 `test_c1_schema_validates_real_ref_and_rejects_adapter_drift`
  和 `test_c3_json_preserves_both_historical_usage_schemas`。测试用系统默认 CP1252
  读取 UTF-8 schema，尚未进入断言。用同一组真实 schema 和测试在本机模拟默认 CP1252
  复现后，只将两处读取显式设为 UTF-8；schema 字节、全部断言与 CI 配置均未改。
  此模拟通过不代替 Windows 原生门禁。旧候选的失败和新候选的独立核对、本机全量及
  hosted CI 均在 PR 中按各自的提交记录。

## 未验证范围

- 官方真实付费 smoke：`NOT_RUN`。
- 标准兼容网关真实付费 smoke：`NOT_RUN`。
- 没有隐式读取密钥执行付费探针；本机回环服务与离线夹具不构成线上兼容证明。
- 多模态、供应商内置工具、后台任务、WebSocket 均不在支持范围。
- 有效续接链超预算会失败；不自动迁移旧 Run，不提供跨 scope 续接。
- 模型响应已发送但尚未提交时进程退出，Run 账本如实保留 unbilled/unknown；
  已消费或无法核实的预算占用不退款。未扩大原有计费保证。
