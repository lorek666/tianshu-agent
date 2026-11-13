# 数据准入与迁移绑定契约 · 独立设计审阅

日期：2026-11-13（Asia/Shanghai）。审阅者：独立第二读者 `/root/review_admission_contract`。

**最终结论：通过；当前无未决设计阻断。该结论仅适用于这份待实现契约，不代表产品已修、迁移实现已验收或 G3 已完成。**

## 1. 版本、范围与方法

- 仓库：`/Users/ronfee/.codex/worktrees/windows-ci-fix/天枢Agent`；分支 `codex/g3-lifecycle-contract`。
- 源码基线：`f509a53881c372d8d7f4b1e2718130edaff4ed06`。最终检查时 `git diff --stat -- src tests` 为空；工作树改动只有作者的契约及关联文档。
- 初审契约 SHA-256：`8c35df012eca720086e523719818c6ecdd0bb73ba63903285800e4e0d630dc01`。
- 本次最终读回契约 SHA-256：`c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`。原文件未提交，因此以内容摘要绑定审阅对象，不能把源码 HEAD 当作该文档的提交版本。
- 最终读回后，作者为处理另一 CI 测试编码问题，将三份文档保全至 stash `45e9a7aef0945ffb9577d43a303f83c775babbd0` 并暂切工作状态。本次冻结副本与作者提供的 `/tmp/tianshu-g3-20261113/data-admission-contract-review-copy.md` 逐字节摘要相同；本报告不把其后其它提交冒充已审版本。
- 只读核对：仓库 AGENTS、`docs/KNOWLEDGE.md`、`CONTRIBUTING.md`、PRD §5.3.5/§6/§8.5，以及 `events.py`、`migrate.py`、`backup.py`、`data_cli.py`、`maintenance.py`、记忆/FTS 相关源码与必要测试。
- 未读取作者自查报告；没有修改仓库或提交，没有调用付费模型或用户真实数据。所有主动探针只创建 `/tmp/tianshu-g3-20261113/` 下的合成库；独立测试关闭 bytecode 和 pytest cache 写入。
- 未接管另一调试会话的 host/exec/ipc/loop/recovery/budget/net/model 工作。对它们没有给出实现验收结论。

以下发现按严重度排列；“契约已修”只表示最终文本消除了该设计缺口，绝不表示对应产品实现已经修好。

## 2. 已反馈并由最终文本修正的发现

### F1 · P1 · 单向清单验证不足以证明备份是当前完整材料点 —— 契约已修

初审定位：`docs/data-admission-contract.md:72-74,84-88`，尤其“清单材料仍逐项按备份的路径、大小与哈希核验”。该写法允许执行者只检查备份已经列出的材料，遗漏源目录新增而尚未登记到 DB 的材料。

源码证据：`src/tianshu/backup.py:614-639` 会收集 `artifacts/`、`blobs/`、`memory_blobs/` 的材料；`src/tianshu/backup.py:1092-1139` 检查的是备份自身的实际文件与其清单，不负责证明活源库存没有新增内容。

独立复现：先备份带有 `artifacts/stable-evidence.json` 的合成库，再只在源根新增 `artifacts/offline-review.json`。输出同时满足：

```text
backup_verify_ok=true
events_equal=true
all_backup_listed_materials_still_equal=true
inventory_equal=false
```

这里既有材料确实经过比较，不是空清单的真值。新证据未进入旧备份，数据库和旧清单内容却完全没有变化；只做单向比较不能推出完整保存点相等。

最小修正：锁内按同一材料范围与排除规则枚举源 inventory，要求源与备份的相对路径集合双向相等，并比较每个文件的大小、哈希。

最终读回：`docs/data-admission-contract.md:99-104` 明确双向集合相等、孤儿材料也使备份失配；`docs/data-admission-contract.md:147-149` 增加未登记到 DB 的新增材料负控。**契约已修。**

### F2 · P1 · canonical 路径锁不能把已有硬链接别名收敛成同一准入 —— 契约已修

初审定位：`docs/data-admission-contract.md:59-62,70-71`。初稿限制了标准根及“不明链接布局”，但没有明确硬链接必须拒绝，执行者容易把 `Path.resolve()` 当作同一实际文件的充分判据。

源码证据：`src/tianshu/maintenance.py:119-127` 用 canonical 路径文本生成锁键；`src/tianshu/maintenance.py:183-228` 再按这个键取得 OS 锁。`Path.resolve()` 不会合并两个硬链接名字。普通 `EventLog` 在 `src/tianshu/events.py:295-297` 使用这条准入路径。

独立复现：合成 DB 创建第二个硬链接。真实子进程持 alias 的共享准入并经管道屏障报告 ready；父进程仍成功取得标准 `events.db` 的独占准入。输出：

```text
same_inode=true
different_maintenance_keys=true
exclusive_acquired_while_real_child_holds_shared_alias=true
link_count=2
```

这不是要求引入 inode 世系身份或物理沙箱，而是证明不能在已有硬链接布局下把路径锁解释成独占维护。

