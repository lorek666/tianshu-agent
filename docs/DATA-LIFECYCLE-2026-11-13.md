# G3 数据生命周期首片 · 2026-11-13

本轮基于 `ccff93e79cfeb93e47e5a35012fd92fae366e941`，分支为 `codex/g3-data-lifecycle`。交付范围是完整离线备份、独立校验与只读恢复预演；本轮不改 C1–C5、事件版本或数据库结构版本。操作说明集中在 [备份指南](backup-restore.md)。

## 1. 范围与验收判据

- `data backup --data-root ROOT --dest BACKUP` 要求标准 `ROOT/events.db`。正常 `EventLog` 真实连接持共享准入，CLI 备份持独占准入；多个正常连接可共存，任一活跃连接会阻止维护，维护中也拒绝新写连接。拒绝不强停进程。
- 准入随真实连接存活，包括外部继续持有的连接与游标；错误线程关闭失败且连接仍可写时不提前放锁。构造失败须清理，硬退后由 OS 释放。锁位于数据根外，不删除锁文件。
- 备份收 `events.db`、`artifacts/**`、根 `blobs/**` 与 `memory_blobs/**`，排除工作区、IPC 密钥和诊断日志。发布前完整校验，缺材料不能生成成功备份。
- `verify` 从保存数据库和材料重新推导引用，校验事件 payload、快照、撤销日志、内容对象与未擦除记忆正文。删除 manifest 引用不能隐藏漏材料。源目录丢失或换成符号链接后不回源读取；备份内符号链接、根外引用与畸形材料拒绝。
- 创建时核对历史路径的同根别名并保存可选 `source_aliases`，兼容 macOS `/tmp` 与 `/private/tmp` 及已验证根别名。保存后的读取只做词法映射；缺必要别名时拒绝，不回源补映射，不将实际根外材料当成同根。
- `restore-plan` 只读，报告原路径/目标干净度、版本、未决 intent/审批/Run/租约。损坏状态、未来 payload 版本和未知封闭值不能降级为空状态。备份 hash/mtime 不变，目标不存在时仍不存在，不启动服务，不激活记忆。
- 预演必要的 `runs` / `approvals` / `leases` 投影缺失时明确拒绝、`reconciliation.complete=false`；仅事件日志的旧库不猜测状态为空。静态备份的专用只读连接不创建 WAL/SHM，活源仍通过 SQLite 在线备份包含已提交 WAL。
- 原有清理候选 API 纳入内容对象与未擦除记忆引用保护，坏引用拒绝候选；不新增删除操作，不扩展 GC 范围或承诺在线 GC 安全。
- 三个备份 failpoint 在真实子进程硬退 `77`；中断目录不发布，公共 verify 始终拒绝 partial，重新备份仍可成功。

## 2. 本机证据

环境为 macOS arm64、Python 3.12。测试只用临时目录，不读取真实用户数据、不调用真实模型。子命令环境隔离 `TIANSHU_*` 和 `ANTHROPIC_BASE_URL/API_KEY`、`OPENAI_BASE_URL/API_KEY`，不修改用户全局环境，也不记录变量值。

基线执行 `uv lock --check` 和 `uv run --frozen ruff check .` 均成功。未隔离的全量为 **1 failed / 2149 passed / 7 skipped，378.81s**；唯一失败是 `test_official_endpoint_is_priced`，受本机 `ANTHROPIC_BASE_URL` 环境配置影响。只在测试子命令隔离相应变量后，该测试 **1 passed，0.06s**；没有为此修改产品或测试。

独立复审整改后的集成定向检查：

```bash
uv run --frozen pytest -o addopts='' -q \
  tests/test_maintenance.py tests/test_backup_materials.py \
  tests/test_backup.py tests/test_migration.py tests/test_data_cli.py
# 157 passed in 6.83s
```

测试先在原实现上真实变红：缺命令、漏记忆/根 payload、清单隐藏引用、外部持有连接时过早放锁、人话输出遗漏和源根被符号链接替换等均有原红记录。原红与本机运行日志保存在工作区外的 `/tmp/tianshu-g3-20261113/`；九份最终独立探针、环境包装器和关键 JSON 已归档到 [复现材料](evidence/data-lifecycle-2026-11-13/README.md)，复跑不依赖原临时目录。

