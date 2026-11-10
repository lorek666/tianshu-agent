# T1 掉电 / 崩溃一致性证据（powercut-evidence）

- 分支：`feat/powercut-consistency`（worktree `/Volumes/Date/ts-wt-power`）
- 基线：`main @ 540c543`，全量 `1080 passed / 3 skipped`，`ruff clean`
- 规格：[`SPEC-RESIDUALS-M0M1.md` §T1](../ts-wt-lead/docs/SPEC-RESIDUALS-M0M1.md)（本工作流只做 T1）
- 收工全量：见 §8（本次实测数字）

---

## 0. 先给结论（哪些宣称已经成立、哪些仍不成立）

| 宣称 | 状态 | 证据 |
|---|---|---|
| `atomic_write_bytes` 的四个持久化边界上崩溃，恢复/撤销行为正确 | **已验证**（进程级注入，确定性） | §2，`tests/test_crash_consistency.py` 21 条 |
| 快照清单 / blob / `events.db`-WAL 被撕裂后，恢复/撤销不崩、不猜 | **已验证**（清单/blob/WAL 尾部） | §3 |
| `fsync + rename + 目录 fsync` 之后的字节，在真实卷被强制摘下后仍可恢复 | **已在 macOS 实跑验证**（opt-in，64MB HFS+ 镜像） | §4 |
| 「没有 fsync 的字节一定丢」 | **不能证明** | `detach -force` 仍会刷脏页；20MB 未 fsync 实测存活（§4.3）。硬件/固件语义要块设备断电实验 |
| 真实断电（页缓存丢失、固件撒谎） | **仍未验证** | 不在本轮范围；`os._exit` 只证明进程中断 |
| Windows 实机 | **仍未验证**（保持原结论） | 无 Windows 实机；CI 只有 `windows-latest` |

本轮另外**发现并修掉**两个真 bug（都不是"补证据"能绕过的，见 §5）：

1. **删除类动作的恢复去读一个已被删除的文件**（issue #54 的真实缺陷）→ `FileNotFoundError` 裸异常穿出 `recover_run`；
2. **撕裂的 WAL 被静默洗成空库**（#54 之外新发现）→ 历史全丢、恢复给出"干净"的假结论。

---

## 1. 两个故障注入层与它们的边界

`tests/test_crash_consistency.py` 用的是**进程级注入**：子进程在指定边界 `os._exit(77)`，不走任何清理逻辑。它证明的是"进程死在这一点时，恢复逻辑看到什么、判什么"，**不证明**页缓存里未 fsync 的字节真的丢了——后者由 §4 的真实摘卷实验补。

`TIANSHU_FAILPOINT` 是唯一的注入开关（沿用 M1 的 `crashpoint.failpoint`，`os._exit` 硬退）。新增的 `TIANSHU_FAILPOINT_PATH` 只做**路径限定**：一次 Run 里 `atomic_write_bytes` 会被调用很多次（快照清单 / blob / 目标文件），只按名字注入的话第一个命中的多半是清单，测不到"目标文件写到哪一步断电"。两个变量都只存在于测试进程，生产路径永不设置、永不触发。

四个持久化边界（`src/tianshu/fsutil.py` 的 `atomic_write_bytes` 内）：

| 故障点 | 位置 | 进程死时磁盘上的事实 |
|---|---|---|
| `after-tmp-write` | tmp 写完、`fsync` 之前 | 目标还是旧文件/不存在；tmp 里是半持久内容 |
| `after-tmp-fsync` | tmp `fsync` 之后、`rename` 之前 | tmp 内容已持久，但名字还没发布 |
| `after-rename` | `os.replace` 之后、目录 `fsync` 之前 | 新名字已可见，但断电后 rename 可能回退 |
| `after-dir-fsync` | 目录 `fsync` 之后 | 整条原子替换已按顺序持久化 |