最小修正：本片维护仅接受根内无符号链接的普通 DB，且硬链接计数必须可判定并等于 1；前置失败时不得先创建迁移进度或备份。普通自定义布局和同 uid 原生写入的保证范围不扩大。

最终读回：`docs/data-admission-contract.md:64-65` 给出上述限制；`docs/data-admission-contract.md:149` 约束拒绝时机。**契约已修。** 不宣称普通连接现在能够收敛所有硬链接别名。

### F3 · P2 · parser 所有权与 admission 的依赖禁令冲突 —— 契约已修

初审定位：`docs/data-admission-contract.md:102-108`：一方面禁止 admission 导入 backup/EventLog，另一方面让 migrate 保有进度解析。复用 migrate parser 会传递依赖 backup；在 admission 再实现一份 parser 则形成两套判定逻辑。

源码证据：`src/tianshu/migrate.py:49` 顶层导入 backup；`src/tianshu/migrate.py:135-197` 拥有现有进度类型及解析。**没有声称当前已复现 import cycle**：实际 `backup.py` 顶层没有导入 events；`src/tianshu/backup.py:1446` 只有恢复函数内部的延迟 migrate 导入。缺口是契约的模块边界约束，不能错误归因为已经存在的顶层环。

最小修正：把机制常量、严格进度解析与纯校验移入不依赖执行器的轻量 `data_state`；migrate 可兼容重导出，保留步骤、进度持久化、事务及报告。

最终读回：`docs/data-admission-contract.md:124-133` 给出单一轻量解析层及明确依赖方向。**契约已修。**

### F4 · P2 · FTS 保存点不能只由虚拟行或 projection_hash 代替 —— 契约已修

初审定位：`docs/data-admission-contract.md:84-88` 已要求类型化全库摘要和索引持久内容，方向正确；但没有具体说明 FTS shadow 表，存在按“可重建索引”将其跳过的实现歧义。

源码证据：`src/tianshu/memory.py:614-618` 创建 FTS5 虚拟表；`src/tianshu/memory.py:1073-1111` 的 `projection_hash()` 比较记忆及虚拟行，并且在 `:1080` 调用 `ensure_schema()`，既不能证明所有持久索引状态相同，也不能直接充当准入的纯只读全库算法。`src/tianshu/backup.py:104-108` 的保留声明还把索引称为可重建，因此必须区分“可重建”与“本次保存点比较可忽略”。

独立复现：FTS 插入十次并提交，再执行正常 `optimize`；虚拟行逐项相等，但 `memory_fts_config/content/data/docsize/idx` 这些持久 shadow 对象的内容整体不同。探针输出 `virtual_rows_equal_after_optimize=true`、`persistent_shadow_content_equal=false`。这是保存点等价范围的证明，不是把 optimize 称为数据损坏。

最小修正：明确 FTS 虚拟表和持久 shadow 表均属于摘要范围；不可读表或不支持的 SQL 类型应拒绝，不能跳过。SQLite 值必须保留 NULL/TEXT/INTEGER/REAL/BLOB 的区分，而非转字符串或仅比事件字段。

最终读回：`docs/data-admission-contract.md:91-96` 已明确类型化范围及 FTS/shadow；探针也实际保留了五种 SQL 存储类的区分。**契约已修。**

### F5 · P2 · 严格拒绝所有旧格式 1 会连已完成的合法机制库一起拒绝 —— 契约已澄清

此前旧进度条款只明确不能续做，正常连接对旧 completed 的具体处理不够明确。

源码证据：`src/tianshu/migrate.py:53-59` 仍写 format 1；`src/tianshu/migrate.py:559-561` 完成后写 completed；`src/tianshu/migrate.py:145-155` 的记录没有实际 DB/基线绑定。独立使用当前库函数迁移合成 v0 库，得到实际 v2、format 1、completed、applied_steps=[1,2]、current_step=null，以及实际两步审计 [1,2]。这是一种当前程序确实会产生的正常旧状态。

独立判断：允许有限只读 legacy completed 兼容是必要且可控的。它只承认可核对的当前 v2 机制状态，不能从缺失字段反推新基线、备份绑定或续做授权。canonical 路径加完整内容点作为本片身份足够，不必在本片建立 db_id/generation/世系控制表；完全相同内容的替换不得被误称为已证明原 inode。

最小修正：只关联实际标准 `ROOT/events.db` 的自身旧进度；严格核对 target/status/applied/current、类型以及真实表/列/索引和审计事实。旧 in_progress 即使最后一步已提交也拒绝；新旧关联记录矛盾拒绝，不通过删旧记录或扫描全机其它数据根补“权威”。

最终读回：`docs/data-admission-contract.md:66-69,110-120` 明确新进度位置、旧记录关联、有限 completed 兼容和 in_progress 拒绝。**契约已澄清。** 该兼容不会拒绝所有无进度的正常 v0 库。

## 3. 核对通过的边界与实现期必验项

