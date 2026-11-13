# 数据准入与迁移绑定 · 独立实施计划终审

日期：2026-11-13（Asia/Shanghai）。第二读者：`/root/review_admission_contract`。作者：`/root/executor_data_cli`。本报告只审计划、契约 Git 对象和冻结证据，不授予实施权。

**结论：`PASS_IMPLEMENTATION_PLAN_ONLY`。计划层面未决阻断 0。产品未修复、未新增产品测试或原生平台 CI 验收，G3 未完成。** 三项任务及 16 个 checkbox 均仍 pending。计划通过不能替代 T1/T2/T3 各自的源码复审、真实 RED/GREEN、最终门禁或父会话的分段实施授权。

## 1. 冻结输入与独立性

- 最终计划：`/tmp/tianshu-g3-20261113/admission-implementation-plan.md`，SHA256 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`。本版承接上一版 `a59cff3e12677d34c3889a00f7e48f57e6389dc6a1541b36f135cf24b76c080b`，最终内容已由第二读者逐项读回。
- 计划 freeze：`/tmp/tianshu-g3-20261113/admission-plan-freeze.json`，revision 3，SHA256 `8bfa48053665c93e217a6777013ac8ad16955053a12c33751fda8ade8120de82`。其中 `product_implementation_authorized=false`、`implementation_status=not_started` 与 16 个未完成步骤一致。
- 权威契约对象：`ccade5edbe47a66c0a48ad09e0667ee7393c148b:docs/data-admission-contract.md`，Git blob `b31fb28511e4be3d9214de165bc4fb72e60a110a`，正文 SHA256 `c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`。与本审阅前轮通过的契约及 `/tmp` 保全副本逐字节相同，未重开契约设计。
- 契约提交父对象：`fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45`。规划实验仍绑定当时产品 `f509a53881c372d8d7f4b1e2718130edaff4ed06`；本报告不把之后父会话同步中的共享工作树当作稳定源码输入。
- 本次恢复只读核对了 cwd/status、Git 对象、既有独立探针及计划；仓库有父会话负责的上游同步和冲突处理，审阅者未改动仓库。所有新增审阅产物只写入 `/tmp/tianshu-g3-20261113/`。
- 未读取 `admission-plan-selfcheck.json` 的内容、未将作者自查用作结论依据。freeze 中记录该文件的摘要不构成自查结论的采信。作者与第二读者分属独立上下文，计划修改由作者完成。

## 2. 本轮发现与处置

下列“已修”均指**计划文字与验收项已闭合**，不表示产品代码已修。

1. **已修：首次绑定与续做的真实机制起点不够明确。** 上版仅说业务摘要排除机制、版本另核对，可能让同根且业务/材料相等的跨机制备份被当作初始点。终稿 L196 明确首次新绑定只接受无任何进度的 v0/v1，`backup.real_user_version == live.initial_user_version == state.from_version`，完整机制定义及 typed audit 快照同时相等。L198 明确续做/原绑定完成重复的备份真实版本等于 `state.from_version`，live 只处于 `classify_progress` 可达位置；继承 audit 行值必须保留，差异只允许已提交注册步骤。L245 增加跨 version/audit 拒绝、继承 audit 被改拒绝和原版本备份合法续做正控。
2. **已修：实际 v2 无进度的普通准入与迁移边界歧义。** L151、L196、L243 明确：合法机制表/audit 本身不签发完成事实；无合法 format2 completed 的实际 v2 不能新建绑定。L233、L245 给出普通 EventLog/普通 CLI、迁移库及后续真实迁移 CLI 的拒绝与现场不变验收。T1 的 CLI 只指现有普通 `--db` 启动，迁移器和迁移子命令的工作留在 T2/T3，任务写权边界未扩大。
3. **已修：三个应具体验证的反例仍可能被弱测试漏过。** L233 增加缺整个 `event_versions` 的 close→reopen 负控，以及 format1 放新 sidecar / format2 放旧位置的拒绝；保留缺单行版本的 v0 正控。L245 增加真实多 segment FTS optimize：先证明虚拟查询结果相同且 shadow 持久内容确已不同，再证明旧保存点被拒绝，不以直接篡改 shadow 冒充该反例。
4. **已修：计划仍写契约待提交。** L11、L272 及 freeze 已直接绑定 `ccade5e…` 的契约 Git blob 和完整正文摘要。后续仍须另绑父会话提供的稳定实施源码基线，不能将契约提交后的任何源码自动视为已复审。

没有剩余计划层面的 correctness 或 standards 阻断。产品完成性与原生平台表现本轮不具备可判定的新证据，不能给产品 PASS。

## 3. 两项窄裁定与缺进度判据

**双 completed 记录比较共同完成事实。** 终稿 L126、L233：两份各自按位置/格式严格合法，且关联同一个 canonical 标准 `ROOT/events.db`。legacy1 必须 target=2、completed、applied=(1,2)、current=None；format2 必须路径匹配、target=2、completed、current=None，applied 为从 from_version 到 2 的完整序列（from0=(1,2)，from1=(2)，from2=()）。实际 DB=v2，完整两步机制定义/audit 合法。时间和旧 backup 路径可不同，不要求原始 JSON 字节相等；不能借 legacy 补造 format2 缺失绑定。任一坏记录、in_progress、错位置、目标或版本矛盾均拒绝，不忽略、删除或改写另一份。legacy 单份只产生有限普通连接兼容。

**completed 后 v2→v2 只读 no-op 分 A/B。** 终稿 L208–L213、L245、L249：必须已有合法 format2 completed、当前计划及各关联状态合法相容，并先满足标准布局、独占和完整 v2 机制检查。

- A：本次 backup canonical 路径等于原绑定路径，按原 manifest 字节摘要、真实起始版本、plan/算法、历史 business/material baseline 核验同一次完成的重复。绑定失败、原备份丢失或业务/材料已变均拒绝，绝不 fallback 到 fresh no-op。
- B：本次路径不同，重新完整 verify，源/别名匹配，保存库真实 v2；当前 live 与保存点的完整机制定义和所有 typed audit（含 applied_at/note）、完整业务逻辑与双向材料清单全部一致。不要求业务/材料等于历史 baseline，也不依赖旧备份继续存在；旧 format2 记录本身仍须合法。报告 from=2/to=2/applied=[]/resumed=false/already_current=true，并报告本次新备份。
- A/B 都不改绑、不新建或重写 progress、不刷新 updated_at、不写 DB 行或审计。正常 EventLog 的 completed 日常准入不重算历史业务基线，因此正常后续业务写入不会被历史基线锁死。

**实际 v2 且两关联进度文件全缺的保守、可测试判据：** 普通 `check_connection` 至少需要一份按位置关联、严格合法的 completed 证据：自身新 sidecar 的 format2，或标准 events.db 旧位置的有限 legacy1 completed。两份均不存在时，即使两步机制 schema/audit 完整，也以 `migration_incomplete` 拒绝，不能当作新空库或补造完成记录；坏 JSON / 格式放错位置为 `progress_corrupt`。`data migrate` 对 v2 无记录同样 `migration_incomplete` 拒绝且不写进度/DB；仅有 legacy1 completed 时只保留普通连接兼容，迁移仍拒绝。不把“文件不存在”伪称为 JSON 损坏。这是对已识别有限兼容形状的保守落实，不增加身份表、自动转换或新恢复流程。

## 4. 契约到任务与验收的映射

以下逐项核对契约的承诺与 §5 六条独立验收；引用行号属于上述冻结计划，契约行号属于 `ccade5e…` Git blob。

1. **契约 §1、§6（L8–L22、L156–L171）范围与诚实陈述：** 计划 L5–L22、L266、L272–L274 将 v0 兼容与 0→2 机制链分开，不宣称完整产品格式、恢复 apply、整批 hold、GC、容量保护或 G3 完成；不改另一会话负责的 host/exec/ipc/loop/recovery/budget/net/model 或 CI。无范围扩张。
2. **契约 §2、§5.1（L24–L56、L139–L142）只读前缀：** 计划 L147–L157、T1 L229–L237 明确真实连接 `PRAGMA database_list` 核实际 DB；共享锁覆盖连接，先排新空库历史旁证，再检查版本/旁表/进度，通过后才建 payload、设置 WAL/FULL、运行 DDL。新空库、已识别 v0、v1、v2、未来/坏版本、内存库分支均有正负控；失败比较完整逻辑/schema、状态字节和材料。缺整旁表 close→reopen 与缺单行版本 v0 分开。
3. **契约 §2.2、§5.2（L58–L69、L143–L144）实际路径与进度：** 计划 L126、L147–L152、L233–L237 把 `<db.name>.migration.json` 绑定实际 DB，仅标准 events.db 另关联旧 migration.json；不读无关根作证。custom.sqlite=v999 旁放无关 v2 库的真实 CLI / EventLog / 后台重开负控落在 T1。错误位置不能通过搬文件套身份。
4. **契约 §3.1、§4（L110–L135）严格类型和模块边界：** 计划 L35–L55、L57–L128、L231、L237、L243 定义共享轻量严格解析，拒绝 bool/强制转换/重复 key/未知字段版本/有洞步骤/矛盾状态；data_admission 不依赖 migrate/backup/EventLog/store，AST/子进程 import 验证禁止依赖。T2 让 migrate re-export 兼容公共名，无第二份宽松 parser。没有误称已复现 backup→events 顶层环。
5. **契约 §3.1 的旧格式有限兼容（L110–L120）：** 计划 L124–L126、L151–L152、L229、L233、L243 分离普通 completed 兼容与新续做权。历史 SQL/状态夹具来自绑定 Git 对象，不由新初始化器或新迁移器动态制造；任何 in_progress 即使实际 DB=v2 仍拒绝。双 completed 与 v2 无记录判据已明确。
6. **契约 §3.1、§5.4 的完整备份一致点（L79–L83、L89–L108、L147–L149）：** 计划 L180–L190、L196、L198、T2 L245–L249 覆盖 schema、全部持久表、SQL 类型、rowid/重复行、FTS/shadow、未知业务表、WAL 与 integrity_check；只有形状核验后的本链机制对象可排除。活根 inventory、保存备份实际文件、manifest 声明三方双向相等。异根/过期、只改投影、额外未引用材料、保存点漏声明材料、manifest/plan 替换和 FTS optimize 均有明确负控；首次与续做机制版本等式补足排除项。
7. **契约 §2.2、§3.1、§5.3（L64–L65、L77–L78、L145–L146）布局及互斥：** 计划 L190、L220、L241、L247、T3 L257–L260 限标准根、真实普通无 symlink 文件、nlink=1，不确定则拒绝，并在持锁后复查。真子进程配屏障分别验证共享挡独占、独占挡普通打开和硬退释放；真实 os.link/symlink 用例在落进度前拒绝。平台不支持构造时如实记录原生 skip，不能冒充 Windows 已验证。
8. **契约 §3、§5.5（L82–L87、L150–L152）落盘、事务与恢复：** 计划 L196–L206、L247、L257–L260 要求进度先落盘，再用同一真实连接将每步 DDL/version/audit 同事务提交；仅 current_step 的提交窗口可领先一步，不按 DB 数字补造 steps。四个原 failpoint 都由真实 CLI 硬退 77，续做和再次执行幂等，普通连接中间态拒绝；写进度/DDL 失败不得报告 completed，事件顺序及 append-only 仍核验。
9. **契约 §3/§4 的 CLI（L73–L75、L134–L135）：** 计划 T3 L253–L260 只新增标准根 migrate，backup 必需，成功0、拒绝/失败2、failpoint77；六类结构化错误可区分且复用既有脱敏。参数/输出不复制算法，不停止服务或启动 daemon。旧“migrate 不存在”测试明确调整，restore apply 继续不存在。
10. **契约 §5.6（L153–L154）独立验收：** 计划 L231–L237、L243–L249、L257–L262 每项保留真实 RED/GREEN 和独立读者边界；T2 要通过移除材料/manifest/plan/版本等式/FTS shadow 等检查实际变红，临时负控与候选提交隔离。最终要求完整源码/测试 SHA、uv lock/pytest/ruff、本地占位/伪测试检查与原生 required CI，不借本地或旧绿色冒充。当前这些均是待执行计划。

意图轴结论：契约承诺已映射到有限任务和可观测断言，未发现剩余漏项。规范轴结论：契约独立 commit、作者/复审分离、无运行时新依赖、Python3.12、文档诚实与后续门禁要求均保持；未把计划层 PASS 提升为发布/产品 PASS。

## 5. 独立证据与可重现性

本轮沿用本审阅上下文前轮亲跑的合成探针，不重复调查或全量测试：

- 脚本 `/tmp/tianshu-g3-20261113/admission-plan-independent-probe.py`，SHA256 `f616f5b741a4587d468c49884f08406d694fc1eeb44db81ba7290e315c0c34e9`。
- 输出 `/tmp/tianshu-g3-20261113/admission-plan-independent-probe.json`，SHA256 `8d37c8d5f82850ee89391a0b88609c66b3193f09f2e52abdda6a33ab2b04be22`。前轮退出0，本轮只核对冻结内容。
- 关键观察：完成迁移后真实追加合法 `run.start`，旧 backup 仍 verify=true、历史 progress 字节不变，但业务摘要已改变；不同路径的新真实 v2 备份 verify=true，业务和完整机制与 live 匹配。随后只改机制 audit.note，业务摘要仍匹配，完整 typed 机制已不匹配。这分别支持 A 不得降级 B、B 不能只比较 business hash 的计划要求。
- 该脚本使用现有备份/旧迁移及临时 typed 摘要，仅验证反例和逻辑必要性；不是未来 data_digest、data_admission 或新 CLI 的验收，不证明完整新算法。历史 FTS/硬链接/旁表重开等证据仍见先前契约独立报告，本轮未重复。

在对应历史基线的隔离 checkout 中，可使用项目 Python3.12 运行脚本：`uv run --frozen python /tmp/tianshu-g3-20261113/admission-plan-independent-probe.py`。脚本中的 REPO/sys.path 和输出根是当时现场；移机时只在临时副本改为该 checkout 与临时输出目录，并只用合成数据，不在新产品代码上把旧输出当新回归结果。

Git/归档核对可复现命令：

```sh
git show --no-patch --format='%H%n%P%n%B' ccade5edbe47a66c0a48ad09e0667ee7393c148b
git diff --name-status fd5f56cdbc83b07f65358c5bd4ae56d7cf18bc45 ccade5edbe47a66c0a48ad09e0667ee7393c148b
git show ccade5edbe47a66c0a48ad09e0667ee7393c148b:docs/data-admission-contract.md | shasum -a 256
git diff --name-only f509a53881c372d8d7f4b1e2718130edaff4ed06 ccade5edbe47a66c0a48ad09e0667ee7393c148b -- . ':(exclude)docs'
```

逐项结果冻结于 `/tmp/tianshu-g3-20261113/admission-plan-git-binding.json`，SHA256 `66a28e740bcfa6af8cb4300bacb6cabca0425a6ff22b3bbb9b5cb1cfd08b77ed`：

- `ccade5e…` 父对象准确为 `fd5f56c…`，11 份改动全部位于 docs，无非文档差异。
- 独立报告及另六份探针/结果/测试输出共七件原件与 Git 归档逐字节相等；报告 SHA256 `a541404561b55dc37dabe31e73cba2f8565395bc4a1a7089c291b48399e7cc93`。未把作者摘要当成原件。
- 归档 README 明确源码基线、设计通过的范围、历史脚本如何在临时基线复现，以及未来应重新验收；没有声称新实现通过。
- README 所称 f509→契约提交仅多文档和一行 UTF-8 测试修正准确：唯一非 docs 路径是 `tests/test_backup_materials.py`，仅 `read_text()` 改为 `read_text(encoding="utf-8")`。本报告没有重新跑或借用父会话的 2273/7 全量、未核验远端 CI。

## 6. 交接边界

下一步由父会话在上游同步与门禁完成后绑定稳定实施源码对象，复核与该对象有关的定位/验收差异，再明确分段授权。若只授权 T1，executor 只可执行计划 T1 文件与验收范围；不得提前实施 T2/T3。之后各阶段须由第二读者按最终源码 SHA 亲跑关键负控，产品与 CLI 全部验收后才能报告本片完成。

当前没有产品实施授权，没有新产品绿色、原生平台绿色或发布结论。当前代码中前轮识别的旧旁表重开等行为仍须由后续实现和验收销账；本报告的“已修”不能作为产品修复证据。

## 7. 终审时的后续继承对象

父会话在终稿冻结后提供了稳定继承对象 `30b973dfe21ce71f40ccfd7e84412de33d23877a`。第二读者补做了有限 Git 对象核对：`ccade5e…` 确为其祖先，契约正文仍为 c62d…b8584；相对 `16721ff14ad08456e8f6a2522c3300b2652b0918` 的差异仍只有原 11 份 docs。

`events.py`、`migrate.py`、`data_cli.py`、`backup.py`、`maintenance.py` 以及版本依据 `event_schema.py`、`memory.py` 共七个相关产品对象相对 `ccade5e…` 的 Git blob 均未变。因此本计划的上述局部定位/语义映射没有新的源码差异需要重审。其余上游 debug 范围不属于本报告；不重审、不重复探针，也不将父会话报告的 full 2416/8 当成本审阅亲跑结果。稳定实施分支和 T1 写权仍由父会话明确指定。

补充冻结文件：`/tmp/tianshu-g3-20261113/admission-plan-inheritance-binding.json`，SHA256 `aed113acb7e80d96dee89af6c430a272ec0733a025b7f0f6b654456e5dd6b636`。本补充不改变计划 PASS 或产品未验收的结论。
