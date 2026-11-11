# 事件 payload 契约与历史兼容（PRD §5.3.5 第 1–2 条）

- 工作流：**W5 · event-history**；分支 `feat/event-payload-contract`；基线 `main @ da84d92`。
- 范围：逐类型 payload 契约、精确版本、写入前校验、旧版映射与夹具。
- 判定口径沿用本轮规格 §0：**有回归测试 + 负控（回退修复后测试会红）才算做完**；
  「要求写进 PRD」≠「已实现」；读不懂就报 unknown，不猜。

## 1 现在到底冻结了什么

| 维度 | 冻结前（`da84d92`） | 本 PR 之后 |
|---|---|---|
| 事件类型 | `EVENT_TYPES` 封闭（41 个） | 不变（不新增类型） |
| payload 形状 | `schemas/c3/event.json` 里是 `type: object`，**任意字段** | 41 个类型各有 v1 schema：`required`、`additionalProperties: false`、嵌套对象/数组/封闭枚举 |
| 版本 | 无 | 事件旁表 `event_versions(seq,type,version)`；当前版本 `1`，冻结前 `0` |
| 写入校验 | 只校验类型名 | 类型名 + payload 契约；非法 payload 在**落 blob / 写库之前**被拒，错误带类型与字段路径 |
| 旧数据读取 | 只能"按现在的代码猜" | 旁表无记录的历史行 → `payload_version=0`，按 v0 映射读取；**不按新版默认补值** |

**能证明**：每个登记类型的新写入都满足一份机器可校验的字段契约；每条事件能精确
回答"它是哪一版 payload"；旧行无版本记录时按旧版读取；旁表读不出来/版本不认识时
明确报错而不是猜。
**不能证明**：见 §7「仍冻结不了的字段」——`tool.result.output` 等由 C2/模型原始输出
决定的开放对象没有冻结；v0 读取路径也不重新校验 payload（见 §6 的口径说明）。

## 2 契约实现与关键字子集

- 权威实现：`src/tianshu/event_schema.py`；契约文件：
  `schemas/c3/event.json` 的 `$defs.payload`（v1）与 `$defs.legacyPayload`（v0）。
  两者由 `tests/test_event_schema.py::test_schema_json_mirrors_python_contract`
  **逐字绑定**（JSON 是镜像，代码是权威）。
- dialect：`https://json-schema.org/draft/2020-12/schema`。别的 dialect（如 draft-07）
  在注册/加载时拒绝（`event_schema.require_supported_dialect()`）。
- 支持的关键字（白名单）：`type` / `properties` / `required` /
  `additionalProperties` / `items` / `enum` / `const` / `minimum` / `pattern` /
  `description` / `title`。**未实现的关键字在注册时抛
  `UnsupportedSchemaKeyword`（fail-closed）**，不是运行时静默忽略——写进契约
  却不生效比没写更糟。
- 注册期结构校验（`event_schema.validate_schema_definition()`）：根必须是
  `object` + `additionalProperties: false`；`required` 不得指向未声明字段；
  `pattern` 必须能编译；`type` 只能是 JSON 七种类型。
- 注册表绑定：`event_schema.ensure_registry(EVENT_TYPES)` 在 `events.py` 导入时
  执行，缺类型/多类型都拒绝；`tests/test_event_schema.py` 再次双向绑定。

## 3 类型清单与版本

当前所有 41 个类型的 payload 版本都是 **v1**（本 PR 引入）。下表列每个类型的
必填字段与"是否存在已知历史形状差异"（有差异的类型在 v0 映射里放宽，见 §6）：