`tests/test_recovery.py` 用的是**Run 级注入**（`after_intent` / `after_write` / `after_result` / `after_receipt`），与本文件互补：它证明"真实 Run 装配路径 × 审批 × 事件"在中断后的一致性；本文件证明"文件系统持久化边界"这一层。

---

## 2. 注入矩阵：4 边界 × {新建, 覆盖写, 删除} × 三条断言

命令：

```
PYTHONPATH=/Volumes/Date/ts-wt-power/src \
  /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q \
  tests/test_crash_consistency.py
```

结果：**21 passed**（本文件）。

每格的断言（矩阵 12 格，`test_boundary_crash_create_overwrite` / `test_boundary_crash_delete`）：

1. **不变量**：目标文件只能是写前字节或写后字节，逐字节；诱饵文件没被动；
   证据清单要么没有本条、要么完整可解析。
   - `after-tmp-write` / `after-tmp-fsync`：目标 = 写前状态（新建=不存在，覆盖=原文）；
   - `after-rename` / `after-dir-fsync`：目标 = 写后内容。
2. **恢复判据**：`recover_run` 不崩、不猜；`applied` 的 `path/sha256/after_state/
   before_state` 与磁盘逐字节一致，恰好一张凭据且 `verify_receipts == []`；
   没执行的那次是 `none_applied`、不产凭据。再跑一次恢复不补事件、不重复发凭据。
3. **撤销可还原**：副作用确实发生时 `undo` 按 preimage 重建回原字节（新建 → 删掉、
   覆盖 → 写回原文）；没发生时撤销拒绝（冲突），一个字节都不改、不留下动作记录。

删除类的特殊说明（`test_boundary_crash_delete`）：`delete_file` 的四个 `fsutil`
边界都落在"写前证据落盘"期间（`record_preimage` → `unlink`），所以进程死在任何
边界时 `unlink` 都还没发生——本格证明"证据的持久化边界不会让删除变成半截状态"。
证据已持久的两格（rename / dir-fsync）再用第二个子进程模拟真实可达状态
"证据已持久 → 进程执行了删除 → 断电"：

- `test_delete_completed_after_durable_evidence_recovers_and_undoes`：
  恢复必须给出 `status=succeeded` / `effect_state=applied` /
  **`output.after_state=absent`** / `deleted=True` / `before_state=sha(原文)`，
  目标确实不存在（不是 0 字节文件），凭据可独立核对，再恢复一次幂等；
  撤销按 preimage 重建回**原字节**。

### 2.1 `test_recovery.py` 的 delete 注入矩阵（issue #54）

命令同上，文件 `tests/test_recovery.py`，结果：**15 passed**（四个 Run 级故障点 ×
{create, overwrite, **delete**} + 幂等/unknown/100 次注入）。

- `after_intent`：删除没执行 → 目标仍是写前内容，恢复判 `none_applied`；
- `after_write` / `after_result` / `after_receipt`：删除已执行 → 目标必须不存在，
  恢复判 `applied`、`after_state=absent`、`before_state=sha(原文)`、恰好一条凭据；
- 100 次注入矩阵配平到 12 个 (写类型, 故障点) 组合 × 8 + delete 每故障点补 1 = 100；
- 子进程里是 `approvals.decide(...)` + `resume_run(...)` 模拟"人在 CLI 上敲批准"，
  审批事件 `requested → decided → consumed` 必须齐全，**没有绕过授权**。

---

## 3. 撕裂 / 丢尾模拟

