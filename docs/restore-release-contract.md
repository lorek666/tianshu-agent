# 受管恢复解除与服务恢复 · C 契约

> **状态声明（2026-11-18）：本文为纯 `docs(contract)` 冻结契约，当前尚未接线产品 CLI / 代码实现。**
> 前序依赖：[受管备份导入、持久隔离与只读复核 · B 契约](restore-held-contract.md)、[数据准入与迁移绑定契约](data-admission-contract.md)、[PRD-v1.md](PRD-v1.md) §5.3.5 / §6 / §8.5。
> 机器契约副本：[schemas/data/restore-v2.json](../schemas/data/restore-v2.json)、[schemas/data/restore-registry-v2.sql](../schemas/data/restore-registry-v2.sql)（既有 v1 契约文件保持原字节不变以锚定 T0/T1 回归）。
> 原方案 → 新方案 → 为什么改 → 影响模块汇总见 [CONTRACT-PHASE2-G3-2026-11-18.md](CONTRACT-PHASE2-G3-2026-11-18.md) §1。

---

## 1. 边界与核心原则

1. **B 与 C 的职责分界**：
   - B 契约负责把已验证完整备份安全复制到原规范数据根 `ROOT`，停在持久隔离态 `published_hold`，并提供零副作用只读复核 `restore-review`、同批 `restore-resume` 与未发布前 `restore-abort`。
   - **C 契约负责 `published_hold` 之后的唯一合法出路**：在用户完成只读复核并显式确认「备份创建后的遗忘/擦除/删除历史不可证明完整」后，通过固定用户 Home 权威注册表（`RestoreRegistry` v2）的两阶段控制事实（`release_intent → released`）与主库 append-only 确定性对账收口，解除恢复隔离并按版本规则移交服务准入。
2. **`.restore-origin.json` 一次写入、磁盘永不改写**：
   - 随 B 阶段 `P3/P4` 落入数据根的 `.restore-origin.json`（`OriginV1`）只记录不可变的 `(version=1, registry_id, batch_id, sealed_sha256, binding_sha256)` 见证，**绝不新增或原地修改 `released: true` 布尔字段，也不在解除时删除该文件**。
   - 一个含有 `.restore-origin.json` 的数据根是否已解除隔离，**唯一判据**是固定 OS 用户 Home 下的 `RestoreRegistry` 存在完整合法哈希链、对应该 `(registry_id, batch_id)` 的尾事件为 `released`（`ReleasedV1`），且当前打开的根 `S` 与主库 `D` 的实时 OS 实体身份（POSIX `device+inode` / Windows `volume_serial+file_id_hex`）与 `ReleasedV1` 完全一致。
3. **不自动重放未知副作用、不静默升级中间态数据库、不自启后台常驻**：
   - 导入库中的未决操作在解除时全部按红线 1/2/5/11 做 append-only 终结或阻断保护，**零模型调用、零工具执行、零盲目重试**。
   - `restore-release` 完成后不自动拉起 `daemon` 或 `host`。

---

## 2. `RestoreRegistry` v2 与无损向上迁移

### 2.1 DDL 与阶段封闭集

- 固定路径不变：仍由 OS 原生账户资料定位器取得真实用户 Home 下的 `.tianshu/restore-registry-v1/registry.db`（不因协议升 v2 另建第二套目录而割裂历史批次权威）。
- `APPLICATION_ID = 1414746673`，新结构版本 `USER_VERSION = 2`（完整 DDL 见 [schemas/data/restore-registry-v2.sql](../schemas/data/restore-registry-v2.sql)）。
- `restore_events.kind` 封闭集由 7 项扩展为 **9 项**（仍受 `UNIQUE (batch_id, kind)` 与只追加触发器约束）：
  - 导入与隔离链：`reserved → stage_owned → sealed_hold → publish_intent → published_hold → release_intent → released`
  - 未发布中止链：`reserved | stage_owned | sealed_hold → abort_intent → aborted`
- 任何其他转移（例如从 `sealed_hold` / `publish_intent` / `aborted` 进入 `release_intent`，或从 `release_intent` 进入 `abort_intent`）在 `validate_event_chain()` 与写入入口一律按 `record_corrupt` 拒绝。

