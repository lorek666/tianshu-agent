# 2026-11-18 · 第 2D/3D 批：W4 模型流式输出实现、W12 C1/C2/C5 Schema 双向绑定与 `run.end` v2 成本汇总

## 背景与交付内容

紧随独立契约 PR [#190](https://github.com/lorek666/tianshu-agent/pull/190)（`beedf51`），完成三项核心交付：

1. **W4 模型流式输出（`model.py` → `loop.py` → `daemon.py` → `host.py` → React `RunTimeline`）**
   - `OpenAICompatibleModel.stream_complete` 与 `AnthropicMessagesModel.stream_complete` 基于纯标准库 SSE 解析增量块并回调 `ModelStreamDelta(schema_version="1.0", kind="text"|"tool_call_start", ...)`；流提前断开或缺少结束原因时一律抛 `ModelError(kind="protocol")`，绝不伪造 `end_turn`。
   - `AgentLoop` / `Worker` / `Daemon` 通过有界高水位保护广播瞬态 `push="model_stream"` 帧，**不逐片写入 SQLite `events` 表**（`replay` 100% 保持原语义）；`Host` `/events` 转发 `event: model_stream`，前端 `RunTimeline` 实时展示流式文本并在收到持久化 `model.result` / `model.error` / `run.end` 时自动收口。

2. **W12 C1 / C2 / C5 字段级 JSON Schema 双向绑定测试（`tests/test_c1_c2_c5_schemas.py`）**
   - 用真实 `FINISH_REASONS`、`ModelError.KINDS`、`SPECS`、`tool_manifest()`、`run_suite()` / `build_report()` / `compare_reports()` 对 `schemas/{c1,c2,c5}/*.json` 执行正负控双向校验。

3. **C3 `run.end` 升 `v2` 货币成本汇总（`event_schema.py` + `schemas/c3/event.json` + `tasks.py`）**
   - `_run_end_payload` 汇总微美元成本 `cost_total_micros`（单价全未知时记 `None`，绝不用 `0` 冒充）与 `cost_unknown_calls`，保留历史 `v0` 与 `RUN_END_V1_SCHEMA` 只读兼容。

## 独立第二读者复审记录（Tier 1）

- **复审会话**：`82d2eb42-1d30-4727-8506-b0877f9549e2`（只读独立复审 + 临时目录对抗探针）
- **Round 1（`fc2f14a`）**：判据 2–5 通过；判据 1 抓出两处流式边界缺陷——`http.client.IncompleteRead` / `BadStatusLine` 非 `OSError` 子类穿透传输层及 `loop.py` 兜底分支未过 `redact()`（1A），以及 Anthropic/OpenAI SSE 畸形 `input` / `partial_json` / 缺失 `content_block_stop` 校验不严（1B）。
- **Round 2 终审（绑定代码提交 `c9e3a52a32ba5ce8cc60afb6ecb7ab8826c7bd7d` · tree `a5a7fca02792c33cdbd52794c9c8c005d531c7b0`）**：修复上述全部缺陷并补齐真 loopback chunked 截断、`BadStatusLine` 密钥回显零明文落库、双协议畸形帧矩阵与 `run.end` v2 零轮次/混合计费回归；经独立探针复跑，五项验收判据全部裁决为 **`PASS`**。
- **Round 3 终审换绑（绑定代码提交 `cfbcc1bf62ca9bc222527e4ea9efe34d086a7089` · tree `39432d36162a1a8adfa049f7467858fa85d5f616`）**：为 100% 兼容既有 IPC `subscribe(since_seq)` 订阅方增加按需 `include_model_stream=True` 隔离（Host `/events` 显式开启，默认订阅仅推持久化 `event`）、将 `RemoteDisconnected` 统一归入 `kind="transport"` 并同步 `test_low_disk_run_creation.py` v2 绊线集合；第二读者独立探针复核裁决为 **`PASS`**，同一非文档代码树本机全量冻结 `pytest` 实跑 **`3828 passed, 12 skipped, 0 failed`**。