| 破坏 | 恢复 | 撤销 | 钉住它的用例 |
|---|---|---|---|
| 快照清单截断 | `SnapshotCorrupt` → **unknown + 阻断**（不裸崩、不当空清单） | 先撞 `WorkspaceBlocked`；人工解除阻断后撞 `SnapshotCorrupt`，零修改 | `test_torn_manifest_is_unknown_and_blocks` |
| 写前 blob 截断 | 恢复仍可判 `applied`（它只用 before/after 哈希判磁盘，不读 preimage） | 拒绝（`写前 blob 哈希不符`），目标一个字节都不改 | `test_torn_blob_refuses_undo_without_modification[truncate]` |
| 写前 blob 位翻转 | 同上 | 同上 | `...[flip]` |
| `events.db`-WAL **最后一个提交帧**撕裂 | 正向对照：SQLite 丢弃断尾、保留已提交前缀（只剩 `run.start`）；恢复不崩、不凭空报 `applied`；撤销不依赖事件日志，仍按 manifest 还原 | — | `test_torn_wal_tail_keeps_recovery_legal` |
| `events.db`-WAL 撕到 **schema 首帧** | 修复前：**打开成功 + `read_run()` 返回 `[]`**（历史静默蒸发，见 §5.2）；修复后：`EventLogCorrupt`（域错误），CLI 退出非 0 | — | `test_torn_wal_schema_loss_is_domain_error_not_empty_log` / `test_cli_recover_on_torn_wal_reports_domain_error` |

**边界**：WAL 截断模拟的是"存储层违背 fsync 承诺"。最后一个提交帧被撕掉时，最后
一条事务会整体丢失（这里恰好是写前 `tool.intent`）：恢复看不到那次待核调用，但
文件仍落在写前/写后之一，撤销靠 manifest 仍能还原。这不是恢复的误判，是事实源
丢了一条记录；`synchronous=FULL` 下不应发生，本测试证明的是**恢复不会因此崩或
伪造结论**。

---

## 4. macOS 强制摘卷实验（opt-in，CI 默认 skip）

### 4.1 复跑命令

```
TIANSHU_POWERCUT=1 PYTHONPATH=/Volumes/Date/ts-wt-power/src \
  /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q -s \
  tests/test_powercut_image.py
```

流程：`hdiutil create -size 64m -fs HFS+ -layout NONE` → `attach` →
子进程用真实 `AgentLoop` 路径在挂载卷上写入/在持久化边界崩溃 → `hdiutil detach -force`
→ 重新 `attach` → 父进程跑 `recover_run` / `verify_receipts` / `undo`，断言四条不变量。
不设 `TIANSHU_POWERCUT=1` 时整文件 skip（CI 默认）。

### 4.2 本地实跑输出（2026-11-11，macOS arm64）

```
TIANSHU_POWERCUT=1 PYTHONPATH=/Volumes/Date/ts-wt-power/src \
  /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q -s \
  tests/test_powercut_image.py
[powercut] 写入完成：target=/private/var/folders/.../mnt/complete/ws/report.md 字节=13
[powercut] 数据根：/private/var/folders/.../mnt/complete/data
[powercut] hdiutil detach -force 完成（未给文件系统 flush 机会）
[powercut] 重新 attach 完成
[powercut] 四不变量全部成立
.[powercut] after-tmp-write：子进程已在持久化边界硬退
[powercut] after-tmp-write：强制摘卷 + 重挂完成
[powercut] after-tmp-write：四不变量成立（applied=False）
.[powercut] after-dir-fsync：子进程已在持久化边界硬退
[powercut] after-dir-fsync：强制摘卷 + 重挂完成
[powercut] after-dir-fsync：四不变量成立（applied=True）
.
3 passed in 4.45s
```

三条用例分别覆盖：完整写入（`after-dir-fsync` 之后正常退出）→ 强制摘卷 → 重挂后
目标字节、清单、事件、凭据全部可恢复；`after-tmp-write` 边界崩溃（未 fsync）→
摘卷 → 重挂 → 恢复判 `none_applied`、撤销拒绝、零修改；`after-dir-fsync` 边界
崩溃（已持久）→ 摘卷 → 重挂 → 恢复判 `applied`、磁盘只能是写后字节、撤销可还原。


### 4.3 这个实验不能证明什么（实测边界）

- `detach -force` **仍会刷脏页**：手工探针在同一镜像上写 20MB 完全不 fsync 的数据、
  `os._exit` 后立刻 `detach -force`，重挂后 20,971,520 字节**完整存活**。所以本实验
  无法制造"未 fsync 的字节丢失"，也**不能**用它的绿来宣称"未 fsync 一定丢"或
  "未 fsync 一定活"。
