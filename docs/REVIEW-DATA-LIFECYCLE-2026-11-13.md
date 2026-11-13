# G3 数据生命周期首片独立复审（提交级通过，保留平台边界）

日期：2026-11-13。独立第二读者 `/root/verify_lifecycle`。作者自查结论未用作独立探针验收依据；按 PRD §5 / §6 / §8.5、CONTRIBUTING §7 与首片计划独立读代码和运行探针。产品代码只读，探针/证据仅写入 `/tmp/tianshu-g3-20261113/independent/`。本复审未提交、未推送、未读取真实用户数据、未调用模型。

## 提交级结论与证据继承

**结论：通过本地独立复审，限定于提交 `5093bf9a74ce082974096f4c38a2b529fff2da9b` 的数据生命周期首片范围。** F1–F5 均已修，无本地待修阻断。九个独立探针和四模块144项回归由第二读者亲跑；其精确命令、输出及对象绑定见下文。Windows/Linux原生运行、本次远端CI和实际恢复不在此结论内。

第二读者已直接读取该完整SHA的Git对象并核对：9个产品/测试文件加`.gitignore`共10项，与最终受审SHA256全相同；仓库归档的10脚本与5份JSON和独立侧冻结源逐字节全相同。详细清单另保留于 `candidate-commit-binding.json`，归档目录为 `docs/evidence/data-lifecycle-2026-11-13/`。权限对照探针固定读取基线完整SHA，不依赖动态HEAD。

另独立核对336文件的全量前freeze与候选提交：全量后差异仅为已阅读的 `docs/DATA-LIFECYCLE-2026-11-13.md`、`docs/backup-restore.md`、归档README三份Markdown，以及新添 `local-gates.json`；产品、测试、锁文件与探针无变化。`candidate-2-stable.json` 的无漂移记录与原始lock/ruff/pytest日志已读取；原日志SHA256与已归档 `local-gates.json` 全相同。原始pytest结尾为 `2273 passed, 7 skipped in 373.01s (0:06:13)`，三个门禁退出码均0。**这组全量是主代理执行、第二读者核原始日志与对象继承的证据，不记作第二读者亲跑全量，也不当作远端CI。** 336文件对账见 `candidate-freeze-inheritance.json`。

已阅读该提交README、知识库增量、备份指南、首片验收记录和复跑README：它们保留协作准入、低层API在线语义、无CLI apply、记忆未隔离、后续删除历史不可证明、GC未交付及未实测平台限制；未发现把演示迁移版本当作全产品写入门的陈述。按归档README原命令从仓库根亲跑 `probe_locks.py`，exit0；命令可用，日志摘要见 `archived-readme-smoke.json`。`git diff --check <基准SHA> <受审SHA>` 也exit0。

后续若只归档本报告或增补文档，可核新完整SHA的产品/测试/探针和已归档JSON均不变后继承本范围；任何实质代码、测试或探针变化须重新复核受影响范围。

## 对象绑定

基准 HEAD：`ccff93e79cfeb93e47e5a35012fd92fae366e941`。该 SHA 只标识基准，**不代表未提交候选已审版本**。首轮代码冻结逐文件 SHA256 如下；修复后须重新冻结、重跑受影响范围、补最终完整提交 SHA 及对象继承核对。

```json
{
  ".gitignore": "79b114316783b44e86b22b48ac921593eaa29da14154203d3d99498d2f06a4ee",
  "src/tianshu/maintenance.py": "39de368fd82d0ce83ff1524167e20d4055a83fa2606cb5b2bc2b62e15aa9cc5d",
  "src/tianshu/events.py": "c380e4f1ba33f32e1cf11d5200488b44cccccf42ea0caa1fad85721628a8c01e",
  "src/tianshu/backup.py": "9cc50d08dcf6d4473223aa85b45cebbbf978da7c1f51c7d68bddd323499cb91a",
  "src/tianshu/data_cli.py": "a4f7bbd44ef46f18a52e224f17d76c36564a73ac113d8fec811da2615f43f8f6",
  "src/tianshu/cli.py": "101272eff7891d74c76f09b5e534f2ff0e049983890b39a055a96520d92b7f60",
  "tests/test_maintenance.py": "63f1581156a3d9515ebcdd11334e32847791266511ea7ca485f42e0aa06e64a0",
  "tests/test_backup_materials.py": "e665ed54cf537d110fe34fe8450e91f9f5133476b1af461fef3b8cc226a47459",
  "tests/test_data_cli.py": "f8c2dec1afdb7b99656c02944afaf5ce2c953546de5d8326953ce9b767079dbd",
  "tests/test_backup.py": "7c96ac1b4fcd074ca08115c1901bd0eb43e23318db000c4f4e2ae5db17193d7a"
}
```

