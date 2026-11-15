# 恢复 B · T1 平台基础候选证据

**状态：`CANDIDATE_PENDING_FINAL_GATES_AND_INDEPENDENT_REVIEW`。**

本记录只覆盖 T1 平台基础，不把内部实现写成可执行的完整恢复产品。基线为
`origin/main@2530a1575695baf75391a917a15a61dd2ffaa363`，分支
`codex/restore-t1-platform`。最终提交 SHA/tree 必须在候选冻结后回填。

## 实现范围

- `platform_identity.py` 从持续打开的原生句柄读取实体身份。POSIX 使用同一
  句柄的 `device+inode`；Windows 使用 `GetFileInformationByHandleEx(FileIdInfo)`
  的卷序列号和 128-bit file ID。最终符号链接、junction/reparse point、全零
  file ID 与错误实体类型闭锁。
- `maintenance.py` 提供固定用户外部锁目录下的 gate、canonical namespace 和
  实体 shared/exclusive 非阻塞锁。组合 access 逆序释放；任一步 close 失败时
  停在原处，后续锁继续持有。
- `restore_registry.py` 在真实用户 Home 下的
  `.tianshu/restore-registry-v1/registry.db` 建立严格、只追加 registry。
  完整读取调用 T0 `validate_event_chain()`；head 只是同事务 CAS 缓存。
- `restore_access.py` 固定 `gate → namespace → entity → registry` 顺序。
  `EventLog` 从 anchor 创建到 SQLite 真正 close 持续持有 namespace/entity；
  SQLite connect 后核对 `PRAGMA database_list`、路径和重开实体身份。
- `migrate` 与离线 `data backup` 使用共享组合实现取得 namespace/entity 独占，
  硬链接别名和仍在运行的 `EventLog` 不能与其并行。

## 必红证据

| 判据 | 本地测试 |
|---|---|
| POSIX/Windows 真实身份、类型与链接拒绝 | `tests/test_restore_platform_access.py` |
| 硬链接别名归同一实体锁 | `tests/test_restore_platform_access.py`、`tests/test_restore_eventlog_access.py` |
| 真双进程 shared/shared 共存、exclusive 冲突、`os._exit(77)` 释放 | `tests/test_restore_platform_access.py` |
| EventLog 连接期持续持锁、双连接并行、硬退与 close 失败保锁 | `tests/test_restore_eventlog_access.py` |
| 路径/祖先替换与 `database_list.main` 实体核对 | `tests/test_restore_eventlog_access.py` |
| registry 私有初始化、schema/tamper/实体替换负控 | `tests/test_restore_registry.py` |
| 两真实进程同 head 只有一个 CAS 成功 | `tests/test_restore_registry.py` |
| 两进程初始化竞争只产生一个权威 | `tests/test_restore_registry.py` |
| 写者持独占时观察立即 busy | `tests/test_restore_registry.py` |
| 四个初始化 failpoint 前后可解释现场 | `tests/test_restore_registry.py` |
| SQL schema 文件与 Python 常量逐字节绑定 | `tests/test_restore_registry.py` |
| EventLog 阻挡迁移，迁移保留既有 barrier/失败夹具 | `tests/test_data_migration_cli.py`、`tests/test_migration.py` |
| Windows `FileIdInfo`/硬链接/rename/锁/registry artifact | `scripts/restore_t1_windows_probe.py` |
| gate 外 absent 后出现 reservation 必须先重观并拒绝普通 anchor | `tests/test_restore_eventlog_access.py` |
| 迁移 SQLite close 失败必须继续持独占 access | `tests/test_data_migration_cli.py` |

## 门禁状态

最终冻结源码的 macOS arm64 完整本机门禁为 **3376 passed / 9 skipped
（589.68s）**，lock 与全仓 Ruff 均通过。T1 四个核心文件为 **166 collected /
165 passed / 1 skipped**；
T0、T1、迁移、数据绑定和离线备份相关回归为 **313 collected / 312 passed / 1
skipped**。UI 未改动，CI 的现有 UI required check 仍会按仓库规则执行。

Windows 原生 runner 除完整测试外，还运行
`scripts/restore_t1_windows_probe.py` 并上传独立 JSON artifact。该探针必须实际
通过 128-bit file ID、持句柄 rename、硬链接、目录句柄、共享/独占锁、
EventLog 连续锁与 registry 初始化；在对应 GitHub Actions run 完成后回填
run/job/artifact/SHA。没有这些原生结果时，Windows 行保持 `NOT_RUN`。

独立第二读者第一轮在未提交工作树上用真实子进程发现并复现了
`absent→reserved` 准入竞态和迁移 SQLite close 失败提前释放 access；两项修复
后第二轮结论为 `PASS（限 macOS 定向复审范围）`，EventLog/registry/migration
定向 **78 passed / 1 skipped**。最终提交 SHA/tree 的第二读者签发仍为 `PENDING`。

## 明确未完成

- 完整 B `apply/review/resume/abort` 与管理 CLI/API、P0–P5 产品链仍 `NOT_RUN`。
- Python 标准库不能取得 SQLite 内部 VFS 的 OS HANDLE。当前能证明的是“锁定实体
  句柄 + `database_list.main` 路径 + 重开实体相同”，不是直接读取 SQLite 内部
  HANDLE。
- Windows 使用 `MoveFileExW(MOVEFILE_WRITE_THROUGH)` 发布 registry 目录；物理
  断电级目录 durability 没有可移植证明，继续标为 `NOT_RUN`，不外推用户设备或
  任意文件系统。
- 当前协作锁只覆盖已接线的程序入口；未持锁 raw SQLite、旧程序及同 uid 直接改
  文件仍在声明外边界。三材料独立写入口的完整入口归属索引仍属于后续 B 实现。