- 它证明的是：**fsync + rename + 目录 fsync 之后的产物，在真实 HFS+ 卷被强制摘下、
  重新挂上后仍可恢复**，且恢复/撤销对重挂后的状态给出的裁决合法（不变量四条成立）。
- HFS+ 之外的 APFS / ext4 / NTFS 真实断电行为未测；真实块设备的页缓存/固件语义
  仍未验证（§0 表）。

---

## 5. 本次发现的两个 bug（都补了回归 + 负控）

### 5.1 issue #54 暴露的新 bug：删除恢复去读已被删除的文件

**现象**：`recovery.py` 的 `applied` 分支无条件执行
`current_backend().size(obs_ws.resolve(change.path))`。`delete_file` 的 applied
状态是"文件已删除"，`size()` 对不存在的路径抛 `FileNotFoundError`，裸异常穿出
`recover_run`——恢复不但没给出结论，还崩了。

**复现**（修复前）：

```
File "/Volumes/Date/ts-wt-power/src/tianshu/recovery.py", line 313, in _recover_run
    "bytes": current_backend().size(obs_ws.resolve(change.path)),
FileNotFoundError: [Errno 2] No such file or directory: '.../ws/a.txt'
```

**修复**（按 lead 授权，跨属 T2 的 3 行；T2 会 rebase）：`expected_after == ABSENT`
时走删除分支，output 与活体 `_delete_file` 同形状
（`path` / `deleted=True` / `after_state=absent` / `before_state`），不再读文件；
非删除分支不变。`receipt.build_receipt` 对删除取 `after = sha256 or after_state`，
`after_state=absent` 能正常出凭据，不用动 `receipt.py`。

**判据 / 测试**：`test_recovery.py` 的 delete 矩阵 + `test_crash_consistency.py`
的 `test_delete_completed_after_durable_evidence_recovers_and_undoes` 钉住：
恢复后目标确实不存在、`applied.after_state=absent`、凭据可独立核对、恢复幂等、
撤销按 preimage 重建回原字节。

**这个 bug 属于审计里没抓到的那一类**：恢复判据此前只检查"哈希一致"，
忘了"applied 的目标可能本来就不存在"。§M2-verification §8 的"真实断电未验证"
没覆盖删除路径的恢复判决。

### 5.2 #54 之外新发现：撕裂的 WAL 被静默洗成空库（fail-open）

**现象**：`events.db` 的 WAL 被撕裂到 SQLite 无法回放（例如 schema 首帧不完整）
时，旧 `EventLog.__init__` 不报错：它照常 `executescript(_SCHEMA)`，把库当成新库
建表 → 打开成功、`events` 表 0 行、原 WAL 被丢弃。**整段审计历史静默蒸发**，
`recover_run` 看到空日志 → 报告"没有任何未决调用"，而磁盘上的副作用可能已经发生。

**复现（修复前）**：

```
崩溃后: wal=70072B, main=4096B
wal.write_bytes(raw[:32+24+100])          # 撕掉 schema 首帧
EventLog(db)                               # 不抛
read_run("run_x") -> []                    # 历史全没
# 打开后 main=20480B, wal 被删除：SQLite 建了全新的空 schema
```

CLI 的表现（最坏的一种回答）：

```
$ tianshu recover --run-id run_t1 --workspace <ws> --db <db>
run_id : run_t1
已确认执行 : （无）
确认未执行 : （无）
无法核实   : （无）
$ echo $?
0
```

**修复**（lead 授权改 `events.py`，本轮只有本工作流在改它）：connect 之前记下
"主库文件存在且 `size > 0`"；connect 之后、`executescript(_SCHEMA)` **之前**读
`sqlite_master`（这一步会触发 WAL 回放，是"库里到底有没有历史"的事实来源）：
老库却读不出 `events` 表 → `EventLogCorrupt`（域错误），message 带库路径、现象
和处置建议（先备份整个数据目录、用 `recover` 核对、**不要删除** `events.db`）。
`PRAGMA journal_mode=WAL` 移到检查之后：WAL 不可回放时它可能先重建/丢弃日志文件，
等于在检查证据之前把证据毁了。

