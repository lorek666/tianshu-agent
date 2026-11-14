# main · run43576167008 / attempt1 原生CI独立验收

结论：**FAIL_THIS_MAIN_PUSH_CI_ONLY**。本次 `push` 到main的首次原生CI总体 **failure**：Windows两分片各有一个真实call失败，Windows汇总因上游failure明确拒绝通过；Ubuntu、macOS和UI三个job为success。3206个Windows节点的全部实际阶段已独立对账，失败保留。PR149的历史绿色只属于其原run，不覆盖或稀释本次main红。**两条本次失败及原b450失败的根因均未由本报告确立。**

## 精确身份与审阅范围

- 仓库：lorek666/tianshu-agent；run：43576167008；run_attempt：1；event：push；head_branch：main。
- run head、实际六job checkout、两artifact的checkout_sha/github_sha均为 `c2cf1c71c65f46dea161e41b140df99d137a5686`。
- Git tree：`35bc297ce3125be4fd8db6fc4087196507a94045`。
- main merge commit的parents按序为 `b450c339d546ae2007ed9754e70de0108c4b86c4`、`a079455ee5aac0b4458cf8166511ab8abd9f79e6`；原始head/checkout Git对象逐字节相同。
- 本报告是独立第二读者对固定main原件的失败验收，包含新鲜只读GET。未创建agent、未另起collector、未跑本地full、未改live/测试/产品源码、未写refs/远端、未merge/rerun/cancel。

main与此前PR149是同一tree，因此仅精确继承四个固定输入字节、旧解析规则和历史内容审阅身份。四输入为workflow、pytest分片器、watchdog包装器、M2测试；六份固定/失败定位源码再次从现有a079 Git对象导出，大小/哈希全部匹配，local source tree等于main原始Git对象的tree。没有fetch或把a079冒称本次checkout。M2测试SHA256仍为 `766a3496526594009638739bf093fd190d76d4adcd16c61acdf405504a0a4161`。

先前PR149报告的151件完整包重新逐件核了payload及index自身，REPORT/facts/index身份不变；它继续保留历史 `PASS_PR149_ATTEMPT1_NATIVE_WITH_RETAINED_WATCHDOG_EVIDENCE`，并非本次main结果。PREPARATION、首红通知和preterminal快照保留其历史准备身份，正式结论以本REPORT与facts为准。

## 六job、steps与实际环境

- **132390772315** `test (ubuntu-latest) · py3.12`：success；**3193 passed / 13 skipped**，468.51s。
- **132390772334** `test (macos-latest) · py3.12`：success；**3198 passed / 8 skipped**，900.11s。
- **132390772492** `windows shard 0 · py3.12`：failure；**1 failed / 1582 passed / 20 skipped / 1603 deselected**，891.19s。
- **132390772345** `windows shard 1 · py3.12`：failure；**1 failed / 1585 passed / 17 skipped / 1603 deselected**，741.15s。
- **132394433521** `test (windows-latest) · py3.12`：failure；两artifact下载step成功，`验证上游结论与完整覆盖` step失败。
- **132390772107** `ui · typecheck / test / build`：success；**13 files / 91 tests passed**；类型检查、构建及dist一致性step全部success。

六job/CheckRun都completed，来自app15368并绑定同一head/check-suite117991074581。两Windows分片的Pytest step失败、上传原始报告step成功；这两job及Windows汇总的 `Post Run astral-sh/setup-uv@v5` 均为skipped，其他step全部success。不存在被取消或整个被跳过的job；未隐藏三个post skip。

原生日志安装Python均为 **3.12.16**。实际Ubuntu是26.04.1 LTS/image ubuntu-26.04；macOS是26.7.2/25G332/image macos-26-arm64；两Windows是Microsoft Windows Server2025 10.0.26100 Datacenter/image windows-2025-vs2026，Python下载目标为windows-x86_64，pytest为platform win32。名称为 `test (windows-latest)` 的汇总job实际运行在Ubuntu runner，不混称Windows执行环境。这里只是本次hosted CI事实，不宣称Windows10/11、ARM64用户设备或桌面体验已验证。

## 两artifact、完整collection及全部phase

片0 artifact **14106651538**：132022B，ZIP SHA256 `ca9ea030aacdebb491f8b6d780ebe415a70b0ab9e1bfb139d767420d8fa681ab`。

片1 artifact **14106374876**：133074B，ZIP SHA256 `6fc9a83bb4d98420669dddb5c5bfb7e14ecd9e683e49d496f41f2074719c69f7`。

两包的API名称/run/head/大小/digest与本地ZIP逐件匹配，每包恰有一份有效shard.json；解出的原始字节一起保留。两runner的full_collection逐项完全一致：**3206 node / 123 files**。独立按固定源码规则重算文件级greedy分片，结果1603/1603；selected与finished逐项相同、无重复/陌生node或phase；两分片交集空、并集恰好完整collection。两session exit均为 **1**，不是0。