## 首轮结果（尚非交付批准）

- 初轮附加事件/契约/内容 API 回归：`156 passed in 0.81s`，exit 0，日志 `independent/events-content-regression.log`；`uv lock --check` exit 0（8 packages）。一次误将不存在的 `tests/test_cli.py` 作为命令参数，pytest exit 4 / no tests ran，已更正为实际存在的上述测试模块，该失败不算产品失败也不作通过证据。
- 指定四个测试模块：`93 passed in 4.77s`，exit 0；日志 `independent/targeted-pytest.log`。
- 独立跨进程锁探针通过八类判据：两个真实 EventLog 子进程共享共存；独占直到两者关闭才放行；shared/exclusive/EventLog 三类持有者硬退 77 释放；独占时 EventLog 在建数据根/DB 前被拒绝；坏库构造错误保留原字节并释放准入；跨线程 close 失败且原连接仍可写时保持准入；同一原路径替换数据根不替换锁身份。日志 `independent/probe-locks.log`。

## 首轮独立发现

### F1：旧 cleanup_plan 将仍有内容对象登记的 blob 列为可清理候选

未修（首轮）。真实 `ContentStore.record()` 登记对象并授予任务可见后，`collect_references()` 仍将该 `artifacts/blobs/<digest>` 列为 orphan，`cleanup_plan()` 列为人工清理候选。正文尚存在、未执行任何删除。原因是旧 collector 只走 payload/snapshot/undo，未用新 `_projection_references()` 的 content/memory 引用。`memory_blobs/` 不在旧 orphan 扫描范围，未亲见该目录正文被误列候选，但 `protected_blobs` 同样缺其引用，不能声称已完整保护。

复现命令（所有命令从仓库根执行）：

```sh
python3 /tmp/tianshu-g3-20261113/independent/run_clean.py uv run --frozen python /tmp/tianshu-g3-20261113/independent/probe_cleanup.py
```

关键输出：`registered` 对象存在，`visible=true`，`marked_orphan=true`，候选路径为 `artifacts/blobs/5d65149b1a3bf6a6c0b8753a65f680ff14e3c90f045879404cedcdbfeb2a3ae6`，`file_unchanged=true`。完整输出 `independent/probe-cleanup.log`。已首先通知主代理；作者正在修复，尚未复验。

### F2：WAL 模式保存库经 data verify 后新增 WAL/SHM，且第一次报告 ok

未修（首轮）。先用实际 create_backup 产生合法备份，再将保存的 events.db 设为 WAL journal_mode，关闭后确认只有 DB；刷新 manifest 中 DB 的真实哈希/长度，以隔离普通文件哈希失败。CLI `data verify` exit 0 / `ok=true`，但新增 `events.db-wal` 和 `events.db-shm`。`mode=ro` 对 WAL 数据库仍会创建旁文件，故不能单独兑现不写备份根的承诺。源库在线读取不能改成忽略 WAL 的 immutable；自包含保存快照需单独的严格只读读取路径。

### F3：含实际 Run 历史的备份缺 runs 投影，restore-plan 仍通过并报空运行态

