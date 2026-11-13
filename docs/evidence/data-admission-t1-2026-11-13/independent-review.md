# T1 实际数据库准入 · 独立源码与探针复审

日期：2026-11-13。第二读者：`/root/review_admission_contract`。唯一作者：`/root/executor_data_cli`。

**结论：`PASS_T1_CONTENT_ONLY`。最终冻结 T1 范围内未决阻断 0。** 一项 P2 测试证据缺口已由作者修正，并由第二读者亲跑真实负控销账。此结论只覆盖下列源码/测试内容，不表示最终组合全量、远端 CI、T2/T3、迁移入口、恢复或整个 G3 通过。

## 输入对象与审阅边界

只读作者树：`/Users/ronfee/.codex/worktrees/g3-data-admission/天枢Agent`，分支 `codex/g3-data-admission`，base `30b973dfe21ce71f40ccfd7e84412de33d23877a`。源码尚未提交，故本次绑定 base 加最终九文件 SHA256；后续完整提交 SHA 必须另行绑定，不能把任意后续差异视为已审。

权威输入是契约正文 SHA256 `c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`（契约提交 `ccade5edbe47a66c0a48ad09e0667ee7393c148b`）与最终计划 SHA256 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875` 的 Task1。未重开计划或归档审阅。

初始 `/tmp/t1-admission/freeze-source.json` 九文件均经独立 hash 校验。P2 修正后，`freeze-source-final.json` 与初始版本的源码/夹具完全相同，唯一变化是 `tests/test_data_state.py`。最终绑定如下：

- `src/tianshu/data_state.py`：`cd687ecb8e0f823681d95b2d6e873ba1ea153a7618bd6f96742ad2d5dc06c5f4`
- `src/tianshu/data_admission.py`：`57f75e945ff37a6ef3b232ac154afa52182bf2f90f861edfebaaa77e8508e303`
- `src/tianshu/events.py`：`b0a8c2e6b8515a10186474f5e47af1c4e3575a5495542ec76bbb1f09cff2da5a`
- `tests/test_data_state.py`：`e5826e76e6738b716344f773148552554b14b816f5b5e44b508becbe1f3e748c`
- `tests/test_data_admission.py`：`c4d95784b6d6a7d0e1f5c64f53783665cd1f275f471a2e197110e1914df2db0d`
- `tests/fixtures/data_admission/README.md`：`df3f17b269e61ccdd936c21effb2953bfa1b1bdcd9a344521e235ca4f725feba`
- `tests/fixtures/data_admission/legacy-completed.json`：`3db8145607ee790b234024f253d4ac558d4f8913b150b2c323b27ad078034d5b`
- `tests/fixtures/data_admission/legacy-v2.sql`：`42f980a4ec9b5022c72898d88378bb5977db5ddf31d42626c91f94bd7d535ded`
- `tests/fixtures/data_admission/v0.sql`：`5650eaf9d900abc1971e6f742bcde756adcd76409dcb7b93af8d148564ec3692`

审阅者没有写仓库、没有修改作者树做负控，没有读取作者 selfcheck 文件或完整测试日志作为证据。工作树里的知识库/候选文档仍由作者回填，不属于本次最终文档与数量验收。未触碰另一会话的 debug 实现，且已确认 migrate/data_cli/backup/maintenance 相对 base 无代码改动，未将 T2/T3 尚未实现的 API 当作 T1 缺陷。

## 发现与处置

**P2 · 已修并复核：重复 JSON key 测试原来不能检测守卫失效。** 初始 `tests/test_data_state.py:157` 只写两个 `format_version` 字段，缺失其余必需字段。在 `/tmp/t1-admission/mutant-duplicate-keys/` 移除 `object_pairs_hook=_unique_pairs` 后，该原测试仍 `1 passed`，会被“缺字段”拒绝掩盖，不能证明重复 key 检查有效。日志为 `independent-mutant-duplicate-test.txt`。

父会话授权作者只修此测试，最终 L157–L172 先将完整合法 state 落盘并成功读回，再参数化制造顶层 `format_version` 或嵌套 `backup.manifest_sha256` 重复，且要求错误含“重复字段”。产品三个文件 hash 均未变。第二读者把最终测试复制到自己的去 hook 变异副本，亲跑得到 **2 failed，均 `DID NOT RAISE MigrationStateCorrupt`**；当前实现新增两套 **127 passed in 0.80s**。该问题现已销账，不借作者负控日志代替亲跑。

未发现仍未修或部分修的 T1 产品缺陷。缺陷搜寻与测试证据检查是分开的；新增独立探针没有将作者的预期作为正确性的唯一依据。

## 已审实现与验收映射

1. **实际文件与只读前缀。** `events.py:296` 先取得共享维护准入，L317 在 SQLite 打开前排除新空库旁证，L364 在真实连接上检查；只有通过后 L365–L372 才建 payload 目录、设置 WAL/FULL 和执行事件 DDL。`data_admission.py:205` 的 `inspect_connection` 用 `PRAGMA database_list` 反核连接实际 main 文件，读取真实 `user_version`，不导入/构造业务 store。失败关闭连接或释放锁仍走既有异常清理路径。探针用实际 custom.sqlite=v999、无关根 v2、普通 CLI 与错配连接亲证拒绝，比较 schema/类型化 SQL 导出、真实版本、journal mode 和材料/进度内容均不变，payload 目录未创建。
2. **live WAL。** 正常 SQLite 连接读取活库，没有 immutable 路径。独立探针保留持有连接、禁自动 checkpoint，在 WAL 提交 v999；复制主文件到临时静态文件证明主文件版本仍是0，同时 live=999。EventLog 对这个实际活库仍拒绝，逻辑现场不变，证明没有误用旧主文件作证。
3. **v0 与 payload/记忆登记版本。** `_versions`（L94–L132）先核 events/event_versions 真表和必要列，已登记 payload 必须对应真实事件、类型受真实 event_schema 注册表支持且 SQL 存储值为受支持 int；memory_meta 的已登记字符串版本只允许冻结支持值，并有测试绑定真实 memory `_SCHEMA_VERSION`。旧事件缺单行版本仍按0读取、普通 v0 保持 user_version0；缺整个 event_versions 时连续两次重新打开都拒绝，表保持缺失，不通过初始化洗白。未来 payload/记忆登记值在 DDL/补列前拒绝。
4. **严格进度类型。** data_state 的 JSON 重复 key hook、字段精确集合、数字 type(int) 与非负检查、摘要/算法和 canonical 路径检查、连续 applied 前缀及 current/实际位置校验已读。dataclass 构造与读取共用不变量；format1 不可编码成新格式。真实完整 state 的顶层/嵌套重复 key、bool 与未知字段均由独立探针验证。read_state_file 使用 `read_bytes().decode('utf-8')`；独立子进程在非 UTF-8 默认编码下读带中文路径的 UTF-8 进度成功，且 `-X warn_default_encoding` 没有 EncodingWarning。
5. **位置、双文件及 v2 无记录。** `_progress_for`（L179–L202）只从实际 canonical DB 的新 sidecar 读 format2；标准 events.db 另读旧位置 format1，两份都独立严格核验。实际路径、from→2 完整步骤、current 及 completed 事实必须一致；任一坏/错位置文件不被忽略。v2 无任何合法 completed 以 migration_incomplete 拒绝，坏 JSON/格式位置以 progress_corrupt 拒绝；任一 in_progress 拒绝普通连接。独立探针覆盖无记录、旧格式放新位置、新格式放旧位置、一份好另一份坏、自定义 DB 自身进度。
6. **legacy v2 与日常业务。** `_mechanism`（L135–L176）核真实 migration_log 列/约束信息、v2 索引目标/唯一性/partial/排序/比较规则，以及完整步骤与 typed 时间/note。真实历史 SQL + format1 completed 可普通连接兼容；两份 completed 的 from0/from1/from2 均可共享完成事实。独立探针使用真实 `minimal_payload('run.start')` 追加当前契约业务事件、关闭再重开成功，原两份进度字节保持不变；没有把历史基线作为日常业务写后的相等门槛。缺步骤、audit float、生成隐藏列、倒序索引均拒绝且现场不变。
7. **新空库与历史材料。** `_has_material`（L38–L49）允许递归全空目录，任何文件（包括0字节文件）或符号链接均是历史旁证。标准新空库在连接前排除关联进度、非空 WAL/journal/SHM 与 artifacts/blobs/memory_blobs 材料。独立探针真实先构造 SnapshotStore，然后对主文件不存在/零字节两种情况打开 EventLog，空骨架均成功；三类材料目录中的空文件、正文文件、指向空目录的符号链接和 dangling symlink 共12种历史形状均在创建 DB/payload 前拒绝，未丢失证据。
8. **内存库与导入边界。** `:memory:` 不读 cwd 坏进度、不生成 DB 或维护/进度文件。独立新子进程 import data_admission 后 execution/store 模块不在 sys.modules；另用 AST 核对 data_state/data_admission 禁止导入 migrate/backup/EventLog/memory/loop/store。无新增运行时第三方依赖，Python 约束未改变。
9. **历史夹具与规范。** 已将 v0.sql、legacy-v2.sql 中事件 DDL 与 base30b 的 events.py `_SCHEMA` 字符串逐字比对；legacy 机制 SQL 为原两步链，非新迁移器动态造历史。新增 Python 文件没有 TODO/FIXME、跳过/only 或伪测试占位。契约正文和已审计划 hash 均未变；本次内容没有扩大事件/工具/记忆契约或触碰恢复、计费、授权实现。

意图轴结论：Task1 的实际文件准入与有限兼容均有实现和可观测正负控，没有剩余阻断。规范轴结论：作者/第二读者分离、只读审阅、临时变异、真实 CLI、历史夹具、UTF-8 读取和版本绑定符合本范围要求；最终文档、最终组合门禁与原生 CI 仍另行验收。

## 亲跑证据

所有验证均在 `/tmp/t1-admission/review-snapshot/` 或独立变异副本运行。该副本由 `git archive 30b973d…` 创建，覆入经 hash 校验的九份冻结文件；P2 修正后只更新最终 test_data_state.py。没有使用作者的活动源码作为隐式可变测试输入。Python 来自项目 Python3.12 环境，PYTHONPATH 显式指向临时副本。

子进程环境先移除 TIANSHU_* 及以 BASE_URL/API_KEY 结尾的键，再为合成数据注入临时 authority/data root；未打印任何环境变量值。pytest 自带 conftest 仍将每个测试的权威根钉到临时目录，未对真实用户数据或真实模型发请求。

- 新增测试最终组：**127 passed in 0.80s**，`independent-final-new-tests.txt`。
- 计划指定既有 events/maintenance/memory_store 针对回归：**158 passed in 1.22s**，`independent-existing-regressions.txt`。这两组不是完整套件。
- 独立 `independent-probe.py`：**41 项成功**，exit0；结果为 `independent-probe-results.json`，运行输出为 `independent-probe-run.txt`。
- 四种独立变异：移除 EventLog 准入调用、移除 registered 版本检查、移除历史材料拒绝、移除机制检查。各自在临时源码副本运行目标测试，均 exit1，且是行为断言失败，没有语法或导入错误。具体命令/日志对应关系在 `independent-mutation-results.json`。
- 最终重复 key 变异：只移除唯一 object_pairs_hook 后，完整合法正控仍可读，顶层/嵌套两负例均不再抛错，**2 failed in 0.06s**。日志 `independent-final-duplicate-mutant.txt`。初始误证据的 `1 passed` 另存，未覆盖历史原件。

重现主要验证（在相应冻结临时副本、使用项目 Python3.12；同样先清理子进程环境）：

```sh
python -m pytest -o addopts= -q -p no:cacheprovider tests/test_data_state.py tests/test_data_admission.py
python -m pytest -o addopts= -q -p no:cacheprovider tests/test_events.py tests/test_maintenance.py tests/test_memory_store.py
python /tmp/t1-admission/independent-probe.py /tmp/t1-admission/review-snapshot
python /tmp/t1-admission/run-independent-mutations.py
```

变异脚本首次执行要求其目标临时目录不存在；再次复现应使用新的临时输出根或保留原件后选择新目录，不能在作者树删改源码。冻结输入、每份探针/日志摘要、执行环境及最终 source hash 记于 `independent-evidence.json`。

## 保留的验收边界

本审阅未运行全量、未使用作者完整自查结论作证明、未核远端原生 CI。测试单文件修订后的最终全量须由父会话在实际整合候选上运行，旧冻结测试的全量不能冒充覆盖本次最终测试。此处本机 macOS arm64 证据不能宣称 Windows/Linux 原生已通过，更不构成用户设备或安装体验验收。

本次只给 T1 范围内容通过。下一步绑定最终完整提交 SHA，核对该提交与上述 source set 的差异，再结合最终候选门禁/独立证据决定 T1 交付；T2/T3 未获本报告的实施或验收许可。
