# Windows CI：调用计时边界与 SQLite 失败现场

2026-11-14。本轮基线为 main
`c2cf1c71c65f46dea161e41b140df99d137a5686`，tree
`35bc297ce3125be4fd8db6fc4087196507a94045`。原 Windows 失败的具体触发机制仍未确定；
本轮只修测试计时边界并补测试诊断，不改变生产期限、重试或 SQLite 写入策略。

## 原生 CI 的实际结果

M2 诊断 [PR #149](https://github.com/lorek666/tianshu-agent/pull/149) 的首次
[run43573586653 / attempt1](https://github.com/lorek666/tianshu-agent/actions/runs/43573586653)
六个 job 成功，独立结论为
`PASS_PR149_ATTEMPT1_NATIVE_WITH_RETAINED_WATCHDOG_EVIDENCE`。
Ubuntu 3193 passed / 13 skipped，macOS 3198 / 8，Windows 两片合3169 / 37；
Windows 完整3206节点，UI 13文件 / 91测试及类型、构建、产物一致性通过。
真实 checkout 为 `1e2d75faec06d5f2cd12b5936b801008873d0573`，
有序 parents 为 b450、a079，tree 与 head 相同。正常保护合并后 main 为上述 c2cf，
有序 parents 为 `b450c339d546ae2007ed9754e70de0108c4b86c4`、
`a079455ee5aac0b4458cf8166511ab8abd9f79e6`，tree 仍相同。

该 main 自身首次
[run43576167008 / attempt1](https://github.com/lorek666/tianshu-agent/actions/runs/43576167008)
**失败**：UI、Ubuntu、macOS 成功，Windows 两片各有一个 call 失败，汇总正确拒绝。
合计 **2 failed / 3167 passed / 37 skipped**，完整 collection3206，
每片 selected=finished=1603。两失败的 setup/teardown 均 passed，不能把完整报告当作通过。
本次 Windows 两次 watchdog 对应的其它节点最终通过，长等待事实保留；不是 M2 错误现场。
PR 的绿色不能覆盖 main 的红。

验证环境仅为该次 GitHub-hosted Windows Server2025 x86_64 / Python3.12.16 runner；
不代表 Windows10/11、ARM64、安装或用户设备体验。没有取消、重跑或放宽 required 门禁。

## MCP：已证明原测试混合计时，原 Windows 卡点仍未知

片1失败于
`test_stalled_send_is_unknown_and_replay_does_not_retry` 的原 `done.wait(2)` 断言。
控制 socket 的 `ready` 在服务端响应 `tools/list` 后发送，早于 Run 创建、模型、审批、
实际 `tools/call` 及结果落盘；`done` 却在整个 worker 收尾后才置位。原 call8.84秒
还包括 finally 中的 join 和 close，不能当作真实发送耗时。该断言失败后的
unknown、Receipt、replay 断言在这次 CI 中没有执行。

在精确 c2cf 的隔离副本中，以真实子进程 stdin/stdout 背压执行一次观察与两次 Event 屏障：

- 正常观察通过，真实 `_send` 约222ms后抛 `McpTimeout`，worker约408ms结束。
- 在真实审批原方法前阻塞，原2秒到期时尚未进入 `call_tool`，原断言失败；
  放行后真实发送按约224ms超时并持久化 unknown。
- 在真实 unknown 提交前阻塞，发送已按约224ms超时，原2秒仍到期失败；放行后完成落盘。

两次刻意红均保留，不能说原断言后来通过。插件只读核实真实 DB 有唯一
failed/unknown/mcp.timeout、无 Receipt；另对 DB 副本 replay 两次，33条事件不变。
这些是造成误判的充分机制，不能证明原 Windows 恰好停在这两个阶段。
本机环境为 macOS arm64 / Python3.12.13 / SQLite3.50.4。

修正目标是观察真实 `call_tool` 的入口与退出，包括异常和回收；原2秒约束直接检查
该区间的 monotonic 时长，避免主线程迟调度时事件已置位而漏测。保留真实200ms配置、
背压子进程、原 join5，以及实际 DB 的 unknown、无 Receipt、两次 replay 不变断言。
准备与收尾须有界并传播失败，不能把 transport 完成当作整个 Run 完成。

## M2：已定位真实 BEGIN，补足被淘汰的等待窗口

片0正常收尾用例第13轮实际 Run failed，worker.errors为空，但 run.end 记录
`OperationalError: database is locked`。事件已到 execution.started，之后无 tool.result
或 Receipt；不能由 run.end 的 none_applied 反推文件完全未动。

诊断明确定位主线程 `append_many → EventLog.transaction → BEGIN IMMEDIATE`
进入阶段的原生 `SQLITE_BUSY(5)`。同时保存了心跳 `LeaseStore.renew` 活跃 context，
acquired 到错误采样约32ms、到 exit约63ms。这是观测时间差，不是单个5秒持锁证明。
原 BEGIN 的 attempt 已被有限 ring 淘汰，无法重建其实际等待起点。

源码中一次 beat 先执行 JobStore.heartbeat，再读取租约、执行 LeaseStore.renew，
两次独立写事务。本用例1ms压力与默认5000ms不同；连续短写重新抢占只是待验证假设。
现有首层 caller 也不能区分正常 `_commit_result` 与 `_fail_unknown` 的 append_many。

诊断增量只在局部 token 保留进入起点和失败耗时，并记录最多4个 canonical 项目源码
上层 frame 及有限运行环境。耗时标为 wrapper 区间，包含调度与观测开销；
不称作纯 SQLite 等待，不声明 OS 锁所有权。保留成功静默、原异常、patch恢复、
每项1200字符/总24000字符上限，以及原60轮、六条断言、1ms压力与生产行为。

## 当前验收边界

最终冻结MCP文件SHA256为
`569b774b52f05cf4bd5f2c3f92f2df70037ecbae22b01877520302bae7acef5e`，
M2文件为
`f2200905cc0d644709c04af300e58abbc70222b6de890ce90b770e99c7a087ba`，
生产、CI、锁策略、重试及期限均未改变。

第二读者 `/root/review_admission_contract` 的正式结论为
`PASS_FROZEN_TWO_TEST_FILES_NARROW_CONTENT_ONLY`，计划范围新增阻断0。
独立整文件MCP **4 passed（1.43s）**、M2定向 **14 passed / 29 deselected（13.21s）**；
原MCP十条assert及两次replay、M2正常收尾源码/六条assert/60轮/1ms均精确保留。
独立73件运行源与c2cf基线逐件一致，不拿作者结果代替独立运行。

- 真实审批和unknown结算各被屏障延后2秒时，新测试均通过，真实调用各约225/224ms；
  保留unknown、无Receipt及两次replay不变断言。
- 恢复真实blocking PIPE后，原returned断言真红；主线程等到done已置位才返回wait时，
  monotonic实测2008697750ns仍被2秒duration断言拒绝。两项负控均为call失败，setup/teardown通过。
- 审批入口传输前注入错误，0次call，原error断言真红；清理后worker退出。
- 原accept失败清理缺口及中间9abd候选覆盖启动异常的问题均保留FAIL原件。
  最终569b把启动/accept放入清理结构，仅在已启动时join；真实启动与accept异常探针
  均保留原异常身份、关闭一次，并确认已启动worker和真实子进程在兜底清理前退出。
- M2真实结果提交的READONLY进入失败与180次真实事务淘汰ring后，仍保留7ms注入区间、
  最多4个canonical上层frame及运行环境。两项删功能变异各在call阶段真红；
  独立格式压力确认总24000字符及四个critical计时字段保留，无frame/异常对象留存。
  READONLY与7ms注入只验证诊断，不冒充原Windows BUSY复现。

完整[独立报告](evidence/windows-ci-timing-2026-11-14/independent/REPORT.md)及
[事实](evidence/windows-ci-timing-2026-11-14/independent/facts.json)的SHA256分别为
`7208ee60ddfad68e38734a5b506d829ed3761e0442bb51dc3ff6bbf88685c59c`、
`47809d9cf9512248683ac428012fae5794d7b48b40109eebd3ba665112be6ce4`。
原本机验证限macOS arm64；Windows junction分支未在本机执行。

root在最终569b/f220冻结上完成提交前门禁：`uv lock --check`、
`uv run --frozen pytest`、`uv run --frozen ruff check .`均exit0，
**3199 passed / 8 skipped，635.00s**（命令635.40s）。1163份Git跟踪或非忽略输入、HEAD、Git状态
前后完全相同；[结果](evidence/windows-ci-timing-2026-11-14/final-gates/results.json)、
[日志](evidence/windows-ci-timing-2026-11-14/final-gates/pytest.log)及输入清单均公开。
此前3d69候选的3199/8、611.04s完整门为历史证据，不覆盖后续清理修补。
UI源码、测试与dist无变化，本机未重复运行UI；新原生CI仍须执行全部job。

本文为提交前验收记录。门后只回填事实文档与原字节证据，并在`.gitignore`为本证据目录
的固定日志增加最窄例外，避免15份原日志被全局`*.log`漏掉；最终Git对象须独立绑定。
正式提交与新原生CI以本轮PR正文及对应run为准。当前不能宣称Windows已修或根因已建立。
#145、#147 继续开放；恢复B本地对象保存，契约推送及产品实施继续暂停。
原件保存在忽略的本地档案；[公开子集索引](evidence/windows-ci-timing-2026-11-14/subset-index.json)
明确列出73份原字节材料及来源，不冒称完整封存包；独立报告描述的是本地76件包，
并非公开子集。脚本按原字节移名`.py.txt`，原文件名和路径由索引映射。关键入口为
[main独立FAIL](evidence/windows-ci-timing-2026-11-14/main-failure/REPORT.md)、
[M2定位](evidence/windows-ci-timing-2026-11-14/m2-phase1/REPORT.md)、
[MCP真实屏障](evidence/windows-ci-timing-2026-11-14/mcp-phase1/REPORT.md)。