未修（首轮）。实际 build_stack 创建 task、start_run，备份后对保存库 `DROP TABLE runs`，刷新 manifest 中 DB 的真实哈希/长度。`restore-plan` exit 3 / `ok=true` / `problems=[]` / `non_terminal_runs=[]`，仅 `projection_tables.runs='absent'`。这无法核对已有事件对应的运行态，不应当成可行预演。无需将确实只有 EventLog 的最小库一概判坏；应区分相关历史存在而必要投影缺失的情况。

F2/F3 同一复现命令：

```sh
python3 /tmp/tianshu-g3-20261113/independent/run_clean.py uv run --frozen python /tmp/tianshu-g3-20261113/independent/probe_readonly_edges.py
```

关键输出：`wal-verify exit=0 ok=true files_added=[events.db-shm,events.db-wal]`；`missing-runs-projection exit=3 ok=true non_terminal_runs=[]`。日志 `independent/probe-readonly-edges.log`。已通知主代理；作者正在修复，尚未复验。

### F4：锁根需要数据库目录的上一级可写，破坏原有自定义 DB 布局

未修（首轮）；由主代理提出风险后，独立真实权限探针确认。仅在临时目录构造 `restricted-parent`（0555）及其中仍可写的 DB 所在目录。从基准 HEAD 的完整 `events.py` 动态加载旧 `EventLog`，在权限限制下仍可 open/append/close；原生 SQLite 也可 CREATE/commit/close；候选 `EventLog` 却因尝试创建上一级 `.tianshu-maintenance` 而报 `MaintenanceError`，其原因是 EACCES。不存在数据目录本身不可写的干扰因素。

```sh
python3 /tmp/tianshu-g3-20261113/independent/run_clean.py uv run --frozen python /tmp/tianshu-g3-20261113/independent/probe_lock_permissions.py
```

关键输出：`baseline_EventLog="open+append+close ok"`；`raw_sqlite="create+commit+close ok"`；`candidate_EventLog="MaintenanceError"`。完整日志 `independent/probe-lock-permissions.log`。临时目录权限在 finally 中复原。建议所有参与者使用同一稳定每用户外置锁根，继续以 canonical DB 身份哈希；不可依赖访问失败时动态 fallback，否则可能使不同进程获得不同锁身份。产品代码未修，已告知主代理。

### F5：macOS 标准 /tmp 别名导致合法大 payload 被误判根外引用

复审第二轮未修。主代理首先用真实探针发现，独立第二读者另写探针确认：`TemporaryDirectory(dir='/tmp')` 里的 EventLog 用原 `/tmp/.../data/blobs` 存 330KB payload；备份将 source root 规范化为 `/private/tmp/.../data`，候选 `ee50817d7e652568818125e8de3f7b00754e16d1b7552fbc1a4000e22cb03221` 的纯词法引用映射拒绝该合法材料。原红证据 `independent/probe-source-alias-initial.log`，探针 `probe_source_alias_initial.py`。

修复须在创建时验证原 root 与 canonical root 同一身份并保存别名映射；独立验证只能按已保存映射词法寻址备份材料，不能为了兼容而重开活源解析。CLI 在提前 resolve 后可能丢原 root provenance，也须覆盖；未给定的根外别名与材料符号链接仍要拒绝，旧 canonical 格式 v1 备份仍需可读。作者正在修复，待最终冻结复验。

## 最终候选独立复验

以下结论绑定下列逐文件 SHA256；运行前后重新计算，未出现漂移。最终已核对这些字节与提交 `5093bf9a74ce082974096f4c38a2b529fff2da9b` 的 Git 对象一致；基准 HEAD 不代替该受审提交。