| 类型 | 当前版本 | v1 required | v0 备注 |
|---|---|---|---|
| `approval.consumed` | v1 | `approval_id`, `request_digest`, `action_digest`, `decided_by` | 无已知形状差异 |
| `approval.decided` | v1 | `approval_id`, `request_digest`, `decision`, `decided_by` | 无已知形状差异 |
| `approval.expired` | v1 | `approval_id`, `reason` | 无已知形状差异 |
| `approval.requested` | v1 | `approval_id`, `request_id`, `request_digest`, `action_digest`, `action_subject`, `action`, `resources`, `rule_id`, `reason`, `expires_at` | 无已知形状差异 |
| `approval.revoked` | v1 | `approval_id`, `decided_by` | 无已知形状差异 |
| `approval.superseded` | v1 | `approval_id`, `reason` | 无已知形状差异 |
| `dispatch.admitted` | v1 | `request_id`, `request_digest`, `call_id`, `grant_id`, `intent_seq` | 历史形状差异（v0 放宽） |
| `dispatch.rejected` | v1 | `request_id`, `rule_id`, `reason` | 无已知形状差异 |
| `dispatch.requested` | v1 | `request_id`, `request_digest`, `action`, `resources`, `decision`, `rule_id` | 无已知形状差异 |
| `dispatch.revoked` | v1 | `approval_id`, `call_id`, `dispatch_id`, `reason` | 无已知形状差异 |
| `execution.started` | v1 | `run_id`, `task_id`, `goal_revision`, `lease_epoch`, `resources` | 无已知形状差异 |
| `grant.consumed` | v1 | `grant_id`, `request_digest`, `remaining_uses` | 无已知形状差异 |
| `grant.issued` | v1 | `grant_id`, `request_digest`, `subject`, `action`, `issuer`, `expires_at` | 无已知形状差异 |
| `job.claimed` | v1 | `run_id`, `job_id`, `claim_epoch`, `previous_epoch`, `owner_pid`, `owner_instance_id`, `deadline_at` | 无已知形状差异 |
| `job.enqueued` | v1 | `run_id`, `job_id` | 无已知形状差异 |
| `job.finished` | v1 | `run_id`, `job_id`, `claim_epoch`, `owner_pid`, `owner_instance_id`, `outcome` | 无已知形状差异 |
| `job.parked` | v1 | `run_id`, `job_id`, `claim_epoch`, `owner_pid`, `owner_instance_id`, `reason` | 无已知形状差异 |
| `job.requeued` | v1 | `run_id`, `job_id`, `claim_epoch`, `owner_pid`, `owner_instance_id` | 无已知形状差异 |
| `model.error` | v1 | `request_id`, `error` | 无已知形状差异 |
| `model.intent` | v1 | `request_id`, `attempt`, `messages_hash`, `context` | 历史形状差异（v0 放宽） |
| `model.result` | v1 | `request_id`, `content`, `finish_reason`, `tool_calls`, `usage`, `cost` | 无已知形状差异 |
| `policy.denied` | v1 | `request_id`, `request_digest`, `action`, `resources`, `rule_id`, `reason` | 无已知形状差异 |
| `receipt` | v1 | `receipt_id`, `run_id`, `call_id`, `tool_id`, `action`, `args_hash`, `effect_state`, `backend`, `issuer`, `issued_at`, `root`, `resource`, `before_state`, `after_state`, `intent_seq`, `blob_ref`, `evidence` | 历史形状差异（v0 放宽） |
| `recovery.blocked` | v1 | `workspace`, `call_ids`, `reason` | 无已知形状差异 |
| `recovery.unblocked` | v1 | `workspace`, `reason` | 无已知形状差异 |
| `run.control` | v1 | `run_id`, `task_id`, `action` | 无已知形状差异 |
| `run.cursor` | v1 | `run_id`, `goal_revision`, `lease_epoch`, `claim_epoch`, `completed_call_ids`, `pending_proposals`, `payload_hash` | 无已知形状差异 |
| `run.effect` | v1 | `run_id`, `task_id`, `effect_state`, `n_applied`, `n_unknown` | 无已知形状差异 |
| `run.end` | v1 | `outcome`, `effect_state`, `artifact`, `error`, `receipt_ids`, `tokens`, `tokens_unknown_calls`, `turns`, `unbilled_model_intents`, `budget_enforced` | 无已知形状差异 |
| `run.fenced` | v1 | `run_id`, `task_id`, `reason` | 无已知形状差异 |
| `run.finish` | v1 | `run_id`, `task_id`, `outcome`, `effect_state` | 无已知形状差异 |
| `run.model_turn` | v1 | `run_id`, `turn_id`, `seq`, `claim_epoch`, `finish_reason`, `usage`, `payload_hash` | 历史形状差异（v0 放宽） |
| `run.start` | v1 | `run_id`, `task_id`, `goal_revision`, `lease_epoch` | 历史形状差异（v0 放宽） |
| `run.state` | v1 | `run_id`, `task_id`, `from`, `to`, `reason` | 无已知形状差异 |
| `task.created` | v1 | `task_id`, `goal`, `goal_revision`, `lease_epoch`, `workspaces`, `exec_policy` | 无已知形状差异 |
| `task.revised` | v1 | `task_id`, `goal`, `goal_revision`, `lease_epoch`, `workspaces` | 无已知形状差异 |
| `tool.intent` | v1 | `call_id`, `tool_id`, `args`, `args_hash`, `request_id`, `request_digest`, `dispatch_id`, `grant_id`, `attempt_id`, `claim_epoch` | 历史形状差异（v0 放宽） |
| `tool.not_dispatched` | v1 | `call_id`, `tool_id`, `reason` | 无已知形状差异 |
| `tool.result` | v1 | `call_id`, `tool_id`, `status`, `effect_state`, `output` | 无已知形状差异 |
| `undo.completed` | v1 | `run_id`, `control_run`, `restored`, `deleted`, `entries` | 无已知形状差异 |
| `undo.failed` | v1 | `run_id`, `control_run`, `error` | 无已知形状差异 |