护栏（都有测试钉住）：

- **0 字节文件当新库**：首次启动建了文件还没写 schema 就崩，没有历史可丢，
  判 corrupt 是误报 → `test_fresh_and_reopened_event_log_are_not_misjudged`；
- **不误杀第二连接**：daemon 的 control/driver 两条连接同库，schema 已在，不得误报
  → 同一条用例里开第二条连接 + 关掉重开；
- **CLI 边界不变**：`except Exception` 已存在 → 退出码 2、stderr 含「错误：」、
  不抛栈、不打印干净结论（`test_cli_recover_on_torn_wal_reports_domain_error`）；
- **只截断最后一帧是安全的**：正向对照用例证明我们不是"见 WAL 就报错"
  （`test_torn_wal_tail_keeps_recovery_legal`）。
- **真实旧库一定带 `events` 表**：从第一次提交起 `_SCHEMA` 就把它建出来。
  `tests/test_leases.py` / `tests/test_m2_acceptance.py` 的迁移用例以前用裸
  `sqlite3` 造"只有 leases/approvals 一张表"的库，那是在模拟一个从未存在过的
  历史版本；现在改成"先用 `EventLog` 建完整 schema，再把那张表降级成旧形状"，
  迁移断言不变（真实旧库照旧能打开、列照旧补上）。

**已知残余**：SQLite 在回放阶段就会丢弃"不可回放"的 WAL 文件，所以打开检查本身
会消耗掉它。防线是"立刻判 corrupt 并拒绝继续"，不是"把 WAL 留着"。

---

## 6. 负控（回退修复后测试必须红）

### 6.1 回退 `fsutil.atomic_write_bytes` 的 fsync/rename（改成直写）

把 `atomic_write_bytes` 换成 `target.write_bytes(data)`（先 `mkdir`），跑
`tests/test_crash_consistency.py`：

```
$ git checkout -- ... # 先备份，再把 atomic_write_bytes 换成直写
$ pytest -q -x tests/test_crash_consistency.py
>       assert proc.returncode == 77, (
E       AssertionError: create/after-tmp-write 应当在故障点退出，实际 0；stdout='' stderr=''
E       assert 0 == 77
FAILED tests/test_crash_consistency.py::test_boundary_crash_create_overwrite[create-after-tmp-write]
1 failed in 0.09s
（不加 -x 跑整文件：20 failed, 1 passed）
```


### 6.2 去掉 `events.py` 的 schema 检查

```
$ # 把 events.py 的 `pre_existing and not has_schema` 分支改成 `False and ...`
$ pytest -q tests/test_crash_consistency.py -k "torn_wal"
FAILED tests/test_crash_consistency.py::test_torn_wal_tail_keeps_recovery_legal
FAILED tests/test_crash_consistency.py::test_torn_wal_schema_loss_is_domain_error_not_empty_log
FAILED tests/test_crash_consistency.py::test_cli_recover_on_torn_wal_reports_domain_error
3 failed, 18 deselected in 0.18s
```

断尾这条也红，是因为那一步 `SELECT sqlite_master` 不只是**检查**：它先触发 WAL
回放，把"可恢复的已提交前缀"落到主库；跳过它直接 `executescript(_SCHEMA)`，DDL
写入会把 WAL 连同还没回放的 `run.start` 一起丢掉。也就是说这条检查同时承担
"先读、再写"的顺序保证。


### 6.3 回退 `recovery.py` 的删除分支（回到无条件 `size()`）