```json
{
  "src/tianshu/maintenance.py": "4ae3414ee3ccea7644c34a6e743aa90a650b4672b2cf67cc8862ed17bf8b9c3a",
  "src/tianshu/events.py": "c380e4f1ba33f32e1cf11d5200488b44cccccf42ea0caa1fad85721628a8c01e",
  "src/tianshu/backup.py": "79f9f64812d4456dcbfe725b96b7edffbbde304981e8c1501b9cbc151f2682e0",
  "src/tianshu/data_cli.py": "881714d85a10d46426d1a956fb5cc910f50c87b651b625b87aeae75d17c15239",
  "src/tianshu/cli.py": "101272eff7891d74c76f09b5e534f2ff0e049983890b39a055a96520d92b7f60",
  "tests/test_maintenance.py": "04b810726846b033f0df6e2d5e9ac7eb0c3c55d5c34dc4fd72609b509c0c38f9",
  "tests/test_backup_materials.py": "0ea3c8ea529b0f95b8e9d778b3bda23d31bd5cefbf347ebf66264197052de101",
  "tests/test_backup.py": "7c96ac1b4fcd074ca08115c1901bd0eb43e23318db000c4f4e2ae5db17193d7a",
  "tests/test_data_cli.py": "67a37e2a33bb6f3ac73ee5bdd23b44415651adff0aa306739e172f9bb8d4a4ee",
  ".gitignore": "8cc75edc2ad9978fd697a49e594c350f7556357c8e168dfd41934182612aed49"
}
```

### 五项发现的最终状态

- **F1 已修**。`probe_cleanup_fixed.py` 在最终候选上重跑：登记且任务可见的 content 与未擦除 memory 都在 protected；故意破坏两类 digest 都抛 `ReferenceUnknown`，不提供清理候选。
- **F2 已修**。`probe_readonly_edges_fixed.py` 要求 WAL header 保存库 verify 前后无新文件、原文件 hash/mtime 不变；`probe_wal_sidecars.py` 要求即便已列入 manifest 的 WAL/SHM 也拒绝，且根内容不变；`probe_live_wal.py` 用实际子进程保留已提交但未 checkpoint 的 WAL，确认主文件0条、含 WAL读取1条、在线备份1条，不因 immutable 丢活源事务。
- **F3 已修**。`probe_readonly_edges_fixed.py` 的真实 Run 历史缺 runs 投影时，restore-plan exit2、ok=false、complete=false、有明确问题、目标未创建。当前实现从严要求 approvals/runs/leases 全部存在；EventLog-only 历史备份仍能独立 verify，但 restore-plan 要求先由匹配版本重建验证投影，此限制不得写成已有自动恢复能力。
- **F4 已修**。`probe_lock_permissions_fixed.py` 固定从 `ccff93e79cfeb93e47e5a35012fd92fae366e941:src/tianshu/events.py` 动态加载完整旧实现；受限上一级0555时旧实现、原生 SQLite、新 EventLog 均成功 open/append/close。`probe_lock_identity_env.py` 对真实存在而不同的 HOME/TMPDIR/TIANSHU_DATA_ROOT 目录启动子进程，同一 DB 的锁身份相同，独占期间所有变体均 BUSY，DB根未创建。`probe_locks.py` 的共享/独占/close失败/硬退/根替换八类断言通过。
- **F5 已修**。`probe_source_alias_fixed.py` 使用330001字节外部 tool.intent payload和真实 SnapshotStore preimage，覆盖raw输入、canonical输入、真实CLI、macOS真实 `/tmp`→`/private/tmp`。删除源后将原alias改指无关目录，并 patch Path.resolve 禁止 verifier/read_saved 对原raw/canonical source解析，校验与payload读取仍通过；CLI restore-plan仍能报告pending intent而不创建目标。移除alias、畸形alias、材料symlink、根外同名blobs全部拒绝；缺alias字段的旧canonical格式v1仍可读。

### 最终运行证据

所有命令从仓库根运行，经 `run_clean.py` 清理项目及provider环境。十个归档脚本（含wrapper）均先通过 `uv run --frozen ruff format --check --config pyproject.toml <十脚本>` 与 `uv run --frozen ruff check --config pyproject.toml <十脚本>`，exit0；未使用ignore/noqa跳过探针问题。随后按最终脚本字节亲跑九个独立探针，全部exit0。精确argv、stdout/stderr、脚本SHA256见 `final-independent-probe-evidence.json`；样式证据见 `final-probe-style-evidence.json`；绑定清单见 `final-bundle-manifest.json`。