## 4 精确版本

- 版本旁表（不改 `events` 表、不 UPDATE 历史行）：

  ```sql
  CREATE TABLE IF NOT EXISTS event_versions (
      seq INTEGER PRIMARY KEY,
      type TEXT NOT NULL,
      version INTEGER NOT NULL
  );
  ```

- `EventLog.append` 与事件行**同一事务**写入版本行——分开提交会留下"没有版本
  记录的新事件"，读取时会被当成 v0，等于把新版语义伪装成旧版。负控
  `test_version_row_is_written_in_same_transaction` 用触发器让版本写入失败，
  断言事件行一起回滚。
- 写入时版本由 `event_schema.detect_version()` 判定：先按 v1 校验，通过 → 1；
  否则按 v0（封闭的历史并集）校验，通过 → 0；两边都不合法 → 拒绝。
  "旧版"不是免检门：v0 也是 `additionalProperties: false` 的封闭形状。
- 读取：
  - 旁表里没有该 seq（升级前写入的历史行）→ `version=0`；
  - 旁表**整个缺失/损坏** → `PayloadVersionUnavailable`（不能把"读不懂版本"
    洗成"是旧版"）；
  - 旁表里的版本本程序不认识（更新的二进制写的）→ `UnknownPayloadVersion`。
- **不回填历史行的版本**：不猜。所有历史行就是"没有精确版本记录 → v0"。

## 5 写入校验与读取兼容

`EventLog.append` 的顺序（全部在任何副作用之前）：

1. `_check_event_type(type)`：未登记类型拒绝（原行为不变）；
2. `event_schema.detect_version(type, payload)`：契约校验/版本判定，失败抛
   `PayloadContractError(event_type, path, message)`，消息里带
   `type=... path=$.xxx：...`；
3. 只有到这才 canonical 序列化、必要时落 blob；
4. 同一事务写 `events` 行 + `event_versions` 行。

读取（`read_run` / `read_after` / `read_types` / 投影重建 / `replay`）对 v0 与 v1
一视同仁：`Event.payload_version` 如实带出，payload 原样返回。`replay` 不因校验
升级失效——`test_replay_still_passes_after_contract_upgrade` 用真实 scripted run
跑 `replay()` 并断言 `problems == []`。

## 6 旧版（v0）映射表

v0 的构造规则（`event_schema.LEGACY_SCHEMAS`，对每个类型）：

1. `required` 清空（旧行缺字段 = "旧版本来就没有"，不补值）；
2. 合并**历史出现过的额外字段**（目前只有 3 个类型）：
   `run.start`：`workspace` / `a` / `x`；`run.end`：`x`；`model.intent`：`ok`；
3. 对少数"当前更紧、但旧版真实出现过"的字段放宽：
   `run.model_turn.finish_reason`（旧 adapter 的 `tool_calls`）、
   `run.model_turn.usage`（缺 `output_tokens`）、`run.start.limits`（部分键）、
   `tool.intent.tool_id`（历史工具名）/`args_hash`（非规范哈希）、
   `model.intent.messages_hash`、`receipt.receipt_id`/`action`/`args_hash`/`evidence`
   （旧回执形状，见下）；
4. 其余字段保持当前类型/枚举/嵌套要求。

夹具（`tests/fixtures/events/`，由 `event_schema` 生成并校验）：