```
$ # 把 recovery.py 的 applied 分支退回"无条件 size()"
$ pytest -q tests/test_crash_consistency.py -k "delete" tests/test_recovery.py -k "delete"
FAILED tests/test_crash_consistency.py::test_boundary_crash_delete[after-rename]
FAILED tests/test_crash_consistency.py::test_boundary_crash_delete[after-dir-fsync]
FAILED tests/test_crash_consistency.py::test_delete_completed_after_durable_evidence_recovers_and_undoes[after-rename]
FAILED tests/test_crash_consistency.py::test_delete_completed_after_durable_evidence_recovers_and_undoes[after-dir-fsync]
FAILED tests/test_recovery.py::test_recover_after_each_failpoint[after_write-delete]
7 failed, 3 passed, 26 deselected in 1.15s
```
（失败原因是同一个：`FileNotFoundError: [Errno 2] ... report.md` 从 `recover_run` 里
穿出来——恢复在"文件已被删除"这件事上崩了，而不是给出结论。）


三处回退后都已恢复原文件并复跑：`36 passed, 3 skipped`（crash + recovery + powercut
的 opt-in skip），`ruff check .` → `All checks passed!`。

---

## 7. 能证明 / 不能证明（一句话版）

- **能证明**：在 `fsutil.atomic_write_bytes` 的四个持久化边界上崩溃，恢复与撤销
  只会看到"写前/写后"两种合法状态，读不出来时是 `unknown + 阻断`（清单）或域错误
  （WAL 不可读），不是裸崩、更不是"没事"；`fsync` 之后的字节在真实 HFS+ 卷被强制
  摘下后仍可恢复。
- **不能证明**：未 `fsync` 的字节一定丢；真实块设备断电/固件语义；HFS+ 之外的
  文件系统；Windows 实机。

---

## 8. 收工验收记录

```
$ PYTHONPATH=/Volumes/Date/ts-wt-power/src \
    /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q
1105 passed, 6 skipped in 103.86s (0:01:43)
```
（基线 `main@540c543`：1080 passed / 3 skipped；+21 条 crash-consistency、+4 条
delete 注入、+3 条 opt-in 摘卷用例默认 skip → 1105 / 6。）

- `ruff check .` → `All checks passed!`
- 新增/修改：
  - `src/tianshu/crashpoint.py`：`TIANSHU_FAILPOINT_PATH` 路径限定（不改变旧行为）
  - `src/tianshu/fsutil.py`：四个持久化边界故障点
  - `src/tianshu/recovery.py`：删除类动作 applied 分支（跨属 T2，lead 授权，3 行）
  - `src/tianshu/events.py`：撕裂 WAL 的 fail-open 修复（lead 授权，本轮唯一改动者）
  - `tests/test_crash_consistency.py`（新）：21 条
  - `tests/test_powercut_image.py`（新）：3 条（默认 skip，`TIANSHU_POWERCUT=1` 时跑）
  - `tests/test_recovery.py`：delete 注入矩阵（+4，共 15 条）
  - `tests/test_leases.py` / `tests/test_m2_acceptance.py`：把"半张表"的伪旧库
    改成"完整 schema + 降级那张表"的真实旧库形状（见 §5.2 护栏最后一条）

## 9. 仍未验证 / 遗留

1. **真实断电**：`os._exit` 只证明进程中断；`detach -force` 会刷脏页（§4.3）。
   页缓存/固件语义需要块设备断电实验，不在本轮范围。
2. **Windows 实机**：保持"未验证"（见 KNOWLEDGE §7 / issue #50）。
3. **WAL 丢尾丢掉了写前 intent**：恢复会看不到那次待核调用（§3）。`synchronous=FULL`
   下不应发生；本测试只证明恢复不崩、不伪造结论，**不能**宣称"掉电后一定不会漏"。
4. **真实块设备层**：ext4 / APFS / NTFS、外置盘、SSD 掉电行为未测。
5. **T2/T3 的后续**：`recovery.py` 的 3 行删除分支需要 T2 rebase；未完成撤销的
   阻断接线属 T3/T2 合并后由 lead 接。