1. **实际 DB 而非 data_root 代证。** 最终 `docs/data-admission-contract.md:26-31,60-69,143-144` 让真实连接和对应进度共用准入前缀，明确自定义文件、相邻诱饵和后台重开。不存在为了让新库“看起来当前”而盲标 v2 的许可。
2. **首次打开与拒绝不变性。** 最终 `docs/data-admission-contract.md:28-36,40-45` 明确不忽略活 WAL、不先设 journal/DDL/建材料目录，允许 SQLite 自身锁/SHM 维护但不声称物理介质零写；非空孤立 WAL/journal 不可被零字节主库规则抹掉。实现必须保留原材料，不用“新空历史”代替不可判定。
3. **缺旁表与缺单行版本不同。** 当前源码 `src/tianshu/events.py:520-544` 在同连接发现旁表缺失时抛域错误；但其 `:363-368` 在重开时会重建空表。本次合成探针证实：删整个 `event_versions` 后同连接读取抛 `PayloadVersionUnavailable`，close→重新 EventLog 却把原 version 1 事件读成 0。**这是尚未修复的当前产品行为，不是本次文档已经修好的实现。** 最终 `docs/data-admission-contract.md:139-142` 已明确要求将此“关闭重开”路径写成未来负控，同时保留缺单行记录按 v0 的正控。
4. **v0 与演示链分开。** 当前旧 `assert_writable` 确实拒绝正常 v0（`src/tianshu/migrate.py:598-623`；探针得到 `UnsupportedSchemaVersion`）。最终 `docs/data-admission-contract.md:10-18,43-56,132-133` 正确禁止把它原样放进所有普通连接；1/2 只解释迁移机制对象，不证明业务 schema 全冻结。
5. **受绑定续做必须新实现验证。** 最终 `docs/data-admission-contract.md:77-87,106-120,145-154` 要求独占、完整当前备份、严格进度、计划及 manifest/基线摘要、事务与四点硬退。现有 `migrate.py:400-416,534-539` 的少数字段备份描述和数字对齐逻辑不能被当成这些要求的已完成证据。
6. **恢复 hold 只保留后续约束。** 最终 `docs/data-admission-contract.md:156-171` 没有把投影状态 UPDATE 或可丢失标记当作持久整批隔离；具体权威字段、发布原子性、复核及 apply 仍须另起契约和实现。当前 `src/tianshu/data_cli.py:186-202` 仍明确 `apply_supported=false` 与 `completeness_unproven`。没有将恢复旧备份视为 unknown 重试授权，也没有证明后续删除历史完整。

## 4. 独立执行记录与证据

实际命令（工作目录为上述仓库；只用现有 Python 3.12 环境）：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /tmp/tianshu-g3-20261113/admission-contract-probes.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python /tmp/tianshu-g3-20261113/admission-legacy-probes.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider --basetemp=/tmp/tianshu-g3-20261113/admission-focused-test-temp -o addopts='' -q tests/test_event_schema.py::test_historical_row_without_version_record_reads_as_v0 tests/test_event_schema.py::test_missing_version_side_table_is_explicit_unknown tests/test_memory_store.py::test_schema_v1_database_is_migrated_in_place tests/test_memory_store.py::test_schema_v2_database_gains_the_layer_column_in_place tests/test_memory_store.py::test_newer_schema_version_fails_closed
```

两份探针退出均为 0；五个已有回归测试结果为 `5 passed in 0.08s`。这些测试验证现有 fixture 的相关语义，**不证明新的 data_admission/data_state/迁移入口已经实现**。本报告没有借用作者全量测试、首片旧绿色或未亲跑的平台 CI 作为新实现验收。

冻结证据：

- 本报告：`/tmp/tianshu-g3-20261113/admission-contract-independent-review.md`
- 主要探针脚本：`/tmp/tianshu-g3-20261113/admission-contract-probes.py`
- 主要探针原始 JSON：`/tmp/tianshu-g3-20261113/admission-contract-probes.json`
- legacy/旁表探针脚本：`/tmp/tianshu-g3-20261113/admission-legacy-probes.py`
- legacy/旁表原始 JSON：`/tmp/tianshu-g3-20261113/admission-legacy-probes.json`
- 独立测试输出：`/tmp/tianshu-g3-20261113/admission-focused-tests.txt`
- 审阅契约快照：`/tmp/tianshu-g3-20261113/admission-contract-reviewed.md`
- 元数据与证据 SHA-256 清单：`/tmp/tianshu-g3-20261113/admission-contract-review-evidence.json`

## 5. 最终判定

**通过（仅待实现契约）；未决设计阻断为 0。** 初审材料清单、硬链接维护前置、parser 依赖、FTS 范围和旧 completed 兼容均已在最终内容摘要绑定的文本中处理。此次允许进入契约独立提交/PR；产品实现、故障注入和原生平台验收应在后续实现分支/PR 完成，不得将本结论表述为“产品缺陷已修”或“G3 已完成”。