```sh
python3 /tmp/tianshu-g3-20261113/independent/run_clean.py uv run --frozen pytest -o addopts= -q -rs tests/test_maintenance.py tests/test_backup_materials.py tests/test_data_cli.py tests/test_backup.py
```

结果：**144 passed in 6.40s，exit0**。精确argv及完整输出见 `final-targeted-pytest-evidence.json`。此数目仅为四个明确模块，未冒充全量或CI。先前维护+事件/事件契约/内容API附加回归为 `192 passed in1.29s`，只作为当时已记录维护对象的附加证据；本段最终四模块结果是在F5最终候选上新跑。

材料/CLI综合探针还独立断言：完整payload/memory/content随真实CLI备份；删源后只读verify和restore-plan不改变备份hash/mtime；移除payload/memory/content正文并同时从manifest.files及references隐藏，仍由保存DB反推引用而拒绝；未知Run状态、future tool.intent版本、缺event_versions均exit2；三个backup failpoint真实硬退77后partial不可验证，维护锁释放，retry成功。

最终十脚本清单：`run_clean.py`、`probe_locks.py`、`probe_cleanup_fixed.py`、`probe_readonly_edges_fixed.py`、`probe_materials_cli.py`、`probe_live_wal.py`、`probe_wal_sidecars.py`、`probe_lock_permissions_fixed.py`、`probe_lock_identity_env.py`、`probe_source_alias_fixed.py`。每个最终hash在 `final-bundle-manifest.json`；不归档临时数据库或`.log`。

## 边界与平台

- 维护锁是协作式准入。旧版程序、直接 sqlite3/原生文件访问不参与，不能宣称进程沙箱或安全边界。
- 旧低层 `create_backup` 保持在线语义；只有新 CLI backup 承诺正常 EventLog 使用者关闭后的独占维护。旧 Python `restore_backup()` 并未新增独占维护或记忆隔离，不属于此首片 CLI 恢复安全交付；CLI 不暴露 apply。
- `cleanup_plan` 不执行删除，也不持有独占锁；不能把某时刻的候选清单直接当作在线 GC 安全证据。
- Windows `LockFileEx` / `UnlockFileEx` 静态对照 Microsoft Learn：共享 flags=1、独占 flags=3，reserved=0，低位长度1/高位0，OVERLAPPED Offset/OffsetHigh/hEvent=0，解锁使用同一区间，未发现参数位置/值错误。官方文档：`https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex` 与 `https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-unlockfileex`。这是静态核对，**不是 Windows 运行实测**；Windows、Linux、ARM64 桌面/安装、真实掉电均未由本复审验证。

复审实测环境：`macOS-26.6.2-arm64-arm-64bit` / arm64 / Python 3.12.13 / SQLite 3.50.4。

## 环境清理与证据纪律

`independent/run_clean.py` 在每个测试子命令环境移除所有 `TIANSHU_*` 以及以 `BASE_URL` / `API_KEY` 结尾的变量，不打印值、不修改全局环境。故障注入进程仅在该清理后注入明确指定的 `TIANSHU_FAILPOINT`。锁子进程通过 stdin/stdout 屏障排序，无 sleep 竞争猜测。原始探针有开发修正（最早 cleanup 探针误用 EventLog 上下文管理器，得到 TypeError，修正探针后才获得 F1 证据），未修改产品代码以迎合探针。

提交 `5093bf9a74ce082974096f4c38a2b529fff2da9b` 的五项已发现阻断均经第二读者独立复验为“已修”，在本报告明确范围内通过独立复审，无待修阻断。此结论不等于远端四项 required CI 已通过，也不扩大为整个 G3、实际恢复/隔离、GC 或未实测平台支持。

核对过程说明：第一次全量freeze对账辅助命令把允许的三份Markdown文件名猜错，导致辅助assert失败；实际输出始终只有上述三份Markdown差异及门禁JSON。第二读者阅读实际文件后按精确名单重新核对通过。该辅助命令错误未改产品、未改冻结探针，未冒充产品失败或通过。