### 2.2 `v1 → v2` 兼容读取与原子原地升版

1. **只读观察（`observe()`）双向兼容且零写**：
   - 当 `PRAGMA user_version == 1` 时，按 `schemas/data/restore-registry-v1.sql` 校验 `sqlite_schema` 全部表与触发器 DDL，并按 v1 7 阶段校验事件链；
   - 当 `PRAGMA user_version == 2` 时，按 `schemas/data/restore-registry-v2.sql` 校验 `sqlite_schema` 全部表与触发器 DDL，并按 v2 9 阶段校验事件链；
   - 其它任何 `user_version` 或同名触发器/表 DDL 篡改一律抛 `RestoreRegistryCorrupt`。
2. **写入 `v2` 控制事实时的单事务无损升版**：
   - 当持有批次操作独占（或 `restore_gate` 独占）及 `registry.db` 的 `namespace + entity` 独占锁准备写入 `release_intent` 时，若当前 `user_version == 1`，在同一 SQLite 写事务内：
     1) 核验既有全链及 `registry_meta` 完整性；
     2) 在同一 `BEGIN IMMEDIATE` 事务内按 `sequence ASC` 读取暂存全部既有 `restore_events` 行（`event_bytes`、`event_sha256`、`previous_sha256` 逐字节不变），移除引用 `restore_events` 的触发器（`registry_events_*` 与 `registry_head_advance_guard`），`DROP TABLE restore_events` 后直接原字面执行 `restore-registry-v2.sql` 中的 `CREATE TABLE restore_events (...) STRICT` 与全部触发器 DDL，并按原序插回既有行（注：不使用 `ALTER TABLE restore_events_v2 RENAME TO restore_events`，因 SQLite 会在 `sqlite_schema.sql` 中把表名改写为带双引号的 `"restore_events"`，导致与字面量 DDL 逐字核验不一致）；
     3) 设置 `PRAGMA user_version = 2`，并在 `COMMIT` 前重核 `sqlite_schema` 与 `_expected_schema_rows(version=2)` 逐字相等。
   - 因全程在同一连接同一事务内原地完成，`registry.db` 的文件实体身份（inode / Windows FileId）与 `registry_meta` 保持 100% 不变；提交前崩溃自动回滚回完好的 `v1`。

### 2.3 规范编码与摘要域扩展

- 沿用 `restore-json-v1` 严格规范字节编码（禁浮点、禁重复键、ASCII 排序紧凑输出）与域分隔前缀 `b"tianshu.restore.v1\x00" + label + b"\x00"`，保证既有 13 个摘要域（`meta`、`event`、`plan`、`inventory`、`sql-row`、`sql-relation`、`sql-schema`、`scope`、`binding`、`cleanup`、`token`、`origin`、`lock`）的摘要值与 v1 逐位相同。
- `DIGEST_LABELS` 增补两个新域（见 `schemas/data/restore-v2.json` `$defs.DigestDomainV2`）：
  - `"review"`：对只读复核报告规范主体（`body_scopes=[]` 脱敏态下的 `RestoreReviewV1` 确定性投影）计算 `review_sha256`，由 `restore-review` 输出并由 `restore-release --expect-review` 强制核验；
  - `"reconcile"`：对 `ReconcilePlanV1` 计算 `reconcile_plan_sha256`，绑定 `ReleaseIntentV1` 与 `ReleasedV1`。
- `RestoreEventV2` 对前 7 种 B 阶段事件保持 `"version": 1`，对新增的 `"release_intent"` 与 `"released"` 使用 `"version": 2`。

---

## 3. CLI 与控制面契约

```text
tianshu data restore-release \
  --data-root ROOT \
  [--batch BATCH_UUID] \
  --expect-review REVIEW_SHA256 \
  --acknowledge-unproven-later-history \
  [--json]
```

### 3.1 参数与退出码