第二读者独立发现并亲复验五类问题：清理候选漏 content/memory 引用、静态 WAL 校验创建旁文件、缺投影误报空状态、锁根依赖上级目录权限、合法源根别名被拒。最终九探针均退出 `0`，独立四模块回归 **144 passed，6.40s**；十份脚本的格式与 lint 检查通过。这些是内容级独立复验，完整提交绑定另记，不替代全量及本次 CI。

最终本机候选（基准提交加冻结工作区）运行：

- `uv lock --check`：退出 `0`。
- `uv run --frozen ruff check .`：退出 `0`，包括新入库的探针。
- `uv run --frozen pytest`：**2273 passed / 7 skipped，373.01s**，退出 `0`。
- 检查前后 **336 个文件无漂移**。首次候选因独立复审发现问题主动中断，不计作全量通过；上述为整改后的完整重跑。

命令、退出码、被测产品/测试 SHA256、原日志摘要与日志哈希见 [local-gates.json](evidence/data-lifecycle-2026-11-13/local-gates.json)。这次全量后仅回填文档与 JSON 证据；提交级独立核对须确认产品、测试和探针对象继承，不能把基准 HEAD 当成已测候选。独立第二读者和本轮四项 required CI 仍须分别绑定完整提交 SHA；本机通过不代表 Windows 已验证或满足合并门禁。

[正式独立复审](REVIEW-DATA-LIFECYCLE-2026-11-13.md) 已将本地结论绑定到产品提交 **`5093bf9a74ce082974096f4c38a2b529fff2da9b`**：被审对象、十脚本与五份独立运行 JSON 均与冻结字节一致，并亲跑归档 README 的锁探针。后续报告归档提交仅增加文档/JSON，不改变受审产品、测试或脚本；继承范围须再次核对。本报告不预填云端通过结论，本轮 PR 当前 head 的 required 状态另看 Actions。

### 2.1 首次 CI 与 Windows 测试编码修正