- `minimal_v1.json`：41 个类型的 v1 最小合法样例（`tests/test_event_schema.py::
  test_minimal_v1_fixture_is_valid_and_round_trips`）。
- `legacy_v0.jsonl`：41 个类型各一行 `{type,payload,note}`，逐行可读；
  其中 **6 个类型**（`dispatch.admitted` / `model.intent` / `receipt` /
  `run.model_turn` / `run.start` / `tool.intent`）是"v1 拒绝、v0 接受"的真实历史
  形状差异；其余 35 行形状与当前一致，用来证明**"旁表无记录的历史行 → v0 读出"**
  这条迁移路径（`test_legacy_fixture_covers_every_type_and_is_readable`）。

**正面证据（旧 receipt 被正确识别为 v0，且语义不变）**：
`tests/test_evidence_keys.py` 里三处构造旧回执（`receipt_id="rcpt_legacy"` /
`"rcpt_old"`，`action` 放的是 tool_id，`evidence` 是 `["tool.result:succeeded"]`
这样的合成字符串数组，且没有 `root` / `intent_seq` / `blob_ref`）：

- `test_verify_receipts_flags_legacy_receipts_as_unverifiable[local_fs]`
- `test_verify_receipts_flags_legacy_receipts_as_unverifiable[memfs]`
- `test_a_database_with_old_style_events_reopens`

读取路径：`EventLog.append` → `detect_version()` 在 v1 下失败、在 v0 下通过 →
`event_versions` 记 `version=0` → `read_run` 的 `_versions_for()` 按 0 取版本 →
`Event(..., payload_version=0)`，payload **原样**返回；`verify_receipts()` 仍按
"缺 `root` → 无法核对"拒绝把它当成可信证据（本 PR 没有改变这个判定）。
这三个用例在加契约前后都绿，且现在会经过 v0 分支——是 v0 映射在真实旧夹具上的
正面证据，不是为通过而放宽。

## 7 仍冻结不了的字段（与原因）

| 位置 | 现状 | 原因 / 边界 |
|---|---|---|
| `tool.intent.args` | `object`，内部开放 | 参数形状由 C2 的**工具自己的** schema 决定（`tool_manifest()`）；本轮只冻结"是对象"，不复制第二份工具契约 |
| `tool.result.output` | `object`，内部开放 | 同上：每个工具的输出不同，且报告/命令输出是给人和下游读的开放载荷 |
| `tool.result.tool_id` | 字符串 `pattern`，**不是当前白名单 enum** | 恢复历史日志时会把旧版工具 id 原样写进新 result（红线 8：schema 变即新工具）；新 intent 仍由 dispatcher 用 `SPECS` 校验 |
| `model.result.tool_calls[].arguments` | **任意 JSON** | adapter 契约是"半截 JSON / null / 数组原样带出、不在 adapter 里修好"（C1 契约测试）；把它钉成 object 会与既有契约冲突 |
| 信封的 `actor.kind/id` | 信封层契约（`schemas/c3/event.json`），不在 payload | 存储层拆成 `actor_kind`/`actor_id` 两列；payload 不重复 |
| v0 读取路径 | 不重新按 v0 schema 校验 payload | 旧行的价值是"能读出来 + 知道它是旧版"；对历史数据做形状拒绝等于让升级后的程序读不了自己的历史。写入侧仍用 v0 做版本判定（封闭） |
| 每个工具的 output 子契约 | **未做** | 属于 C2 工具契约与后续评测范围；本轮不冒充"payload 已全字段冻结" |

一句话边界：**类型封闭 ≠ 字段契约完整**。本 PR 把"固定形状的部分"冻结了，
开放对象与历史兼容口径之上仍按上表如实登记。

## 8 判据、测试与负控

新增/调整的测试：

- `tests/test_event_schema.py`：**76** 条。41 个类型 ×（一正一反例）、v1/v0 夹具逐行、
  版本旁表（缺失/损坏/未来版本/无记录）、事务性、blob 路径、`replay` 兼容、
  schema JSON 镜像、枚举与代码常量三处绑定、关键字/dialect fail-closed。
