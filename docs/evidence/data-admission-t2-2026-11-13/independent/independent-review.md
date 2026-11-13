# Task2 独立验收

日期：2026-11-13。第二读者：`/root/review_admission_contract`。结论：**PASS_TASK2_CONTENT_ONLY**；本片内容未决0，部分修/未修0。仅对应下列冻结候选，不签发T3、正式提交或CI结论。

## 精确候选与范围

- 基线 `585e3474b7f9f03bb118308470fac1f500c3a02e`；完整585副本加精确25项overlay，438项其它跟踪文件无漂移，最终463文件全部稳定。两产品、两测试、21文档/证据；T1九项与恢复fixture逐hash不变。
- 交接manifest SHA256 `dddec59d87775811550493c535efee2fdc30faff199c41bb6bdbf9c80598c68a`；候选完整绑定见 `candidate-binding.json`，SHA256 `fd215d2a6347b017f7b4120fb53f6caf752d238156bc132a8965ab1d89f42042`。
- 契约SHA256 `c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`；计划SHA256 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`。只按§1.4、§2、Task2及冻结12项矩阵；作者自查结论未作验收证据。

- `src/tianshu/data_digest.py`：`e22ad1d0c8488df5d36984f7dbf3f8cd76afb482649e73c2e7fb31fbf340930b`
- `src/tianshu/migrate.py`：`686e59b8ff5a5360a02c0a3d8ae54190e126133c1014464ab34e693400875cf8`
- `tests/test_data_binding.py`：`1c56e2d66a9bc2dcea54e3a26f988c8ad9a2c5e9c1b563341dcb7160220d5e83`
- `tests/test_migration.py`：`02bb86f3608c1841d74516b63d801f0b8e9b17cc9777ada7caba300d304fe2eb`

## 实跑证据

- Python3.12.13 / pytest9.2.0，macOS arm64。独立81项通过（0 skip/error），另2项固定时钟/双sidecar只读no-op通过；原55是81子集。针对回归298项通过（binding76、migration14、backup21、materials60、state52、admission75），未跑full/CI。
- 真实EventLog/build_stack与6次memory_control.remember，离线HashEmbeddingProvider；覆盖FTS/vector/游标/summary/账本、未知typed表、非checkpoint已提交WAL、唯一持锁live conn。6个库级真实os._exit(77)起点及21种from0/from1进度位置；不手造completed替代迁移。
- 六次进度发布失败、两次DDL后故障均不报completed，回滚/释放后可重获锁和SQLite独占并续做；检查事件全文、append-only触发器及全部逻辑/材料/关联进度不变性。

## 12项隔离变异

- 每项是463文件完整独立副本，仅一个目标源码文件变化；每个副本的两个真实迁移正控均通过（共24）。原件及精确diff见 `mutation-source-bindings.json`、`mutation-results.json`、`diffs/M*.patch`。26个目标行为/不变性断言失败，0 setup/import/error/skip；11项具名kill。
- M01 `omit-extra-live-material`：命中真实行为/不变性断言；目标 3 项。
- M02 `events-only-business-digest`：命中真实行为/不变性断言；目标 6 项。
- M03 `omit-manifest-byte-binding`：命中真实行为/不变性断言；目标 2 项。
- M04 `omit-plan-binding`：命中真实行为/不变性断言；目标 3 项。
- M05 `ignore-real-backup-version`：仍被完整机制/起始版本独立守卫拒绝，**不记kill**；目标 3 项。
- M06 `ignore-origin-mechanism`：命中真实行为/不变性断言；目标 1 项。
- M07 `ignore-inherited-audit`：命中真实行为/不变性断言；目标 2 项。
- M08 `omit-fts-shadow-content`：命中真实行为/不变性断言；目标 1 项。
- M09 `legacy-number-union`：命中真实行为/不变性断言；目标 2 项。
- M10 `same-path-A-fallback-to-fresh`：命中真实行为/不变性断言；目标 1 项。
- M11 `audit-step-only-noop-B`：命中真实行为/不变性断言；目标 3 项。
- M12 `noop-progress-refresh`：命中真实行为/不变性断言；目标 2 项。

M05删掉三处显式backup real-version比较后：首次/B仍被完整mechanism_digest（包含实际version）挡住，resume由_reachable_mechanism要求保存起点version==from挡住。真实版本差异已拒绝；不声称这些被删单行各自必要，也不为凑12 kill拆掉其它保护。M06红在首次不匹配本应DDL前拒绝却已推进机制/进度，虽然后续guard仍拒绝。M09在隔离共享classifier内恢复数字union补齐；候选共享T1无变动。

M11使用合法等效显式ASC索引定义及applied_at/note变体，verify/inspect先成功再检验；不是用被shape直接拒绝的DESC冒充机制摘要证据。M12附加固定时钟且两份关联completed：变异前后字节完全相同，仍被writer spy以same_bytes=true的写尝试抓住（2真红）。保存备份多文件原先verify守卫不计M01；M01只用三目录额外活材料。

## 代码面与分层结论

- **已修 · 共享严格类型/旧兼容边界**：migrate重导出同一MigrationState与MigrationStateCorrupt；无第二份coerce/parse；T1九项及恢复fixture逐hash继承；legacy-only/v2无modern状态拒绝且不写。 代码：`migrate.py:29-47,265-270,450-460`。
- **已修 · 单真实连接与布局**：from0/from1未checkpoint提交WAL正控；live inspect/digest/每步事务共用唯一真实conn并持独占；conn关闭；hardlink/symlink及锁后布局复核拒绝；空主库先复用T1历史旁证检查。 代码：`migrate.py:248-252,303-337,450-554,557-572`。
- **已修 · 完整逻辑/机制/材料绑定**：真实6次控制面记忆栈含FTS/vector/summary/游标/账本，未知typed扩展；schema/table/columns/rowid/sequence/重复/generated/WITHOUT ROWID/IEEE754编码；不可读拒绝；FTS真实optimize；三方双向材料；manifest/plan；完整机制起点与继承audit。 代码：`data_digest.py:35-172,182-224; migrate.py:290-300,340-371,404-447`。
- **已修 · 严格续做与只读A/B**：6个from0/from1库级os._exit(77)起点与21种合法形状/实际位置组合；same-path A替换不降级；B业务真实新增且移走A成功；合法ASC定义与audit变体拒绝；no-op字节及writer尝试均0。 代码：`migrate.py:450-554`。
- **已修 · 失败原子性及资源释放**：六次进度发布失败、两次DDL后抛错：不completed，可重获维护准入/SQLite独占并成功续做；DDL/version/audit回滚；历史事件与append-only触发器保留。 代码：`migrate.py:374-401,557-572`。

- 意图轴PASS：完整基线、全持久内容/typed、双向材料、manifest/plan/起始机制/继承审计、严格resume及A/B只读均由本次探针/变异支持。
- 规范轴PASS：纯标准库、共享严格parser，无T1/CLI/契约/依赖变更；旧migration负例保留并增加真实format2现场断言。两产品无TODO/stub/未实现分支；既有symlink平台条件skip本机未发生。
- **部分修：无。未修：无。本片内容未决：0。** M05是重复守卫证据边界，不是候选缺陷。

## 复现、原件与边界

- 主命令：`python3 /tmp/t2-admission-independent/run_review.py baseline`、`regression`、`mutants`；既有输出/副本保留，脚本对同名产物fail closed，重放须在复制的证据目录或改用新运行名。完整执行参数、退出码和JUnit/原始stdout/stderr见 `logs/*-run.json` 与同名文件。
- 原件索引 `evidence-index.json` SHA256 `56885f13e63914ca10ee50ac4c3b8bcd020d0eca657b8986a4c858818b6d5132`；合成输入/最终现场索引 `synthetic-artifact-index.json`，5008项。所有候选/变异源文件均已亲核稳定，文件摘要/探针/diff/日志与路径在JSON中。
- 首轮runs父目录遗漏只造成55项pytest setup ERROR，未到产品调用；原日志保留且不计红。随后55通过，再扩至81；未覆盖原始失败。
- 本报告未写作者树、未commit/push、未full/CI、未执行T3。库级硬退不等于CLI/真实跨进程验收。正式提交绑定、新原生CI和T3由父会话另行授权/验收；合作锁、同uid raw writer及同内容无generation身份限制沿用批准契约。
