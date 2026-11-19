# 数据备份、校验、迁移、受管恢复与离线清理（PRD §8.5）

备份与显式迁移的早期分片见[数据生命周期](DATA-LIFECYCLE-2026-11-13.md)、[集成记录](DATA-ADMISSION-2026-11-13.md)及 [T2](DATA-ADMISSION-T2-2026-11-13.md)/[T3](DATA-ADMISSION-T3-2026-11-13.md)的冻结记录。§7–§8 保留 2026-11-11 原 W4 实验和负控输出。受管恢复 B/C 与 W15 离线 GC 的集成验证见 [PR #198](https://github.com/lorek666/tianshu-agent/pull/198)。

规格见 [PRD §8.5](PRD-v1.md)、[恢复 B](restore-held-contract.md)、[恢复 C](restore-release-contract.md)与 [W15](CONTRACT-W15-RETENTION-2026-11-15.md)。CLI 入口在 `data_cli.py`。库实现分属 `backup.py`、`migrate.py`、`restore_manager.py` 与 `data_gc.py`，协作式准入由 `maintenance.py` 与 `restore_access.py` 负责。

---

## 0. 先给结论（哪些成立、哪些仍不成立）

先关闭使用该数据根的 Host、Daemon 和内核 CLI；备份命令遇到活跃连接会退出 `2`，不会强停进程。所有命令都要求明确指定根或备份目录：

```bash
uv run --frozen tianshu data backup --data-root /absolute/data-root --dest /absolute/backups/backup-1 --json
uv run --frozen tianshu data verify /absolute/backups/backup-1 --json
uv run --frozen tianshu data restore-plan /absolute/backups/backup-1 --data-root /absolute/data-root --json
uv run --frozen tianshu data migrate --data-root /absolute/data-root --backup /absolute/backups/backup-1 --json
```

`backup` / `verify` 成功退出 `0`；`verify` 不完整退出 `1`；前置条件不满足退出 `2`。`restore-plan` 的检查通过退出 `3`，表示待人工复核，**没有执行恢复**；检查不通过退出 `2`。去掉 `--json` 可读人话输出。

`migrate` 必须同时给出 `--data-root` 与既有的 `--backup`，不会自动生成备份；成功退出 `0`，拒绝或执行失败退出 `2`，四个原故障注入点退出 `77`。JSON 失败含 `operation="migrate"`、`ok=false`、`code` 与 `error`；六类 code 为 `incompatible`、`migration_incomplete`、`progress_corrupt`、`backup_mismatch`、`maintenance_busy`、`migration_failed`。成功含 `ok=true`、`code=null` 和迁移库报告。JSON 及迁移文本统一脱敏；内部编程异常不伪装成这六类业务失败。

备份、迁移与受管恢复处理标准 `ROOT/events.db` 和根内材料。升级前如果曾用自定义数据库名或把 `data_root` 放在数据库目录之外，请保留旧数据库及其材料目录。新版本检查因默认布局调整而受影响的快照、撤销、内容和记忆目录，发现文件、链接或不可判定内容即拒绝。请先制作离线副本，再用兼容旧布局的版本复核未决事务。

`restore-plan` 通过时给出 `plan_sha256` 和 `apply_supported`。实际导入、隔离复核、解除与平台范围见 §4。报告会标明 `later_forget_delete_history=completeness_unproven`，因为备份只记录其一致点以前的历史。空间清理单独走 §6 的显式离线流程。

`verify` 校验 Receipt 的 blob 引用和写前快照。`restore-plan` 对全部事件做 payload 版本校验，核对 Run 全集身份与最终状态。缺行或矛盾时拒绝确定结论。早期校验问题及修复证据见[第二轮总账](DEBUG-ROUND2-2026-11-13.md)。

预演需要 `runs`、`approvals`、`leases` 三张投影及事件版本旁表可读。缺失或损坏时退出 `2`、`ok=false`、`reconciliation.complete=false`，不会将未能核对的状态报告为空。只有事件日志的旧库也不猜测重建成功；须先由匹配版本重建并验证必要投影。

| 宣称 | 状态 | 证据 |
|---|---|---|
| 备份取**一致点**（SQLite 在线备份 API，不复制正在写的库） | **已实现 + 有测试** | §2、`tests/test_backup.py` |
| `backup.json` 记录版本/每文件 sha256/引用清单/恢复须知/保留期 | **已实现** | §2.4 |
| 备份原子发布；中断的 `.partial-*` 目录**不会**被 `verify` 当有效备份 | **已实现 + 有测试（含子进程真崩溃）** | §2.3、§7 负控 |
| `verify_backup` 校验哈希/引用/事件 seq 连续/清单可解析/版本兼容 | **已实现 + 有测试** | §3 |
| `data restore-plan` 核验目标、备份、版本及未决项并生成计划摘要 | **只读，成功退出 `3`** | `tests/test_data_cli.py`、`tests/test_restore_b_execution.py` |
| `data restore-apply/review/resume/abort/release` | **受管导入、持久隔离、复核、续做与显式解除** | §4、[恢复 B](restore-held-contract.md)、[恢复 C](restore-release-contract.md) |
| 迁移有机制版本号、步骤注册表、严格进度、完整备份绑定与中断续做 | **T2/T3 已随 PR #136 交付；仅迁移机制，非恢复 apply** | §5、T2/T3 事实记录 |
| 迁移不 UPDATE/DELETE 历史事件；append-only 触发器保留 | **已实现 + 有测试** | §5 |
| 保留期声明 + 引用保护（被引用 blob 永不进清理候选） | **已实现 + 有测试** | §6 |
| **换路径恢复（重定位）** | **未实现** | §4.3：事件 `payload_ref` / 快照 `blob_ref` 可能是绝对路径 |
| 实际 EventLog 打开前的版本/进度准入 | **T1 已接线** | §5.5；正常 v0 兼容，不自动迁移 |
| **首次启动自动建库并迁移** | **未实现** | 新库 `user_version=0`，当前程序不会自动迁移；见 §5.5 |
| CLI 备份与正常 `EventLog` 连接互斥 | **本轮新增协作准入；不强停进程** | §2.2；直接调用低层在线 API 不自动独占 |
| Windows 用户设备、物理掉电与跨机器恢复 | **未验证** | 本机 macOS arm64 的文件系统与进程测试只覆盖各自场景。换路径及跨机重定位尚未实现 |

---

## 1. 备份 ≠ 快照 ≠ 撤销（先把语义分清）

| 机制 | 范围 | 目的 | 存在哪里 |
|---|---|---|---|
| **写前快照**（`snapshot.SnapshotStore`） | 一次 Run 的受控写入 | 让**撤销**能把**工作区文件**改回写前内容 | `<data_root>/artifacts/{snapshots,blobs,undo}` |
| **备份**（本模块） | 整个数据根 | 数据根丢失/损坏后，把**事件、投影、快照证据**恢复到一个干净根 | 用户指定的备份目录（不放在数据根里） |
| **撤销**（`undo.py`） | 一次 Run | 按快照证据改**工作区文件** | 直接作用在用户目录 |

三个结论必须记住：

1. **备份不含工作区文件**：恢复的是证据，不是用户目录。想恢复用户文件，需要有工作区
   自己的备份；备份只保证"恢复之后，撤销仍然知道该把文件改回什么"。
2. **恢复不是撤销**：`restore_backup` 不写任何工作区路径，只把数据根恢复到某个一致点。
3. **备份不替代事件日志**：事件日志仍是唯一事实源；备份只是它的一份可校验副本。

---

## 2. 一致点备份（`backup.create_backup`）

### 2.1 数据根里进备份的东西

| 数据根相对路径 | 进备份 | 类别 | 说明 |
|---|---|---|---|
| `events.db` | ✅ | `event_log` | **SQLite 在线备份 API** 取一致点，不是文件复制 |
| `artifacts/blobs/<sha256>` | ✅ | `blob` | 快照写前内容与内容对象，内容寻址、不可变 |
| `blobs/<sha256>` | ✅ | `artifact_other` | 内核 CLI 大事件 payload 的默认位置 |
| `memory_blobs/<sha256>` | ✅ | `artifact_other` | 记忆正文；未擦除记忆的正文引用须完整 |
| `artifacts/snapshots/<run>.json` | ✅ | `snapshot_manifest` | 撤销/恢复证据；原子整文件替换 |
| `artifacts/undo/<run>.json` | ✅ | `undo_journal` | 撤销意图日志（含未完成撤销） |
| `artifacts/**`（其它） | ✅ | `artifact_other` | 保守带上，宁可多一份证据 |
| `run/`（socket、锁、**`ipc.key`**） | ❌ | — | 认证密钥不进备份；daemon 首次启动会重建 |
| `logs/daemon.log` | ❌ | — | 诊断材料，保留期见 §6 |
| `metrics.jsonl` | ❌ | — | 本地指标 |
| `events.db-wal` / `-shm` | ❌ | — | 在线备份已给出单文件一致点 |
| 工作区里的用户文件 | ❌ | — | 不属于数据根 |
| `migration.json` / `events.db.migration.json` | ❌ | — | 旧进度/新绑定进度不进备份；仅有旧备份不等于取得新的迁移或恢复许可 |

### 2.2 一致性怎么来（以及窗口有多大）

**CLI 离线入口**在复制前对 canonical `ROOT/events.db` 取得独占维护准入。每个正常 `EventLog` 的真实 SQLite 连接持有共享准入直到连接关闭；共享连接可共存，独占与共享相互拒绝。连接被外部变量或游标继续持有时也不提前放锁；关闭失败且连接仍可写时保持准入。POSIX 使用 `flock`，Windows 使用 `LockFileEx`；两者均非阻塞。CLI 不启动服务，也不代替用户停止活跃进程。

锁位于固定的 OS 用户临时目录：POSIX 为 `/tmp/tianshu-maintenance-<uid>/<数据库身份摘要>.lock`；Windows 由原生已知目录 API 取得用户 LocalAppData，再用 `Temp/tianshu-maintenance/`。不按 cwd、数据根、`HOME` / `TMPDIR` / `LOCALAPPDATA` 环境变量或权限失败切换锁根；数据库上级目录不可写也不影响锁准入。数据根更换前后仍使用相同 canonical 数据库路径摘要。

锁不进入备份，正常释放不删除文件（删除会造成 inode 竞态）。POSIX 核对锁目录所有者并收紧为 `0700`、文件 `0600`；Windows 继承 OS 用户目录 ACL，拒绝最终目录的 symlink/junction，未单独核对 owner SID 或验证跨用户 ACL。锁是**协作式准入**，不抵御旧版本或同 uid 直接 SQLite；不作为沙箱或授权边界。清理这些锁文件前必须关闭所有天枢进程。

**低层 `create_backup()`** 保留在线 API，调用者负责离线准入；原有复制机制如下：

1. **事件库**：`sqlite3.Connection.backup()` 在线备份 API。写者不会被中断，拿到的是
   单个事务边界上的一致快照（不是"复制一个正在写的文件"）。
2. **材料**：blob 内容寻址不可变；快照清单 / 撤销日志用 `os.replace` 整文件替换。
   逐个复制读到的是"替换前"或"替换后"的完整文件，不存在半截。
3. **复制顺序是 DB 先、材料后**。写路径是"先落 blob，再写事件行"（`EventLog.append`
   的大 payload 也是先写 blob）。DB 先快照 → 之后复制的材料至少包含快照引用到的
   blob；反过来可能漏掉窗口内新写的 payload blob，而 DB 已经引用它（`verify` 会
   正确判备份无效）。
4. **低层 API 本身没有停止写者**。未持独占准入时，备份期间 daemon/host 可以继续跑；`backup.json` 用只读探针
   记录当时写者状态（`writer.ipc_probe` / `run/daemon.lock` / `run/host.lock`），
   供恢复者判断现场。**代价**：DB 快照与材料复制之间有一个窗口，材料可能是比 DB
   更新的超集（多出来的证据，不破坏一致性）；要严格跨库一致必须停止写者，本轮
   必须由调用者持独占准入；本轮 CLI 已持有。直接使用库函数仍有此边界。

发布前必须对临时备份做完整校验；缺材料的源数据不能发布一个“成功备份”。校验从**备份中的数据库**重新推导事件、内容对象与未擦除记忆的引用，不仅信任 `backup.json.references`。源目录已丢失或被符号链接替换时也不回源读取。

历史绝对引用可能使用同一数据根的另一种拼写，例如 macOS 的 `/tmp` 与 `/private/tmp`。创建备份时仅在活源上核对“别名根解析后等于 canonical 根、材料相对后缀完全一致”，再将验证过的根保存到可选 `source_aliases`。后续 `verify` 与 `restore-plan` 只按保存的根做词法映射并读取备份材料，不解析或读取活源。无此字段的旧 v1 备份仍可校验 canonical 引用；确实需要别名却缺映射时明确失败，不猜测根外材料属于该备份。

### 2.3 原子发布与中断

```
<dest>.partial-<uuid>/          ← 构建目录（含 .partial 标记）
  .partial                      ← 构建中标记；verify 看到它就拒绝
  backup.json                   ← 最后写
  events.db / artifacts/...
→ 去掉 .partial → fsync → os.replace(临时目录, dest)
```

- 目标已存在 → **拒绝**（本模块永不覆盖备份）。
- 构建中异常 → 清理临时目录后抛出；`os._exit` 注入的进程硬退留下带 `.partial`
  标记的目录，`verify_backup` 明确拒绝，不会被当成有效备份。
- `dest` 不允许在数据根里。

### 2.4 `backup.json` 字段

| 字段 | 内容 |
|---|---|
| `format` / `format_version` | `tianshu-backup` / `1`（本模块契约，独立于库结构版本） |
| `app_version` | 生成备份的程序版本（`tianshu.__version__`） |
| `db_user_version` | 事件库结构版本（`PRAGMA user_version`，由 `migrate.py` 管理） |
| `created_at_ms` / `source_name` / `source_path` | 时间 / 数据根名 / **数据根绝对路径**（恢复只能回原路径，见 §4.2） |
| `source_aliases` | 可选的已核对同根别名，默认为空；仅创建时发现，保存后的校验不回源解析。格式版本仍为 `1` |
| `files[]` | 每个文件的 `path`（数据根相对、`/` 分隔）、`sha256`、`size`、`category` |
| `event_count` / `event_seq_head` / `run_ids` | 一致点的事件统计与 Run 清单 |
| `references` | 事件 payload、Receipt材料、快照、撤销日志、内容对象和未擦除记忆的引用；兼容旧字段。校验时从备份材料重新推导，删除清单里的引用也不能隐藏缺材料 |
| `writer` | 备份时只读探到的写者状态（不是锁，不停止任何进程） |
| `excluded` | 明确排除的顶层条目 |
| `retention` | 保留期声明（§6） |
| `restore_notes` | 恢复须知（干净根、原路径、不含工作区/密钥、版本兼容） |

> 隐私：备份是**本地**材料（D7），但它包含事件 payload 与数据根绝对路径。
> 不要把备份上传到任何地方；共享前先脱敏。

---

## 3. 校验（`backup.verify_backup`）

返回 `BackupVerification(ok, problems, remediation, manifest, files_checked,
bytes_checked, db_user_version)`。`ok=False` 时 `require_ok()` 抛 `BackupInvalid`，
message 里带**问题 + 处置建议**。

| 检查 | 拒绝条件 |
|---|---|
| 目录/标记 | 目录不存在 / 不是目录 / 含 `.partial` 标记或名字是 `.partial-*` |
| 清单 | `backup.json` 缺失、不是 JSON、`format` 不认识、`format_version` 更新、缺 `source_path` |
| 文件集合 | 清单声明的文件缺失；备份里有清单之外的文件 |
| 文件内容 | 大小或 sha256 与清单不符 |
| 事件库 | 打不开 / 结构版本不符 / 事件条数或 `seq_head` 不符 / **seq 不连续** |
| 引用 | 快照/撤销日志/blob、事件 payload、内容对象或未擦除记忆正文缺失、哈希/大小不符；不支持的根外布局或畸形材料；清单隐藏引用无效 |
| 版本 | `db_user_version > migrate.SCHEMA_VERSION` → 拒绝（来自更新版本） |

明确不做：把"读不出来"当成"空历史"。清单/日志/事件库读不出来一律 `ok=False`。

已保存的自包含快照用专用 immutable 只读连接，避免 WAL 模式数据库在校验时生成 `-wal` / `-shm`。备份内的数据库伴随 `-wal` / `-shm` / `-journal` 即便写进清单也拒绝；不能忽略未折叠事务后声称完整。活源读取仍使用普通只读连接，SQLite 在线备份 API 会包含尚未 checkpoint 的已提交 WAL 事务，不把活源当 immutable。

---

## 4. 受管恢复（回到备份记录的原路径）

### 4.1 从导入到复核

受管写入需要支持目录 `fsync` 的 POSIX 文件系统。Windows 的计划与符合隔离绑定的复核保持只读，计划返回 `apply_supported=false`。有效的导入、写入型续做、中止和解除返回 `durability_unavailable`（CLI 退出 `2`）。收到该错误时保留原数据根、暂存目录和注册表。Windows 目录持久化缺口见 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。

关闭使用该根的服务与 CLI。保留原数据现场和独立备份。目标必须不存在，或仅含全空的目录骨架，且与备份的原数据根一致。`verify` 和 `restore-plan` 检查通过后，核对计划的备份路径、目标路径与未决项，再从预演 JSON 取得 `plan_sha256`。

```bash
uv run --frozen tianshu data restore-apply /absolute/backups/backup-1 \
  --data-root /absolute/data-root --expect-plan "$PLAN_SHA256" --json
uv run --frozen tianshu data restore-review --data-root /absolute/data-root --json
```

`restore-apply` 成功进入 `published_hold`。恢复根继续隔离，普通业务入口和服务仍被拒绝。复核校验绑定、材料及未决历史，默认隐藏正文。可用 `--body-scope` 按报告的选择器披露指定正文，用 `--later-backup` 对照已知后续变化。含正文的报告请只在本地查看。

导入中断后，用 `restore-resume --data-root ROOT` 续做同批次。进入 `publish_intent` 前也可用 `restore-abort --data-root ROOT` 按冻结计划清理该批次。已有中止意图时，续做只会完成清理。`--batch` 可显式绑定报出的批次。保留恢复标记、注册表与暂存目录，由管理命令核验与推进。

### 4.2 复核确认后解除隔离

仅在 `review_complete=true` 且人工核对结果后，从复核 JSON 的 `result.review_sha256` 取得摘要。以下命令明确确认备份点后的遗忘、擦除和删除历史无法证明完整。只有接受该限制时才执行。

```bash
uv run --frozen tianshu data restore-release --data-root /absolute/data-root \
  --expect-review "$REVIEW_SHA256" --acknowledge-unproven-later-history --json
```

解除时追加确定性对账事件，封存旧的非终态 Run，使陈旧审批失效并清理旧授权与租约。未知效果留待人工核查。在支持写入的平台上，若解除中断，以同一复核摘要和确认开关重跑 `restore-release`。父目录持久化失败时，发布和清理仍处于隔离批次中。检查错误报告中的保留路径，使用同一批次续做。除预演外，各恢复管理动作完成退出 `0`，拒绝或操作失败退出 `2`。读取失败响应中的批次、阶段与建议命令以确定是否需要续做。

解除后 v1 库须按返回的下一步命令显式 `data backup` 和 `data migrate`，完成后才可启动服务。已解除的 v2 库只有在结构校验和当前实体的已解除证明均成立时，才可免迁移进度文件。低层 `backup.restore_backup()` 也走受管导入，成功后停在隔离态。

### 4.3 路径与验收范围

事件 `payload_ref` 与快照 `blob_ref` 可能记录绝对路径。恢复使用原路径，校验读取备份时识别已核对并保存的同根别名。跨路径重定位仍未实现。§7–§8 的原 W4 库函数实验属于历史记录。本机故障注入和原生 CI 的取证范围见[集成 PR](https://github.com/lorek666/tianshu-agent/pull/198)与[B09 持久化记录](changelog/2026-11-19-restore-b09-durability.md)。设备掉电持久性仍待实测。

---

## 5. 版本化迁移（`migrate`）

### 5.1 结构版本与步骤

- 权威版本号：`PRAGMA user_version`；当前目标 `SCHEMA_VERSION = 2`。
- `STEPS` 是首尾相接的 `MigrationStep` 注册表。每一步在自己的事务里执行
  DDL **并** `PRAGMA user_version = to_version` —— 两者同提交、同回滚，
  不存在"表改了版本没改"的混合状态。
- 本版步骤是迁移机制的演示登记，不是全产品表结构版本：`0→1` 建迁移审计表 `migration_log`；`1→2` 给该表加 `note` 列并建索引
  （第二个步骤刻意用 `ALTER`，用来验证"提交后、写进度前崩溃"的续做分支）。
- **不 UPDATE/DELETE 历史事件**；`events` 表的 append-only 触发器保持原样。

### 5.2 进度与续做

新进度原子写到 `<data_root>/events.db.migration.json`，固定 `format="tianshu-migration"`、`format_version=2`。字段及严格规则以 [数据准入契约](data-admission-contract.md) §2.2/§3.1 和 `data_state.MigrationStateV2` 为准：绑定 canonical `db_path`、`from_version`/`target_version`、状态/步骤/时间、完整 SQL `plan_sha256`、原备份路径及 manifest 字节摘要、业务与材料的版本化摘要。数字不强制转换，坏类型、重复字段、未知字段与不可达步骤拒绝。

标准根的旧 `migration.json` 只接受格式 1；新 sidecar 只接受格式 2。两份都在就都验证，格式放错位置或内容冲突均拒绝。有限 legacy completed 只给普通连接兼容，不授予迁移或续做权，也不自动转换为格式 2。

每步落盘两次：**步骤开始前**（写 `current_step`）与**完成后**（追加
`applied_steps`）。崩溃点与恢复行为：

| 崩溃点 | 库版本 | 进度文件 | 续做行为 |
|---|---|---|---|
| `migration-before-step-N` | N-1 | `in_progress, current_step=N` | 重跑第 N 步 |
| `migration-after-step-N` | N | `in_progress, current_step=N` | 识别第 N 步已提交 → 标记完成，继续 N+1 |
| 最后一步提交后、写完成前 | 2 | `in_progress, current_step=2` | 以库版本为权威，补成 `completed` |

续做时重新验证实际文件、原备份路径、manifest 字节、真实备份起点、业务与双向材料清单，并仅接受已登记步骤的提交前/后窗口。备份移走、损坏或绑定变化就拒绝；不按 DB 数字补造步骤。完成后用原备份重复核原绑定，或用不同路径的新 v2 备份核当前一致点，两路均不重写 DB/进度。细节与证据见 [T2 记录](DATA-ADMISSION-T2-2026-11-13.md)。

### 5.3 前置条件（不满足一律不动库）

1. 标准根内 `events.db` 是单硬链接普通文件，用户 symlink/junction 或根外材料拒绝；取得锁后复查布局。
2. 对实际 DB 持独占维护准入，覆盖到全部进度收尾。正常 EventLog 的共享连接会挡迁移；迁移持锁时普通打开在创建 payload 目录前拒绝。daemon/host 的 IPC/PID 探活只作补充；判定不了也拒绝。
3. 备份先通过完整验证，再证明与活库升级前的完整机制、业务及材料一致；仅同根、同事件数或 verify 通过都不充分。第一份严格绑定进度写成后才执行 DDL。

拒绝保持历史事件、库逻辑内容、进度及材料不变；SQLite 打开活 WAL 的自身锁/SHM 行为不构成物理文件零写承诺。合作式维护锁不抵抗 raw SQLite 或旧版程序。

### 5.4 故障注入

每步前/后调用 `crashpoint.failpoint("migration-before-step-N" /
"migration-after-step-N")`（仅 `TIANSHU_FAILPOINT` 命中时 `os._exit(77)`，生产路径
永不触发）。测试用**真子进程**在四个注入点崩溃，重启后续做，断言：可完成、
最终 `user_version=2`、每步只应用一次、历史事件摘要不变、append-only 触发器仍在。

### 5.5 实际 EventLog 准入与显式迁移

T1 已在所有正常 EventLog 的实际连接前缀接入共享准入与只读检查，先检查真实 DB 及其关联进度，再设置 WAL、执行事件 DDL、创建 payload 目录。自定义 DB 检查自身 `<数据库名>.migration.json`，不借旁边标准库作证。

- 真正新空库与合法无进度 v0 保持兼容，不盲标为 v2，也不自动迁移。标准库名按路径比较规则查找历史材料及旧进度；即使主库为空或缺失，发现历史证据也拒绝重新初始化。
- v1 与任何 `in_progress` 拒绝普通打开。v2 必须有合法的 completed 进度；无进度时，只有结构与步骤审计通过且有匹配当前根及主库实体的 `ReleasedV1` 才可打开（§4.2）。
- 未来/畸形已登记版本、缺整个事件版本旁表、坏 JSON 或关联记录冲突拒绝。标准库的有限 legacy completed 兼容仅限普通打开；别名必须与标准库为同一实体，同名比较不能代替实体核验。新版进度的精确路径绑定不变，旧进度也不能转成新续做权。
- `migrate.assert_writable()` 保留为旧专用严格 helper，未原样塞入普通启动；它会拒绝正常 v0。T3 的 `data migrate` 仅接 T2 迁移库，不复制版本、摘要或路径算法。

0→2 仍只证明迁移机制，不代表完整产品格式或自动升级回退矩阵已交付。受管导入、持久隔离与显式解除见 §4；各版本与平台的实际证据见知识库和对应 PR。

---

## 6. 保留期与引用保护（§8.5）

声明在 `backup.RETENTION_POLICY`（备份清单里也带一份，随备份走）：

| 类别 | 保留期声明 | 额度声明 | 清理责任 |
|---|---|---|---|
| `event_log` | 永久（唯一事实源） | 无 | 用户显式操作；本模块不提供删除路径 |
| `snapshot_manifests` | 永久（未撤销/未完成 Run 需要） | 无 | 用户显式操作 |
| `snapshot_blobs` | 被引用即永久保护 | 2 GiB | `data gc --approve` 显式离线回收孤儿，`cleanup_plan()` 只生成候选 |
| `undo_journals` | 未完成必须保留；完成后可归档 | 64 MiB | 人工清理 |
| `diagnostic_trace` | 默认 30 天 | 50 MiB | 人工清理，不参与备份 |
| `indexes` | 可重建；同库派生表随 `events.db` 一并保存 | — | 不单独备份外置索引；重建代替清理 |

`collect_references(data_root)` 汇总事件外置 payload、执行流溢出、Receipt、快照、撤销、内容对象和未擦除记忆引用。受保护材料以登记路径和核验后的原生文件身份共同判定。大小写等不同拼写解析到同一文件时，该文件仍保留。扫描 `artifacts/blobs/` 与根 `blobs/` 下的内容对象，保留 `memory_blobs/`。`cleanup_plan(data_root)` 只生成候选。引用读取或材料完整性无法确认时，清理拒绝。

```bash
uv run --frozen tianshu data gc --data-root /absolute/data-root --json
```

预演成功退出 `3`。确认候选后，关闭使用该根的 Host、Daemon 和 CLI，再对同一根显式添加 `--approve`。批准命令持独占准入并重新核验候选。清理范围为孤儿 blob、符合条件的超期空终态快照、已终态作业，以及已结清且已知费用的过期预占。事件日志、摘要版本、引用材料和未知预占继续保留。默认保留期为 90 天，`--older-than-days N` 可指定正整数天数。

- `0` 表示本次执行完成。启动回收 SQLite 表行和 WAL 检查点需要至少 64 KiB 有效写入余量。POSIX 上，低余量时可先删除文件并同步父目录。若重测仍不足，本次不会做检查点，有待删表行时以 `db_rows_deferred_low_disk=true` 标记，可在腾出空间后重新执行。
- 预演和只读执行预检发现主库的 `-wal`、`-shm` 或 `-journal` 时返回 `2`，保留现场。请正常关闭使用数据根的进程并完成 SQLite 恢复，避免手动删除旁文件。
- Windows 的批准执行先只读预检。批次有任何文件候选时返回 `2`，所有文件、表行和 SQLite 检查点都保持原样。静态库只有表行候选时仍可清理或按余量延后。
- 执行中的 `gc_failed` 也退出 `2`，报告已确认成功的 `unlink`、逻辑字节数、已提交表行及可用的 checkpoint 状态。未确认的效果需要核对现场。逻辑字节数与实际可用空间可能不同。

`usage_report(data_root)` 和 `tianshu data retention-audit` 仅统计和审计。`data gc` 是人工触发的协作式离线清理。详细保护清单与失败边界见 [W15 变更记录](changelog/2026-11-18-w15-safe-gc.md)。

---

## 7. 原 W4 验收（2026-11-11 历史证据）

### 7.1 测试命令

```
PYTHONPATH=/Volumes/Date/ts-wt-backup/src /Volumes/Date/天枢Agent/.venv/bin/python \
  -m pytest -o addopts="" -q tests/test_backup.py tests/test_migration.py
# → 31 passed

PYTHONPATH=/Volumes/Date/ts-wt-backup/src /Volumes/Date/天枢Agent/.venv/bin/python \
  -m pytest -o addopts="" -q
# → 1347 passed, 6 skipped in 116.63s   （基线 1316 + 本轮新增 31）

/Volumes/Date/天枢Agent/.venv/bin/ruff check .
# → All checks passed!
```

### 7.2 覆盖到的场景

备份：一致点、清单自解释、密钥/日志不进备份、中断（子进程真崩溃 ×3 注入点：
`backup-after-db` / `backup-before-manifest` / `backup-after-manifest`）、
磁盘满（monkeypatch）、内容被改、文件缺失、清单坏、格式更新、seq 缺口、
引用丢失、脏目标拒绝、换路径拒绝、恢复失败保留原数据、恢复后核对、
引用保护、usage/额度、写者只读探针。

迁移：正常迁移 + 幂等、备份前置（缺失/损坏）、活写者与 unknown 写者拒绝、
更新版本/断链拒绝、写入门状态、状态文件损坏、四个中断点续做、续做要求原备份、
目标版本属于另一套计划。

### 7.3 负控（回退后必须红，实测输出）

见 §8。

---

## 8. 负控实测（本机 macOS arm64，2026-11-11）

### 8.1 负控 A：去掉迁移的备份前置

把 `_require_backup` 改成直接放行（不校验备份）后跑
`tests/test_migration.py::test_migrate_requires_verified_backup`：

```
>       with pytest.raises(MigrationRefused) as excinfo:
E       Failed: DID NOT RAISE MigrationRefused
FAILED tests/test_migration.py::test_migrate_requires_verified_backup - Faile...
```

（没有备份也能迁移 = 这个前置条件是可被绕过的；测试因此红。）

### 8.2 负控 B：迁移里「先删后建」（破坏历史事件）

在步骤 1→2 的事务里注入 `DELETE FROM events`（模拟"先删后建"式迁移），跑
`test_migrate_is_versioned_idempotent_and_never_touches_events`：

```
>                   conn.execute("DELETE FROM events")
E                   sqlite3.IntegrityError: events is append-only: DELETE forbidden
...
E           tianshu.migrate.MigrationFailed: 步骤 v1 → v2 失败并已回滚：
            IntegrityError: events is append-only: DELETE forbidden
FAILED tests/test_migration.py::test_migrate_is_versioned_idempotent_and_never_touches_events
```

（append-only 触发器当场拒绝；即使绕过触发器，测试后面「事件摘要不变」的断言也会红。
这一步失败时事务整体回滚，库停在上一步完成的版本。）

### 8.3 负控 C：去掉「中断目录不是有效备份」的保护

同时去掉两层保护——① `create_backup` 不写 `.partial` 标记；② `verify_backup`
不按标记/目录名拒绝——跑 `test_interrupted_backup_is_never_a_valid_backup`（三个注入点）：

```
>       assert verification.ok is False
E       AssertionError: assert True is False
E        +  where True = BackupVerification(ok=True, manifest=BackupManifest(...),
E            problems=[], remediation=[], files_checked=5, bytes_checked=321774,
E            db_user_version=0).ok
FAILED tests/test_backup.py::test_interrupted_backup_is_never_a_valid_backup[backup-after-manifest]
FAILED tests/test_backup.py::test_interrupted_backup_is_never_a_valid_backup[backup-after-db]
FAILED tests/test_backup.py::test_interrupted_backup_is_never_a_valid_backup[backup-before-manifest]
```

关键点：在 `backup-after-manifest` 崩溃点上，临时目录里"文件清单完整、`backup.json`
也在"，去掉两层保护后 `verify` 会返回 **ok=True** —— 一份从未发布的中断备份会被
当成有效备份。这就是这条保护必须存在的理由（两层保护：`.partial` 标记 + 目录名）。

### 8.4 负控 D：去掉引用保护

把 `collect_references` 里「被引用 blob 不算孤儿」的判断改成恒真（所有 blob 都进
候选），跑 `test_reference_protection_only_orphans_are_candidates`：

```
>       assert index.orphan_blobs == [f"artifacts/blobs/{'f' * 64}"]
E       AssertionError: assert ['artifacts/b...ffffffffffff'] == ['artifacts/b...ffffffffffff']
FAILED tests/test_backup.py::test_reference_protection_only_orphans_are_candidates
```

（被快照/事件/撤销引用的 blob 被当成可清理候选——正是绝不能发生的事。）

回退后全部恢复原文件并复跑：`tests/test_backup.py + tests/test_migration.py`
→ **31 passed**；全量见 §7.1；`ruff check .` → `All checks passed!`。

## 9. 仍未验证 / 剩余 unknown

1. **换路径恢复（重定位）未实现**：见 §4.3。需要相对化 `payload_ref` / `blob_ref`
   （`events.py` / `snapshot.py` 属别的工作流）或恢复时重写引用表（不能 UPDATE 历史
   事件，需要新机制）。
2. **新库不自动迁移，完整产品格式与升级回退矩阵未交付**：实际 EventLog 准入及显式迁移边界见 §5.5。
3. **低层在线 API 没有停止写者**：跨"事件库 + 材料"的严格一致需要独占维护；
   `create_backup()` 自身只有在线备份 API + 复制顺序。新 `data backup` CLI 已持独占
   准入；旧版本与直接 SQLite 访问不参与该协作锁，边界见 §2.2。
4. **迁移步骤只有两步**、且是本模块自有的 `migration_log`：W5 的事件 payload 契约
   版本旁表合入后需要追加新步骤（注册表就是为这个准备的），本轮不预造。
5. **Windows 历史 W4 证据**：只有 `windows-latest` CI，**没有实机**。当时 CI 暴露并修掉两处
   Windows 兼容缺陷：① `os.fsync` 对只读句柄报 `EBADF`——`_fsync_file` 改用可写
   句柄，仍失败只在"该边界已落盘"上降级（哈希/引用校验不受影响）；②
   `_pid_alive` 在 Windows 上不能用 `os.kill(pid, 0)`（那是 TerminateProcess
   语义），改用 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`，由
   `windows-latest` 上的 `test_pid_alive_sees_current_process` 给经验证据。
   `restore` 的原子改名、目录 fsync 与 APFS/跨机器语义仍无实机证据。**收尾态实测**：
   `main @ ec7d7ae` 的 `windows-latest` 上 `test_pid_alive_sees_current_process` 与全部
   backup/migrate 用例通过（该 job 总计 5 failed / 1509 passed / 29 skipped，剩余失败都是
   authority 子进程编码、命名管道、资源 URI 编码这些既有族，与备份无关）。这不证明 T3 候选的原生 Windows CI 已通过，T3 本轮只有 macOS 本机定向证据。
6. **备份大小/耗时**：本轮没有大库压测（测试库都是 KB 级）；在线备份 API 对超大库的
   暂停时间未测量。
7. **密钥材料**：`ipc.key` 明确不进备份；备份里的 `events.db` 是否含敏感 payload
   取决于用户任务内容，文档只声明"本地材料、勿上传"，没有字段级脱敏。

## 10. 复跑与证据说明

- 本文件所有数字都是在本机（macOS arm64，Python 3.12，worktree 内实测）跑出来的；
  没有 Windows 实机、没有跨机器恢复证据。
- 负控输出是"把修复回退后测试真的红"的原始输出；回退后已恢复原文件并复跑全绿。
