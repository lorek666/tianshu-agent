# T1 round1 独立窄复验

**规格：PASS_T1_ROUND1_NARROW_C1_C2_WITH_EXACT_V1_INHERITANCE。质量：PASS_T1_ROUND1_NARROW_SIX_FILE_DELTA_AND_ORIGINAL_TAKEOVER_GUARDS。** C1、C2 与 Q1/takeover 在本轮分别销为**已修**，本轮授权范围内无剩余阻断。v1 FAIL 报告及其全部原件保持原字节；本结果不改写 v1，也不等于整项低磁盘功能、最终门禁或原生 CI 已通过。

## 精确范围与输入

固定 root 候选 `/tmp/tianshu-g3-20261113/low-disk-t1-round1-root-20261114-5g28_83k/candidate-complete`，542 源文件、相对 H 共40文件候选；尚无实现提交 SHA，作者 HEAD 为同基线 tree 的 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215`。完整 patch SHA `ecb4e7b713ff4fd0f78080748bddd5cec0f7f69fc0f06cf0d00ce6e61f1bd6a1`，round1 delta SHA `ede3898a6d3b1470c7c545b233ec280cff8586cfce9a34fba50dfe290f9a89a2`，作者 manifest SHA `4ac68a766cf941509c4c38436fdf48806692e7401ff9b84e099813868b68bfa5`。root 的 `source-hashes.json` 自身 SHA 与完整原件索引另见 `external-input-index.json`，不混淆这两种清单。

本读者先读 root facts、source-hashes、delta，再重读原 task-1-brief 与已合契约。独立重新计算542文件 hash/大小/0444模式，前后完全相同、目录仍恰542文件，无缓存新增。与 v1 原始542源清单独立对照，只有六项变化：KNOWLEDGE、C3 schema、dispatcher、event_schema、新低盘创建测试、takeover测试；其余536文件字节相同。原 v1 的47项索引与索引自身48文件再核保持原样。v1源目录既有57缓存继续留痕，不删、不加入本轮源差异。

本轮未读作者自查结论/报告或 live 源，也未把作者467/800/659项的结果当本读者执行证据。root报告659作者原件已核是输入完整性事实；这里执行的是下面列出的独立探针。本轮全部运行在新 `/tmp` 工作副本，原542源不写；Python `-B`/`PYTHONDONTWRITEBYTECODE=1`、pytest `-p no:cacheprovider`，provider环境已清理、数据根均临时。未提交/推送/修改 refs/index/调用 API、真实模型或收费端点。

## C1：已修

修复仅将 `open_control_run` 的 create_task/start_run 放入可组合 `EventLog.transaction`，预检/MCP准备保持在前，租约/队列次序保持在后。独立 `c1_real_controls.py` 从真实 direct control、remember、undo 入口分别运行全高、初低、最终低、最终未知12案。undo 的来源文件由真实 Loop/create_report 先生成，已有 snapshot 与文件字节作为前证；没有手造 Run 或跳过权限。

`c1-real-controls.log` **15 PASS / 0 FAIL**：除12入口案，还含外层事务全高后异常回滚、最终低与最终未知传播回滚3案。最薄磁盘 leaf 根据真实事务中是否已出现新控制 Task 标识最终检查，从而在外层事务内也能区分预检/最终检查；未 patch 创建入口放行。拒绝案中 tasks/runs/events/event_versions/jobs/leases/approvals 七表**完整行**及既有 snapshot/workspace 材料 hash 完全回到 before；足量正控实际新增 Task/Run/Job，undo正常移除其目标。外层案SQL trace只有一次 BEGIN。

独立负控 `negative-c1-remove-transaction` 只在工作副本撤掉这一处事务，使用相同探针得到**9P6F**，失败恰为三种入口的最终低/最终未知；随后按SHA逐字节恢复。它复现了v1空壳缺陷，证明当前正控不只是初始预检通过。

保留既有嵌套事务语义：异常必须传播到外层才触发整个外层回滚；这次没有引入savepoint，也没有把租约/整个控制执行包入新长事务。不得把被调用方内部捕获后仍主动提交外层的情形说成本片保证。

## C2：已修

新增语义 hook 只在 `run.start` v2 结构检查通过后复用原严格 `RunExecutionPolicyV1.of`，错误沿 `PayloadContractError` 返回。没有改严格对象或通用 `_type_matches`。schema镜像只增加准确description，明确标准 JSON Schema integer 不能区分1与1.0，项目事件写入另有严格语义检查。

`c2-real-write-read.log` **13 PASS / 0 FAIL**。effects、pure_only两mode分别用整数1、1.0、True、未来2调用真实 `EventLog.append`，带超过offload阈值的有效大文本，随后关库重开走真实TaskStore reader。整数1真实持久v2并严格读回；其余写端 `PayloadContractError`，events/event_versions及blob字节集合不变，重开无该事件。其他当前事件/字段的goal_revision=2.0仍真实写读为float；用精确H schema仅在播种窗口真实append历史v0/v1，再恢复当前模块重开，历史policy仍None，v1的2.0原语义保留。45类型历史/current/schema镜像逐项结构对账；严格对象源码、通用integer匹配函数与历史fixture字节保持。

独立负控 `negative-c2-remove-semantic-hook` 只移除新v2 hook，相同探针得**11P2F**，恰为两mode的float1.0写入/读取不闭合；已精确恢复。这不是全局收紧integer，也没有用放宽 `.of` 洗坏数据。

## Q1 / takeover：已修

静态字节对照确认 `tests/test_takeover.py` 唯一变化就是在原真实子进程 Loop 构造中传同stack的memory/contents；移除该行即精确还原v1整份测试文件。原LeaseConflict rc2、权威阻断、holder、recovery.blocked、目标文件字节断言全部保留。

本读者实际在新副本运行原完整模块：`uv run --frozen --python <3.12.13> python -B -m pytest -p no:cacheprovider -o addopts= -q -rs tests/test_takeover.py`，**6 passed in 1.27s**，未skip。命令/日志在 `takeover-original-tests-*`。这是本轮独立执行，区别于v1报告只亲核作者1F5P原件。

另 `takeover_real_trace.py` 不导入测试helper，而是从原fixture AST提取未改写子进程程序并执行，独立检查生产持久结果；`takeover-real-trace.log` **3P0F**。真实前任SIGKILL后，Loop与control的半写case实际输出`REFUSED 未核实`、**rc2**、stderr为空；holder仍run_dead、权威阻断、恰一条对应c1的recovery.blocked、目标文件hash不变。control干净正控输出ALLOWED/rc0，holder换成新Run、有lease_takeover审计、无权威阻断且文件不变。原fixture的SIGKILL语义只在本次macOS/POSIX环境验证，不冒充物理断电或Windows证据。

独立负控只删新加的两实例参数，运行原单条Loop测试，真实重现**1 failed**：context mismatch rc1而非预期rc2；已精确恢复。

## 六文件质量复核、继承与限制

- 六文件diff已逐读，改动限于C1原子性、C2新v2语义/准确镜像说明、定向测试、takeover实际实例及知识库记录；未见删除原业务断言、全局足量stub、生产legacy后门或未知策略兜底。四个变化Python独立compile，新增diff行无TODO/skip/only占位。
- 原v1未变范围按 `inheritance.json` 的536路径和原证据精确继承：阈值/context、实际位置与44叶子案、PURE_ONLY真实清单/材料、全部生产者与子Run、原Claim/JobStore/LeaseStore守卫、真实双进程竞争与重开等。新v2有效政策写入、control/remember/undo与takeover的受影响处已由本轮补证。未重跑800/467或作者23变异全部。
- **OBSERVED_LOCK_HELD_DURING_CONTROLLED_LEAF** 及root裁决原样保留：最终事务内现读绑定+现测；受控Event等待证事务持锁，不能证明原生disk_usage长延迟、死锁、平台SLO或时限已修。OS/文件系统阻塞没有本次证明的时间上界。本轮不改guard→事务顺序或另建timeout框架。
- 原生不同卷/真实嵌套挂载、其他平台、实际ENOSPC/恢复/soak、T2执行/恢复/停止例外、T3完整入口与组合门、最终full/lock/ruff/原生CI仍未在本轮运行。原v1同tmp卷叶子注入的证据边界保持，不能借继承升级成跨卷平台通过。
- README:280的49个.py数字既有漂移仍未改（H63、本候选65），作为原低优先文档提示保留；最终交付前文档对账仍需处理。它不属于本次六文件修复窄结论的已修项。

## 复现、封存与下一步

`run_probe.py <new-tag> c1_real_controls.py`、`c2_real_write_read.py`、`takeover_real_trace.py` 分别运行对应独立探针；`run_probe.py <new-tag> --pytest tests/test_takeover.py` 跑原六项。须在新的可写/tmp副本运行，禁止覆盖既有raw或直接向冻结根跑pytest。`negative_controls.py` 仅可针对可丢弃工作副本：它逐项扰动、记录diff/失败输出并finally还原；本报告的三份负控和所有正控均已实际执行，最终工作副本542源hash精确等于冻结候选且无额外缓存。

全部命令、原始输出、源前后清单、独立继承清单与结构化facts随本报告封存。`hash-index.json`包含本目录所有文件（含工作副本）但不含自身；文件统一0444。reviewer worktree仍是cefa1e375974d96e2ec25e199f6605f6dcf2239b、detached clean，stage/unstaged均空；common refs共享，本lane只读不据此声称其他lane未更新refs。

本轮C1/C2/Q1可销账，六文件未见新增阻断；是否开启T2由root另行授权。本报告不实施T2，也不宣称LD-01–06整体或最终提交/原生CI已验收。