PR #134 head `f509a53881c372d8d7f4b1e2718130edaff4ed06` 的
[run 43375968422](https://github.com/lorek666/tianshu-agent/actions/runs/43375968422)
已完成：UI **76 passed**、Ubuntu **2268 passed / 12 skipped / 3 warnings，363.99s**、
macOS **2273 passed / 7 skipped，569.17s**，三项成功；Windows 为
**1 failed / 2244 passed / 35 skipped，1168.94s**，失败不能算作通过。

Windows 唯一失败在本片新增的 `test_backup_covers_cli_payload_memory_and_content`：
第 111 行用默认 `Path.read_text()` 读取既有 UTF-8 记忆正文，该 runner 默认 cp1252，
解码抛 `UnicodeDecodeError`。实际被测合成提交为
`a6deecd98b0ce40a0f397bd25ae03e4770eb7cf4`，环境为 Windows Server 2025
（image `windows-2025-vs2026`）、Python 3.12.16；不泛化为用户设备验证。

修正仅为该测试显式指定 `encoding="utf-8"`，断言、产品、CI 和 skip 条件不变。
第二读者在 `LC_ALL=C LANG=C PYTHONUTF8=0` 中亲跑：父提交原测试与原 conftest
逐字节复制后 **1 failed**（ASCII 解码错误），当前测试 **1 passed**，同环境四模块
**144 passed，6.23s**。这是非 UTF-8 默认编码的独立红绿证据，原生 Windows 仍需新 CI。
完整命令、原始输出和对象摘要见
[独立编码复核](evidence/data-lifecycle-2026-11-13/windows-encoding-review.md)及其同目录 JSON。

该一行修正后的本机全量为 **2273 passed / 7 skipped，374.91s**，
`uv lock --check` 与 `uv run --frozen ruff check .` 均退出 0；
246 个非文档文件在运行前后摘要完全一致。运行输出、命令及冻结对象见
[编码修正本机门禁](evidence/data-lifecycle-2026-11-13/windows-encoding-local-gates.json)。
这些结果只绑定本次编码修正候选，后续同步 main 或实现准入时须重新验证受影响组合。

### 2.2 同步 main 的 I/O 与预算修复

本地从编码修正 `fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45` 合并
main `a8399062dd87675cc10b7c258ab4ce3679b53613`（PR #131/#132 已合入）。
产品与测试没有人工解冲，唯一知识库冲突保留双方历史。第二读者逐项核对 353 条路径
（含旧 UI 资产的删除），除 README/知识库外均精确继承对应父对象；本片十个产品/测试/忽略项
全部继承已审编码修正，未接管另一 debug 会话的业务修改。

该组合本机 `uv lock --check`、`uv run --frozen ruff check .` 均退出 0，
全量 **2416 passed / 8 skipped，425.09s**，253 个非文档对象在运行前后无漂移。
UI install/typecheck/**80 tests**/build 均退出 0，提交的 dist 重建后逐字节一致。
证据见 [组合本机门禁](evidence/data-lifecycle-2026-11-13/main132-integration-local-gates.json)和
[独立内容继承](evidence/data-lifecycle-2026-11-13/main132-integration-independent-review.md)。
编码修正的提交级报告也已归档为同目录 `windows-encoding-commit-review.md` / JSON。

以上仅为 a839906 组合；最终恢复修复 PR #133 仍由另一会话推进，后续同步须重新绑定。
本组合的完整 Git 提交 `16721ff14ad08456e8f6a2522c3300b2652b0918` 已由第二读者
[正式绑定](evidence/data-lifecycle-2026-11-13/main132-integration-commit-review.md)，核对父对象、
253 个冻结非文档文件与原始日志，未冒称独立复跑全量。原生 Windows/四项 required CI
仍另行确认，不从本机或历史绿色推断。旧报告的历史段落/新增行统计口径另有更正附录，
原件与实际内容继承均保持。

### 同步实际 main #133 后的新组合

恢复修复由原 debug 会话正常合入 main
`f2aef9da75d2e546af99baa523d196d1e099c196` 后，本分支同步该实际对象。
知识库两处冲突保留双方内容；本片维护/备份/CLI 与测试不重写上游恢复实现。
新组合 `uv lock --check`、`uv run --frozen ruff check .`、`uv run --frozen pytest`
均退出0：**2439 passed / 8 skipped，427.25s**，255个冻结非文档文件前后无漂移。
测试仅对子进程隔离项目/provider配置，不打印环境值。UI对象及3份dist完全未变，
精确继承a839上已记录的typecheck/80tests/build证据，没有另跑UI。
[原日志与冻结清单](evidence/data-lifecycle-2026-11-13/main133-integration-local-gates.json)
绑定本次作者执行；正式提交与该候选的新原生四项CI仍须分别核实。
第二读者的[内容与门禁复核](evidence/data-lifecycle-2026-11-13/main133-integration-independent-review.md)
已经归档；产品、测试和配置均按已审父对象继承，最终提交与当前 CI 分别绑定。

## 3. 交付边界与后续顺序

1. 锁是协作式准入，旧版或同 uid 直接 SQLite 可绕过；不承诺硬链接别名收敛。锁统一位于稳定的 OS 用户临时根，不要求数据根上级目录可写；Windows 未独立验证 owner SID / 跨用户 ACL。低层 `create_backup()` 保留在线语义，只有新 CLI 持独占准入。
2. 新 CLI **不提供恢复 apply**。原 `restore_backup()` 是 W4 库函数；它未证明备份后的 forget/erase/delete 历史完整，也没有恢复记忆隔离。旧备份漏材料时会如实拒绝，不把校验通过当成激活许可。
3. `migrate.SCHEMA_VERSION=2` 仍是两步迁移机制演示；正常产品库仍是 `user_version=0`。先以独立 `docs(contract):` PR 登记全产品版本、v0 准入和记忆恢复隔离，再实现迁移启动门与实际恢复，不能直接拒写所有现有 v0 库。
4. 换路径恢复、低磁盘保护、自动保留/GC 均未交付。现有清理候选 API 不等于可执行的全数据根 GC。
5. Windows 原生锁行为要等本次提交的 Windows runner；runner 结果不等于 Windows 10/11、ARM64 或用户设备安装体验。未运行 10 小时稳定性、真实模型评测、凭据/安装升级回退，不能将本切片登记成整个 G3 或后续批次完成。

本轮避开另一个 debug 会话负责的 I/O、预算、模型、恢复、Host/IPC、UI 和 CI 修改；不接管其 PR #131–#133，不用其历史绿色为本轮作证。
