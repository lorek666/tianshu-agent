# 模型用量的 C3 精确 v2 契约

- 决策日期：2026-11-20；状态：**契约草案，待实现**。
- 适用范围：仅 `model.result`、`run.model_turn` 的新写 payload；不改 C1、事件类型集合、信封、数据库机制版本或评测口径。
- 基线：`a0732d9881247998df24db7af9f1c7989c1ae7a4`；实施与契约分开提交、分开 PR，实施合入须晚于本契约。

## 1 原方案

C1 已允许 provider 上报 `cache_read_input_tokens`、`cache_creation_input_tokens`，未知不记零，缓存字段不并入 `input_tokens`（[PRD §5.1](PRD-v1.md#51-c1-model-io)）。主循环将规范化 usage 原样写入模型结果、模型回合与持久游标。

但两个 C3 事件的 v1 usage 是封闭双键对象，只允许必填 `input_tokens`、`output_tokens`。合法 C1 缓存字段因此可在模型响应返回后被 `model.result` 的写前校验拒绝；仅修改该事件还会在 `run.model_turn` 再次被拒。

## 2 新方案

### 2.1 仅两个事件的新写采用 v2

| 项目 | 约束 |
|---|---|
| `input_tokens` / `output_tokens` | 保持原 v1 的必填及类型语义，本次不另行收紧 |
| `cache_read_input_tokens` / `cache_creation_input_tokens` | 各自可选，类型 `integer|null`，整数值非负；不得接受布尔值、负数或非整数数值 |
| 缺席与 null | 缺席表示未上报，不补 0/null；允许显式 null，不将未知改为零 |
| 用量统计 | 缓存字段不并入 `input_tokens`，不另加到既有 input+output token 汇总 |
| 封闭性 | usage 保持 `additionalProperties=false`，其余字段不放宽 |
| 版本记录 | 事件与 `event_versions(seq,type,version)` 旁表同事务提交，精确记 2；不在 payload 加版本键 |

`integer` 沿用现有 JSON Schema 数值语义（整数值数值可匹配），同时必须兑现新字段的非负约束；不得为此改变旧版本或其他事件的校验语义。两事件即使本次没有缓存字段，新写也采用 v2，不能按 payload 形状猜回 v1。其他事件保留原精确版本，不升级全局 `CURRENT_PAYLOAD_VERSION`。

### 2.2 旧版解释与回退

- 两事件的完整 v1 schema 保留不变，包括封闭双键 usage；v0 原 schema、原读取映射也不变。`run.model_turn` v0 的 usage 原本允许 `object|null`，不能借此收紧为双键。
- 新读取器识别这两个事件的 0/1/2，旧 payload/hash/版本原样读取，不补新默认值；未知版本、不可读版本旁表继续 fail-closed。不 UPDATE/DELETE 历史事件，不自动迁移、补写旧模型结果或重放已发模型调用。
- 机器副本须同步：`schemas/c3/event.json` 的 `$defs.payload` 仅更新这两个 current，`historicalPayloadV1` 保存其旧 v1，`legacyPayload` 不改。实现不得原地修改共享 usage 节点，反向扩张历史 schema。
- 最低新读取器是**包含本契约实现且支持上述逐事件版本的读取器**，不是仅凭应用版本号 `0.1.0` 判断。确切旧基线 a073 的 `EventLog` 打开路径在 WAL/schema 初始化前调用 `data_admission.check_connection`，其中 `_versions` 全库检查已登记版本；发现两个新 v2 会拒绝 `AdmissionRefused(incompatible)`。逐行读取还会由 `schema_for` 拒绝未知版。
- 上述只描述已核 a073 的准入/读取点，不推广到所有旧二进制；连接、restore access 和 SQLite 打开先于版本检查，不承诺全文件零接触，也不覆盖原始 SQLite 或绕过准入的调用。不得混用新旧写者；回退保留新读取器核验或使用升级前一致备份，不把含新 v2 的库降写成旧版。本次不重构全库准入协议。

## 3 为什么改

C1 已承认的缓存用量是模型事实，不能通过删除合法字段、放开任意 extras、改塞 content/cursor 或把未知金额改为零来消除提交错误。模型结果、回合和游标仍须同事务提交，工具只有在 assistant turn 已持久化后才能派发。

[event-payload-contract §6.1](event-payload-contract.md#61-可选字段与旧二进制读新库f2-起) 与 §12 存在发布前 optional 沿 v1 的历史例外。**本次两个事件明确不沿用该例外**：已有精确草稿/归档对象需要区分新旧数据，选择逐事件 v2 以保住旧 v0/v1 的解释和旧读取器拒新语义。这不是全局废除例外，也不规定未来所有可选字段一律升版。

## 4 影响哪些代码与验收

- 必改：`event_schema.py` 的两事件 current/v1/版本路由及新缓存字段约束、C3 JSON 镜像、相应真实契约/存储回归与必要 current 夹具。先固定原 v0/v1，再以独立节点建立 current v2；不改历史夹具为新形状来追绿。
- 回归而不预设重写：`loop.py` 的 result/turn/cursor 同事务；`_tasks_cursor_rebuild.py` 的完整 turn/hash/幂等；`RunCursor` 的原样 round-trip；EventLog 精确版本旁表、全库准入；replay/投影重建及 pause/resume 不增加模型请求或重复副作用。
- 摘要走独立 `SummaryStore`，不发这两个事件；其既有 input/output/micros 存储范围不扩成本次“缓存明细持久化”。普通与摘要用量均维持现有 pricing/budget 语义：不模拟的非零缓存档或缺价仍金额未知，已知 token 与未知 micros 分维度结算、未决占用不退款，不改价、不自动重试。
- 必须离线证明：两个真实 adapter 的受控 transport 使用同组缓存缺席/零/正数/未知用量用例；实际 loop 与独立 `commit_model_turn` 均在旧实现复现拒绝、在新实现保存完整用量与两个 v2；未知键、布尔/负整数/负整数值数值/非整数数值/缺 required 拒绝；第二事件或版本行失败时 result/turn/cursor 一起回滚，派发未发生。
- 兼容/负控：固定 v0/v1 schema、payload/hash/version；只两个事件升版、旧基线拒新、未来版本拒绝；分别退回任一事件、只改一份镜像、污染旧 schema、丢缓存字段、放开 extras、把未知费用记零均应触发对应失败。正常与立即失败后的内存/游标/终态/重开须分别核对，不能用后续成功掩盖前序漏账。
- 当前仅契约草案，无实现/测试通过声明。实施按风险分级完成定向红绿、负控、受影响域回归和独立复审；最终产品代码批次再执行全量、lock/ruff 与 required CI。既有真实评测报告、安全判据、失败与未运行组均保持原样。
