# 2026-11-18 · 恢复 B 完整受管导入执行链与恢复 C 隔离解除 / 服务恢复（G3-B06）

## 背景

在冻结契约 [restore-held-contract.md](../restore-held-contract.md)（`schemas/data/restore-v1.json`、`schemas/data/restore-registry-v1.sql`）与 [restore-release-contract.md](../restore-release-contract.md)（`schemas/data/restore-v2.json`、`schemas/data/restore-registry-v2.sql`）基础上，补齐受管备份导入、持久隔离、零副作用只读复核、未发布中止/同批续做（Restore B），以及用户复核确认后的两阶段隔离解除、主库确定性 append-only 对账与解除后数据库版本准入（Restore C），并与离线安全 GC（W15）完成融合收口。

## 关键设计与实现决策

1. **受管导入与持久隔离（Restore B，`src/tianshu/restore_manager.py`）**：
   - `plan()` 只读校验完整备份、源路径一致性、目标不存在或任意深度纯空目录骨架（非递归栈扫描，支持深层空目录）、四位置互不重叠与非创建式容量探测，输出域分隔 `plan_sha256`。
   - `apply()` 严格按 `P0 → P1(reserved) → P2(stage_owned + 4 窗口 .restore-token.json + D 实体锁流式复制核验) → P3(sealed_hold + 4 窗口 .restore-origin.json) → P4(publish_intent + 空目录后序清理 + 同卷原子发布) → P5(published_hold)` 推进，在产生任何暂存子项前先持久化 `StageOwnedV1`，在进入 `publish_intent` 后永久禁止 `abort`。
   - `review()` 在共享门禁/实体锁下以 `?mode=ro&immutable=1` + `PRAGMA query_only=ON` 打开静态快照（事先核验无 `-journal/-wal/-shm` 伴随恢复文件），零实例化业务 Store，默认全部正文脱敏并输出确定性 `BodySelectorV1` 与 `review_sha256`（`RestoreReviewV2`），支持 `--body-scope` 按槽位披露及 `--later-backup` 差异比对；`review_sha256` 基于静态隔离根全脱敏投影（`body_scopes=()`、`known_later_changes=()` 且剥离可选外部 `later_backup` 通过检查项）计算，确保附带 `--later-backup` 或 `--body-scope` 产出的指纹可直接用于 `restore-release --expect-review`。
   - `abort()` / `resume()` 在批次操作独占锁下工作；一旦存在 `AbortIntentV1`，二者均只执行已冻结的 `CleanupPlanV1`（发现未知文件立即闭锁拒删，每次后序 unlink/rmdir 后对父目录及最终稳定父 `P` 执行目录持久化屏障）。
2. **双锚点隔离守卫、连续 `main` 准入归属与旁路封堵（B01–B05 + 第二读者复审加固）**：
   - `backup.restore_backup()` 收口走受管 `plan + apply`（停在 `published_hold`），根除无守卫直写恢复旁路。
   - `restore_contract.py` 保持 `RestoreFailureV1` / `RestoreResponseV1` 运行时严格限定 7 个 v1 `Phase`，新增 `RestoreFailureV2` 供 `RestoreResponseV2` 承载 9 阶段错误，保持 `contract_schema()` / `contract_schema_v2()` 与冻结 JSON Schema 100% 全等。
   - `restore_access.py` 核验本地见证（`.restore-origin.json` / `.restore-token.json` / `.tmp`）与固定用户 Home `RestoreRegistry` 的双向一致性，为 `migrate` 全部读写/探测入口（在 `sqlite3.connect` 前拦截以防热日志回滚）、`MetricsRecorder`、`RuntimeModelSlot`、`data_gc`、`exec.spawn`、`daemon`、`host`、`retention-audit`、`diagnostics_export` 统一接线隔离守卫，并通过 `ensure_material_write_allowed()` 在 `SnapshotStore`、`JournalStore`、`ContentStore`、`MemoryProjection`、`EventLog` 每个写 API 入口动态重验隔离态并绑定同根已初始化准入 `events.db` 的活跃连续 access（拒绝未初始化/0 字节主库先造标准材料及 `custom.db` 跨根/冒写标准材料）。
3. **无损向上迁移、确定性对账与受管 v2 准入（Restore C，`restore_contract.py` / `restore_registry.py` / `restore_manager.py` / `data_admission.py`）**：
   - `RestoreRegistry` `observe()` 双向零写兼容 `USER_VERSION = 1`（7 阶段）与 `USER_VERSION = 2`（9 阶段）；首次写入 `release_intent` 时在同一 `BEGIN IMMEDIATE` 事务内按 `sequence ASC` 暂存历史行、移除旧触发器与表、字面执行 `restore-registry-v2.sql` DDL 并原序回填历史字节（保持 `registry.db` 实体身份与 `sqlite_schema` 字面量逐字一致，避免 `ALTER TABLE RENAME` 引入双引号表名）。
   - `.restore-origin.json` 保持一次写入、磁盘永不改写；解除态唯一判据是 `RestoreRegistry` 对应批次尾事件为 `ReleasedV1` 且实时打开的 `(S, D, P)` OS 实体身份与 `OriginV1` 完整匹配。
   - 阶段 `R2` 在单一 SQLite 写事务内按冻结 `ReconcilePlanV1` 做 append-only 对账：可证未越界的 `intent` 归 `none_applied` 并释放对应预算预占，其余未决/执行中调用归 `unknown`（零重放、保留未知预算占用、追加 `recovery.blocked` / `recovery.object_blocked` 并同步本机 `authority`），陈旧 `pending/approved` 审批全部 `expired`、旧 `grants` 清零、`leases` 清空、非终态 `runs` `fenced`、非终态 `jobs` `cancelled`、非终态 `subtasks` `fenced`，随后 `wal_checkpoint(TRUNCATE)` + `fsync` 收敛为无伴随 `-wal` 的自包含文件。
   - 解除后数据库准入：`v2` 恢复库无 `.migration.json` 时，仅当 `inspect_connection` 结构/步骤审计通过且 `RestoreDatabaseAccess` 持有匹配当前 `(S, D)` 实体的有效 `ReleasedV1` 证明时放行（手工复制到新 inode 的同字节 `v2` 库仍以 `migration_incomplete` 拒绝，且 `tianshu data gc` 对已解除 `v2` 恢复库与保留合法 `stage_owned` `.restore-token.json` 的数据根无缝支持）；`v1` 恢复库即使已 `released` 仍强制拒绝普通 `EventLog` 打开，须显式执行 `tianshu data backup` + `tianshu data migrate` 升至 `v2` 后方可服务。

## 验证命令

- `uv lock --check`
- `uv run --frozen ruff check .`
- 定向契约与验收矩阵：`uv run --frozen pytest -q tests/test_windows_compat.py tests/test_restore_contract.py tests/test_restore_registry.py tests/test_restore_eventlog_access.py tests/test_restore_platform_access.py tests/test_restore_b_execution.py tests/test_restore_c_release.py tests/test_restore_bc_review_regressions.py tests/test_w15_safe_gc.py`
- 本机全量回归：`uv run --frozen pytest -o addopts="" -q -rs`
