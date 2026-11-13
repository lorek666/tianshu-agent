# PR #134 Windows 测试编码问题 · 独立复核

日期：2026-11-13。第二读者 `/root/verify_lifecycle`。本轮只读仓库，证据仅写 `/tmp/tianshu-g3-20261113/independent/`。未以作者“已通过”结论作为独立证据。

## 结论与版本

**本地候选复核通过，完整修复提交对象绑定待完成；原生 Windows 修复验证仍待新 CI。** 此结论只覆盖测试读取编码的一行修正。既有产品复审范围仍以 `docs/REVIEW-DATA-LIFECYCLE-2026-11-13.md` 为准，不扩大旧结论。

父提交为 `f509a53881c372d8d7f4b1e2718130edaff4ed06`，分支 `codex/g3-data-lifecycle`。检查时唯一未提交差异是 `tests/test_backup_materials.py:111` 将 `.read_text()` 改为 `.read_text(encoding="utf-8")`，正文相等断言、材料集合断言和后续verify断言全部保留。未修改产品逻辑、契约、CI配置或跳过条件。

五个受审产品源文件（maintenance/events/backup/data_cli/cli）、其他三个测试文件与`.gitignore`的SHA256和前轮冻结值完全一致；唯一改变的受测文件是上述一行测试。其新SHA256为 `a9af99927d18d3035a719f4d325ed22c3d06e1bfd93943681ba0788d33e7f6ad`。逐项hash和精确diff见 `windows-encoding-scope.json`。检查前确认待审契约文档已保存在stash `45e9a7aef0945ffb9577d43a303f83c775babbd0`；未读取、恢复或修改它。

## 原始 CI 核对与根因

直接读取run `43375968422`、Windows job `131722421320` 原始日志：实际checkout为PR head `f509a53881c372d8d7f4b1e2718130edaff4ed06` 与基线合成的merge `a6deecd98b0ce40a0f397bd25ae03e4770eb7cf4`。该次runner报告Microsoft Windows Server 2025、image `windows-2025-vs2026`、Python3.12.16，不将该次环境泛化成Windows永久版本或用户设备支持。

唯一failure在测试第111行默认 `read_text()`，调用stdlib `encodings/cp1252.py` 后抛出 `UnicodeDecodeError: 'charmap' codec can't decode byte 0x8f in position 2`；全量结尾为 **1 failed / 2244 passed / 35 skipped，1168.94s**，进程exit1。原始日志SHA256为 `50cc3668fe49eff7a61bc1c5f395f5e5e4d3c7fcdb9a3da1fb7c2e9990514d63`，关键行号和摘录已记入scope JSON。其他job的状态不在本轮独立日志核对范围内。

产品记忆正文在 `memory.py` 以UTF-8编码后按bytes持久化；测试却依赖宿主默认编码解码同一正文。此次修正使测试按已有持久化格式读取，未更改产品存储语义，也没有把失败摘除或弱化断言。

## 第二读者亲跑的红绿证据

只在子命令环境移除 `TIANSHU_*` 和以`BASE_URL`/`API_KEY`结尾的变量，再设置 `LC_ALL=C LANG=C PYTHONUTF8=0`，未打印变量值或改全局环境。实测macOS26.6.2 arm64、项目Python3.12.13、SQLite3.50.4；metadata确认`sys.flags.utf8_mode=0`、`locale.getpreferredencoding(False)=US-ASCII`。

从完整父提交以`git show`取原测试和`tests/conftest.py`，逐字节复制到同一个系统临时tests目录。正式旧测试运行的`--setup-show`确认`_pin_authority_root`、`_pin_local_backend`、`_pin_tool_registry`三个原autouse夹具全部生效，保证红绿比较具有同样的隔离语义。

- 原测试在上述locale中真实变红：exit1，**1 failed，0.34s**；第111行默认读取抛出ASCII `UnicodeDecodeError`。这是非UTF-8默认解码的独立复现，不冒充原生Windows/cp1252运行。
- 当前仓库原测试在相同locale中：exit0，**1 passed，0.10s**。
- 相同locale下四模块 `test_maintenance.py`、`test_backup_materials.py`、`test_data_cli.py`、`test_backup.py`：exit0，**144 passed，6.23s**。
- 目标`ruff check tests/test_backup_materials.py`和`git diff --check`均exit0。

正式命令argv、stdout/stderr、原测试及conftestSHA256均在 `windows-encoding-evidence.json`。红测试复制品与临时数据位于系统临时目录，运行后清理。最初一次外置旧测试未同拷conftest，作为preliminary结果单列保存，没有用它替代上述正式有原夹具的红绿证据。

## 待完成项

修复提交生成后，第二读者须核完整SHA里的测试对象正是已跑新hash，五个产品源文件以及原有测试/探针/证据不漂移，再记录提交级继承。主代理的全量门禁由其自行执行、另行记录，本报告不将作者运行写成独立亲跑全量。新Windows CI通过前，不声明原生Windows修复已验证或本PR四项required全绿。