- `--data-root ROOT`（必填）：待解除隔离的规范恢复数据根路径。
- `--batch BATCH_UUID`（可选）：显式指定批次 UUID。省略时从 `ROOT/.restore-origin.json` 与固定 `RestoreRegistry` 唯一解析；若传入值与 `ROOT` 绑定的批次不一致，报 `binding_mismatch`（退出 `2`）。
- `--expect-review REVIEW_SHA256`（必填）：64 位小写十六进制 SHA-256，必须等于当前 held 现场实时重算的 `review_sha256`。
- `--acknowledge-unproven-later-history`（必填布尔开关）：显式确认已知晓 `later_forget_delete_history == "completeness_unproven"`（旧备份无法证明备份点之后的 `memory.forget` / `memory.erase` / 文件删除是否完整）；缺失时在任何写操作前拒绝（退出 `2`）。
- `--json`（可选）：输出符合 `schemas/data/restore-v2.json` `$defs.RestoreResponseV2`（`operation="restore-release"`, `version=2`）的单行/格式化 JSON。
- **退出码**：
  - `0`：成功完成解除并持久化 `ReleasedV1`（或同参数对已 `released` 批次的幂等重入确认）；
  - `2`：前置条件不满足、批次不在 `published_hold`/`release_intent`/`released`、`review_complete == False`、`expect_review` 不匹配、实体身份或 `BindingV1` 失配、锁忙或权威损坏；
  - `77`：命中显式故障注入点（仅 `TIANSHU_FAILPOINT` 设置时触发）。

---

## 4. 解除执行协议、确定性对账与故障点

### 4.1 阶段 R0：独占准入与全量复核（写前门禁）

1. 在 gate 外读取完整 `RestoreRegistry` 快照，定位目标 `batch_id`；只允许当前尾阶段为 `published_hold`、`release_intent`（中断续做）或 `released`（幂等确认）。其他阶段一律拒绝（退出 `2`）。
2. 按固定顺序取得批次操作独占：`acquire_restore_gate(exclusive=True)` → `final_database` canonical `namespace` exclusive → 打开并校验稳定父目录链 `P`、最终根 `S`、主库 `D`（单硬链接普通文件）并取得 `entity` exclusive，复核 `RestoreRegistry` 未过期后释放 `gate`，全程保持 `namespace + entity` 独占至命令返回。
3. 核验 `ROOT` 无活跃 `daemon` / `host` / `worker`（锁探针忙则立即报 `maintenance_busy` 退出 `2`）。
4. 核验 `ROOT/.restore-origin.json` 字节精确等于 `PublishIntentV1` / `PublishedHoldV1` 绑定的 `OriginV1`（磁盘永不改写该文件）。
5. **当尾阶段为 `published_hold` 时**：
   - 重核 `(S, D, P)` 实体身份与 `SealedHoldV1.binding`（`BindingV1`：双向 `copy_inventory`、`database_bytes`、`sqlite_scope` / `scope_sha256`）逐项一致；任何字节或 inode/FileId 漂移立即报 `binding_mismatch`；
   - 在同一静态只读快照上重算复核报告，硬性要求 `review_complete == True` 且 `review_sha256 == expect_review`（`review_complete == False` 报 `incompatible`；哈希不符报 `binding_mismatch`）；
   - 检查 `registry.db` 所在卷与 `ROOT` 所在卷的实际可用空间大于已知待写控制/对账字节（未知或 0 报 `capacity_unavailable`；**不套用新建 `effects` Run 的 2 GiB 门槛**）；
   - 读取 `D` 的 `PRAGMA user_version`（仅允许 `0, 1, 2`，高于 `2` 报 `incompatible`）及当前 `MAX(seq)`（`pre_release_event_seq_head`），生成确定性对账计划 `ReconcilePlanV1`（含固定毫秒时间戳 `reconciled_at_ms` 及排序后的待处理对象列表）。

### 4.2 阶段 R1：持久化 `ReleaseIntentV1`（`FP-RELEASE-INTENT`）