- 片0：setup1603、call1584、teardown1603；19个setup skip、1个call skip、**1582 call passed + 1 call failed**；teardown全部passed。
- 片1：setup1603、call1589、teardown1603；14个setup skip、3个call skip、**1585 call passed + 1 call failed**；teardown全部passed。
- 全部Windows合计 **2 failed / 3167 passed / 37 skipped**。无wasxfail行、无collection error。失败phase恰为下面两项，且两项均setup/teardown passed。

逐节点阶段台账在 `audit-output/all-windows-node-phases.csv`。原始artifact报告与两份-vv日志结果也对照：每片1603条结果，其位序/结果全部匹配；片1所有名称完全相同。片0有三条敏感内容测试的参数名称在日志中呈 `***` 占位，artifact保留原node名称；三对原值/日志字面值及结果完整列在facts的 `windows_collection.verbose_log_crosscheck.0.literal_log_redaction_placeholders`，其余1600条同名。未把原始日志说成每个node字节都完全相同。

## 失败一：normal-finish再次实际失败

节点 `tests/test_m2_acceptance.py::test_normal_finish_and_pause_do_not_report_heartbeat_failure` 在片0日志 **L1049** 为FAILED，真实call耗时 **34.68s**；详细失败块为 **L1926–1976**，断言位置M2源码1003行。

**iteration=13**；实际 `run.state='failed'`，期望 `succeeded`。`worker.errors=[]` 与失败同时出现，不能解读为没有错误：`run.end` 明确记录 `error='主循环异常：OperationalError: database is locked'`、`outcome='failed'`、`effect_state='none_applied'`、`tokens_unknown_calls=1`。原事件序列包含tool.intent、dispatch.admitted、execution.started、job.finished、run.end、run.finish；完整序列及run.end保留于facts和原日志。

诊断附注原样保留并解析了 **41条JSON**。日志L1937给出transaction的 `OperationalError / SQLITE_BUSY(5) / phase=enter`，MainThread、nested=false，caller为 `src/tianshu/events.py:508 append_many`；L1938最深transaction帧为 `events.py:412 transaction`，固定源码该行执行 `BEGIN IMMEDIATE`。这比仅有database-is-locked字符串更具体，但仍只建立观察到的失败边界。

L1941的 `transaction.active_at_error` 是同run/job/epoch下、`tianshu-heartbeat`线程、`leases.py:621 renew`、phase=acquired、native_in_transaction=true的进程内context。**这不是OS权威锁owner证明，不据此宣称heartbeat已被证明是根因。** collection同时报告 `tx_dropped=5660`、`error_dropped=8`；其余evicted/dropped/collection_failed为0。有限ring已经丢弃条目，不能宣称完整事务历史。本附注error类别只见transaction和test，不把没有drive/beat错误记录编造成另一个捕获故障。

完整原文和仅Unicode转义解码的可读副本分别在 `audit-output/failure-normal-finish.txt` 与 `failure-normal-finish-unicode-readable.txt`，每条诊断JSON另在 `normal-diagnostic-rows.json`。本报告不承担另两位审阅者的根因诊断工作。

## 失败二：MCP发送背压返回期限断言

节点 `tests/test_mcp_transport_timeout.py::test_stalled_send_is_unknown_and_replay_does_not_retry` 在片1日志 **L1467** 为FAILED；失败块 **L1822–1827**，实际call耗时 **8.84s**。

固定源码159行执行 `returned = done.wait(2)`，163行断言 `assert returned, "真实派发因 MCP stdin 背压突破调用期限"`；本次日志明确 `assert False`。工作线程运行engine.resume_run，finally会释放peer/worker并close_all；记录的8.84s是整个call耗时，不能直接当作被测2秒期限内部实现的耗时或根因。

**163行断言失败后，164–176行后续unknown/replay断言没有执行。** 不将测试名称当作本次已经证明unknown/replay语义，也不把它叫flaky或环境问题来稀释红。完整原块和Unicode可读副本在 `audit-output/failure-mcp-send.txt` 与 `failure-mcp-send-unicode-readable.txt`。首红提前取得的片1job.log与完整collector最终job日志逐字节相同，SHA256均为 `36fa0f28dbfea5fe207a2ca627926adef3580bb94a9325a2b4b24795550ed475`。

## Windows汇总明确拒绝

汇总job日志 **L238** 的环境值为 `UPSTREAM_RESULT: failure`；**L240** 明确 `CI shard verification failed: Windows shard jobs did not all succeed`；**L241** exit code1。固定verify函数第一条语句先require上游success，本次在读报告/验证覆盖前即拒绝。

因此官方汇总**没有给出coverage通过结论**。两artifact下载成功、本报告独立完成全collection/phase对账，均不改变官方汇总及本次run的failure。摘录在 `audit-output/failure-windows-summary.txt`。

## junction真实通过及本次两次watchdog