- `tests/fixtures/events/minimal_v1.json`、`legacy_v0.jsonl`。
- 两处**授权**的既有测试最小改动（旧口径 → 新口径，断言强度不变）：
  1. `tests/test_events.py::test_every_registered_type_can_be_written_and_read_back`：
     旧=`{"probe": type}` 任意 dict 可写；新=每类型用 `event_schema.minimal_payload()`
     写读往返，并额外断言 `payload_version == 1`。仍遍历全部 41 个类型。
  2. `tests/test_credentials.py::test_event_db_actually_can_contain_text_...`：
     旧=`run.start` 写任意 `{"probe": sentinel}`；新=最小合法 `run.start` payload +
     `task=sentinel`，断言（sentinel 明文出现在 SQLite 字节里）未动。
- 全量（本分支，merge 最新 `main` 前）：`1391 passed, 6 skipped`，
  `ruff check .` 干净。

负控（真跑过，回退后红）：

1. **去掉写入校验**：把 `EventLog.append` 里的 `detect_version()` 换成直接记当前
   版本（`payload_version = 1`）→ `tests/test_event_schema.py` 红 6 条：

   ```
   FAILED tests/test_event_schema.py::test_invalid_payload_is_rejected_by_append_before_any_side_effect
   FAILED tests/test_event_schema.py::test_invalid_payload_with_large_size_leaves_no_orphan_blob
   FAILED tests/test_event_schema.py::test_invalid_payload_rolls_back_the_whole_transaction
   FAILED tests/test_event_schema.py::test_legacy_shaped_write_is_stamped_version_zero
   FAILED tests/test_event_schema.py::test_read_after_and_read_types_carry_versions
   FAILED tests/test_event_schema.py::test_legacy_fixture_covers_every_type_and_is_readable
   6 failed, 69 passed
   ```

2. **把旧版按新版默认补值**：在 `_row_to_event` 里对 `version == 0` 的 payload
   合并 `minimal_payload()` 的默认值 → 红 2 条：

   ```
   FAILED tests/test_event_schema.py::test_legacy_shaped_write_is_stamped_version_zero
   FAILED tests/test_event_schema.py::test_legacy_fixture_covers_every_type_and_is_readable
   2 failed, 73 passed
   ```

   这正是"不许按新版默认补值后假装原始记录如此"的机器判据。

复现命令：

```bash
PYTHONPATH=/Volumes/Date/ts-wt-events/src /Volumes/Date/天枢Agent/.venv/bin/python \
  -m pytest -o addopts="" -q tests/test_event_schema.py
/Volumes/Date/天枢Agent/.venv/bin/ruff check .
```

## 9 剩余 unknown / 未做（如实登记）

- **不把历史行补写成 v1**，也不回填版本：没有"原作者意图"可依据，猜就是造假。
- **版本旁表不在 W4 的备份/恢复覆盖范围内**（W4 负责 `backup.py`）；本 PR 只保证
  旁表与事件同事务。若备份只导事件表，恢复后所有行都会按 v0 读——W4 实现备份时
  需要把 `event_versions` 纳入一致点导出（接口已在本文登记，未跨工作流改代码）。
- **CLI 文案未专门区分** `PayloadVersionUnavailable` / `PayloadContractError`
  （`cli.py` 不在本工作流文件所有权内）：它们都是 `TianshuError` 子类，会按现有
  错误路径报出；是否折叠成人话由后续工作流决定。
- **Windows 未实机**：本 PR 没有平台相关逻辑（纯 sqlite/py），但按纪律不宣称在
  Windows 上验证过。
- **未实现** `oneOf` / `anyOf` / `$ref` / `format` 等关键字：注册时拒绝，不假装支持。
- 历史数据里"从未在测试中出现过"的旧字段组合：读取按 v0 原样给出（不校验），
  但**写入口**无法把它们识别成旧版（会拒绝）——这是刻意的：新写入必须满足当前契约。

## 10 变更影响面

- `src/tianshu/events.py`：新增导入期注册校验、`event_versions` 表、`append` 校验
  与同事务版本行、读取版本映射、`Event.payload_version`。既有读写 API 签名不变。
- `src/tianshu/event_schema.py`（新）：契约、校验器、版本判定、v0 映射、样例生成。
- `schemas/c3/event.json`：`$defs.payload` / `$defs.legacyPayload`（Python 契约的镜像，
  逐字绑定）。
- `tests/test_event_schema.py` + `tests/fixtures/events/`（新）；`tests/test_events.py`
  两处授权调整；`tests/test_credentials.py` 一处授权调整。
- 不新增事件类型、不改信封字段、不加运行时依赖。