- 触发故障注入检查点 `FP-RELEASE-INTENT`（提交前/后窗口）以验证中断恢复。
- 若 `RestoreRegistry` 仍为 `USER_VERSION = 1`，按 §2.2 同事务无损升至 `USER_VERSION = 2`，并以 CAS 追加 `kind = 'release_intent'`（`ReleaseIntentV1`，绑定 `published_hold_sha256`、`binding_sha256`、`review_sha256`、`acknowledge_unproven_later_history=True`、`(S, D, P)`、`origin_sha256`、`pre_release_db_user_version`、`pre_release_event_seq_head`、`reconcile_plan` 与 `reconcile_plan_sha256`）。
- 一旦进入 `release_intent`，普通业务连接在 `_active_scope_blocks` 下仍然被严格阻断（视为未终结恢复范围）。

### 4.3 阶段 R2：主库单事务 append-only 确定性对账收口（`FP-RELEASE-RECONCILE`）

在仍持有批次操作独占的前提下，对主库 `D` 开启单个 SQLite 写事务（**绝不执行 `UPDATE/DELETE events`、绝不修改 `PRAGMA user_version`、绝不重放任何工具或模型调用**），严格按 `ReleaseIntentV1.reconcile_plan` 顺序执行：

1. **未决工具意图与派发（`pending_intents` / `dispatches`）——守死红线 5/11**：
   - 对 `dispatches.state == 'intent'` 且 `boundary_protocol` 可确定证明从未越过执行/外发边界的调用：追加 `tool.result`（`status="failed"`, `effect_state="none_applied"`, `output={"reason": "restore.reconciled_unsent", "batch_id": batch_id}`）并将 `dispatches.state` 置为终态；
   - 对其余一切未收尾 `tool.intent`、`dispatches.state == 'executing'` 或历史已标 `unknown` 的派发：**绝不重放、绝不猜 `none_applied`**；若尚无 `tool.result` 则追加 `tool.result`（`status="unknown"`, `effect_state="unknown"`, `output={"reason": "restore.reconciled_unknown", "batch_id": batch_id}`），同时对涉及的工作区或 MCP 对象（若尚未处于阻断态）追加 `recovery.blocked` / `recovery.object_blocked` 事件并同步登记本机用户级 `authority` 阻断库。
2. **陈旧审批、票据与排队请求（`pending_approvals` / `grants` / `dispatch_requests`）——守死红线 4**：
   - 所有 `approvals.state IN ('pending', 'approved')` 的记录逐条追加 `approval.expired`（`{"approval_id": id, "reason": "restore.release_reconciled"}`），投影 `approvals.state` 更新为 `'expired'`；
   - 所有未进入终态的 `dispatch_requests`（`waiting` / `admitted`）标记为已失效终态，所有 `remaining_uses > 0` 的旧 `grants` 作废（过期截止置为 `<= reconciled_at_ms` 或清零可用次数），杜绝旧备份里的批准在恢复后被再次消费。
3. **陈旧工作区租约（`live_leases`）**：
   - 清空 `leases` 投影表中的导入残留行（原持有进程属于备份前历史时刻，不能冒充当前存活租约）。
4. **导入的非终态 `runs`、`jobs`、`subtasks` 与未决预算预占（`budget_reservations`）**：
   - 每个 `runs.state` 处于非终态（`created`/`queued`/`running`/`waiting_approval`/`paused`/`reconciling`/`cancelling`）的 Run：追加 `run.fenced`（`{"run_id": run_id, "task_id": task_id, "reason": "restore.release_reconciled"}`），将 `runs` 投影更新为 `state='fenced'`、`outcome='cancelled'`、`control_requested=NULL`、`effect_state` 按该 Run 已有及本事务补齐的 `tool.result` 真实推导（含 `unknown` 则为 `'unknown'`，含已生效写入则为 `'partial'`/`'applied'`，否则 `'none_applied'`），并清理对应 `tasks.active_run_id`；
   - 每个非终态 `jobs` 行（`state NOT IN ('done', 'cancelled')`）：追加 `job.finished`（`outcome="cancelled"`, `by="restore_release"`, `reason="restore.release_reconciled"`）并置 `jobs.state = 'cancelled'`；
   - 每个非终态 `subtasks` 行（`queued`/`running`/`cancelling`）：置 `state = 'fenced'`、`ended_at = reconciled_at_ms`、`error = "restore.release_reconciled"`；
   - 每条 `budget_reservations.state == 'reserved'` 的未决预占：仅对确证未发出的请求释放预占，其余一律按 `known_dimensions=set()`（`known=0`）保留未知占用，绝不按 `0` 退款。