片0的 `test_worker_diagnostics_real_transaction_caller_and_nested_reuse` 在日志 **L1056**，两个junction参数 `[original-src]` / `[external-copy]` 分别在 **L1057/L1058**，均为setup/call/teardown三阶段真实passed，无skip/xfail。固定nt分支使用check=True执行 `cmd.exe /d /c mklink /J`，随后必须验证resolve、alias co_filename、真实LeaseConflict和caller/frames。成功pytest捕获未单独打印mklink stdout，不虚构独立命令跟踪。junction回归通过与normal-finish失败同时保留。

仅计数原日志中完整独立 `PYTHON STACK WATCHDOG` marker，本次恰 **2次**，均片0，不继承PR的3次：

1. **08:37:56.2838009Z / L750**：`test_eval_separation.py::test_cli_compare_accepts_previous_report_as_baseline`；真实call **60.25s**，L796于08:37:56.5327952Z随后PASSED；test frame为源码114行。
2. **08:43:45.6220794Z / L1543**：`test_recovery.py::test_100_random_fault_injections`；真实call **62.62s**，L1599于08:43:48.2422412Z随后PASSED；test frames为_spawn135行与测试419行。

两node均artifact三阶段passed；完整marker、全栈、后续结果与duration摘录分别在 `audit-output/watchdog-1.txt`、`watchdog-2.txt`。这是保留的长等待事实；normal-finish不是这两node，不把watchdog堆栈冒充其错误现场。未见Fatal Python error或watchdog采集/退出失败；但C扩展长期占GIL时无法保证60秒栈的已知边界仍保留。

## 实际skip、注解与保护快照

Ubuntu13、macOS8、Windows37个skip的实际-rs分组计数、源位置、原理由、仅Unicode解码可读理由和原日志行号全保留在facts与 `SKIP-REASONS.md`。Windows的33个setup skip/4个call skip只代表相应语义未执行；每片1603 deselected是已独立分区核验的另一半，不混称skip。

六job的annotations所有分页原件与聚合及count核对一致，共 **13条：6 warning、4 notice、3 failure**。warning为Node20 action被强制Node24运行的弃用提示；notice为Ubuntu标签迁移及macOS队列容量提示；三failure均是exit1。三job的error日志标记也保留，没有把failure注解过滤掉。

本次六项新鲜只读GET：main ref、classic保护、branch rules、c2cf的CheckRuns、legacy status、PR149。报告时main ref仍为c2cf，PR149 closed/merged且merge_commit为c2cf；head/base仍为a079/b450。六真实CheckRun的3success/3failure与原件一致。

四required的准确名称为 `test (ubuntu-latest) · py3.12`、`test (macos-latest) · py3.12`、`test (windows-latest) · py3.12`、`ui · typecheck / test / build`，均app15368；其中**required Windows汇总failure**，其余三success。保护仍strict=true、enforce_admins=true、禁强推/删除；required approving reviews为0；rulesets branch rules空列表。legacy status是state=pending、total_count=0、statuses=[]，这是没有legacy上下文，不能冒充六CheckRuns尚在pending或掩盖三红。所有查询仅为时点快照。

## 原件、观察器错误与封存

完整collector **96件 / 1762771B** 在复制前后及审阅后逐件集合、大小、SHA256一致；首红两件也同样复核。collector的errors=[]只表示采集没有报告错误，approval=false只是其元数据，不是独立验收结论。产品结论来自原生run/job/check/artifact/日志的独立对账。

审阅脚本首次误用verify源码拼写假设自行报错，已改成真实require表达式；随后新增全-vv名称对比时，对三条 `***` 日志占位的字面相等假设再次报错，已改为完整保留差异并逐条核对位序/结果。两次观察器脚本及stdout/stderr/run记录分别保留在 `observer-attempt-1/2`；最终audit exit0、stderr0仅说明独立解析完成，**不把观察器成功当作产品成功**。

另一次封存前continuity观察器误读默认windows-ci-fix工作树，把其中cefa HEAD错误描述成g3现场变化；cefa只属于错误路径，并不是当前g3 HEAD。该错误读回及未封草稿保留在observer-wrong-worktree，observer-continuity-expectation明确标注纠正。随后所有Git读取使用显式g3-data-admission路径；新鲜live-readback实际记录g3 HEAD为c2cf、分支codex/windows-deadline-and-lock-observation，具体未提交改动以该时点原输出为准，不声称其他作者此后未修改。未回退或修改任何工作树；main固定原件与同tree源码身份不受影响。

正式脚本、输出、六项GET、固定输入、历史继承身份、全部原件及失败/skip/注解/watchdog证据均逐件索引。index schema为 **payload**，精确列除自身之外所有文件的bytes/SHA256/mode；自身及全集合由外部seal receipt记录。文件0444、目录0555、无symlink，并在封存后独立进程再次回读精确集合/大小/hash/mode及REPORT/facts结论。

本结论严格限本run/attempt及完整c2cf SHA：**FAIL_THIS_MAIN_PUSH_CI_ONLY**。两个真实失败、根因未定、诊断ring缺失、长等待、skip和用户设备验证边界继续保留。