5. 提交上述单一 SQLite 事务，随后执行 `PRAGMA wal_checkpoint(TRUNCATE)` 并 `fsync` 主库文件句柄，使 `D` 收敛为无残留活动 `-wal` 的自包含一致文件。此处覆盖故障注入点 `FP-RELEASE-RECONCILE`。

**中断重入幂等性**：若进程在 `ReleaseIntentV1` 提交后、`ReleasedV1` 提交前硬退（`FP-RELEASE-INTENT` 提交后或 `FP-RELEASE-RECONCILE` 前/后），下次执行 `restore-release` 读取同一 `ReleaseIntentV1`：
- 若 `D` 的 `MAX(seq) == pre_release_event_seq_head` 且字节仍匹配 `BindingV1`，则按原 `reconcile_plan` 执行一次阶段 R2；
- 若 `D` 的 `1..pre_release_event_seq_head` 事件前缀不变，且 `pre_release_event_seq_head + 1 .. MAX(seq)` 与投影精确等于 `reconcile_plan` 的确定性产物，则跳过重复写入直接执行 `wal_checkpoint(TRUNCATE)` 进入阶段 R3；
- 任何第三种现场均报 `record_corrupt` / `binding_mismatch` 闭锁保全，不猜不洗白。

### 4.4 阶段 R3：持久化 `ReleasedV1`（`FP-RELEASED`）

- 重核 `(S, D, P)` 实体身份与 `.restore-origin.json` 未变，计算收口后自包含主库 `D` 的 `post_release_database_bytes`、`post_release_db_user_version`（等于 `pre_release_db_user_version`）与 `post_release_event_seq_head`。
- 触发故障注入点 `FP-RELEASED`（提交前/后窗口），在 `RestoreRegistry` 中以 CAS 追加 `kind = 'released'`（`ReleasedV1`）。
- 返回 `RestoreResponseV2(version=2, operation="restore-release", ok=True, result=RestoreReleaseResultV2(...))`：
  - `db_user_version == 0` → `migration_required=False`, `post_release_admission="admitted_v0"`, `next_commands=[]`；
  - `db_user_version == 2` → `migration_required=False`, `post_release_admission="admitted_v2_released_proof"`, `next_commands=[]`；
  - `db_user_version == 1` → `migration_required=True`, `post_release_admission="requires_explicit_migrate_v1"`, `next_commands` 给出先 `tianshu data backup` 再 `tianshu data migrate` 的精确后续命令。

---

## 5. 解除后数据库准入规则（Post-Release Database Admission Rule）

解除隔离后，所有连接入口（普通 `EventLog`、守护进程 `daemon`、离线 `backup` / `migrate` / `gc`）按以下精确规则协同判定：

1. **恢复范围裁决（`restore_access._active_scope_blocks` 与 origin 核验）**：
   - 未终结阶段（`reserved` / `stage_owned` / `sealed_hold` / `publish_intent` / `published_hold` / `abort_intent` / `release_intent`）继续一律阻断普通打开（报 `RestoreScopeBusy` / `held`）。
   - 尾阶段为 `released` 的批次不再阻断目标范围，**但必须满足双向实体与见证核验**：
     - 若目标根存在 `.restore-origin.json`，必须在固定 Home `RestoreRegistry` 中找到对应 `(registry_id, batch_id)` 的完整有效链且尾事件为 `ReleasedV1`，`.restore-origin.json` 字节匹配 `OriginV1`，且当前打开的根目录 `S` 与数据库文件 `D` 的实时 OS 实体身份精确等于 `ReleasedV1.final_root.identity` 与 `ReleasedV1.final_database.identity`（否则立即按 `held` / `record_corrupt` / `binding_mismatch` 拒绝！注意：普通业务写入会合法推进 `D` 的文件大小与内容哈希，故日常打开核验的是不可变的 `(S, D)` 实体身份 + `OriginV1` + `ReleasedV1` 链，而非冻结 `post_release_database_bytes`）。
2. **`v2` 数据库免 `.migration.json` 受管准入（`data_admission.check_connection`）**：
   - 备份按设计不携带 `<db>.migration.json`，`restore-release` 也绝不伪造一份假的 `.migration.json`。
   - 当 `snapshot.mechanism_version == 2` 且 `snapshot.progress is None` 时：若且唯若 `inspect_connection` 已通过 `_versions(conn)` 与 `_mechanism(conn, 2)` 完整结构/索引/步骤审计校验，**且**本次连接的 `RestoreDatabaseAccess` 持有与当前实时打开句柄 `(S, D)` 实体身份绑定的有效 `ReleasedV1` 注册表证明，`check_connection` **予以准入放行**。
   - 任何既无合法 `completed` `.migration.json`、又无匹配 `(S, D)` 实体的 `ReleasedV1` 注册表证明的 `v2` 数据库，继续以 `AdmissionRefused("migration_incomplete", "v2缺少合法completed进度")` 拒绝。
3. **`v1` 数据库仍强制要求显式离线迁移**：
   - 当 `snapshot.mechanism_version == 1` 时，**即使已有合法的 `ReleasedV1` 证明**，普通 `EventLog` / `daemon` 连接在 `check_connection` 处**仍然以 `AdmissionRefused("migration_incomplete", "v1是迁移中间版本")` 拒绝**。
   - 用户必须先对已 `released` 的数据根运行 `tianshu data backup` 生成升级前一致备份，再运行 `tianshu data migrate --data-root ROOT --backup BACKUP` 将其显式推进到 `v2`（产出真实 `.migration.json`），随后方可正常启动服务。
4. **`v0` 数据库准入不变**：
   - 当 `snapshot.mechanism_version == 0` 且无冲突迁移进度时，通过上述 `ReleasedV1` 范围与实体核验后按既有 `v0` 兼容路径放行。

---

## 6. 独立验收矩阵（待实现 PR 逐项提供实跑证据，当前 `NOT_RUN`）

| 编号 | 必验正控与必红负控 |
|---|---|
| C01 | `published_hold` → `restore-review` 取得 `review_sha256` → `restore-release --expect-review ... --acknowledge-unproven-later-history` → `ReleasedV1` 落库，`.restore-origin.json` 字节前后零变化 |
| C02 | 缺 `--acknowledge-unproven-later-history`、`expect_review` 错 1 bit、`review_complete=False`、或 held 期间篡改任一材料/主库字节或替换 inode，均在 `ReleaseIntentV1` 前以退出码 `2` 拒绝且零写 |
| C03 | 既有 `RestoreRegistry` v1 库在首次 `restore-release` 时无损原子升至 `USER_VERSION = 2`，历史批次 `event_bytes`/`event_sha256` 逐字节不变 |
| C04 | 导入含非终态 `runs`、`pending/approved` 审批、存活 `leases`、未决 `intent`（含可证未发与 `executing`/`unknown` 两类）的备份：解除后 `runs` 全 `fenced`、旧审批全 `expired` 不可再消费、`leases` 清空、`unknown` 原样保留零重放且受影响工作区/MCP 对象已登记阻断 |
| C05 | 三个故障点 `FP-RELEASE-INTENT`、`FP-RELEASE-RECONCILE`、`FP-RELEASED` 真子进程 `os._exit(77)` 注入：中间态普通 `EventLog` 打开仍被拒；同命令重入幂等收敛到唯一 `ReleasedV1`，不产生重复 `run.fenced` / `approval.expired` / `tool.result` 事件 |
| C06 | 解除后数据库准入正负控：`v2` 恢复库无 `.migration.json` 凭 `(S, D)` 绑定 `ReleasedV1` 正常打开 `EventLog` 并继续追加新业务事件；手工复制一份同字节 `v2` 库到新 inode（无对应 `ReleasedV1` 实体绑定）打开必被 `migration_incomplete` 拒绝；`v1` 恢复库在 `released` 后直接打开 `EventLog` 仍被 `migration_incomplete` 拒绝，须经 `data backup` + `data migrate` 后方可正常打开 |
