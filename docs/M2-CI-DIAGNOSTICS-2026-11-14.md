# 正常收尾用例：SQLite 锁错误诊断

2026-11-14。原 CI 的实际抛错边界、持锁者和触发条件仍为 **NOT_ESTABLISHED**。
本片先保留失败现场，不宣称修复 Windows 或心跳逻辑。

## 原生失败

审批诊断 PR #148 已正常保护合入 main
`b450c339d546ae2007ed9754e70de0108c4b86c4`。该 main 自身首次 push
[run43567298082 / attempt1](https://github.com/lorek666/tianshu-agent/actions/runs/43567298082)
为 failure；Windows shard1 的
`tests/test_m2_acceptance.py::test_normal_finish_and_pause_do_not_report_heartbeat_failure`
在第55轮 call 失败，setup/teardown 均通过。Run 实际 queued、期望 succeeded；
`worker.errors=['OperationalError: database is locked']`，无 run.end。Windows 合计
3156 passed / 1 failed / 37 skipped，3194个节点、9549条阶段报告完整对账。
Windows required 汇总正确拒绝上游失败，其余四个 job 成功。
原件身份和检查范围见[main独立失败报告](evidence/m2-ci-2026-11-14/main-failure-independent-report.md)。

本次审批节点三阶段均通过，没有进入 M5 审批失败诊断。PR148 自身原生 PR CI 绿
不能覆盖这轮 main 红；审批问题 #147 及旧正常收尾问题 #145 仍开放，未证明同因。
没有取消或重跑原 run，不能将其归为 flaky、Windows 专属或基线缺陷。

## 已证实的机制与尚缺的证据

在未改 b450 产品的隔离副本中，以真实心跳事务和确定性屏障持锁，分别在以下位置
得到原生 SQLITE_BUSY(5)，均产生相同的五事件、queued 与 job.parked(reason=queued)：

1. `AgentLoop.resume_run → _acquire_leases → LeaseStore.acquire → transaction BEGIN`。
2. 租约真实获取成功后，`resume_run → TaskStore.transition(running) → transaction BEGIN`。

无 hold 正控成功、errors 为空。人为持锁超过原有5000ms等待只是充分条件实验，
**不是 Windows 原红根因复现**。实验是同进程真实主线程与心跳线程、两条真实连接，
没有冒称两个进程。macOS arm64 / Python3.12.13 / SQLite3.50.4 的结果不能外推
Windows 用户设备、ARM64 或安装体验。
调用帧、原生码及无 hold 正控见[隔离调查报告](evidence/m2-ci-2026-11-14/mechanism-investigation-report.md)
和对应原字节探针/输出；它们不是当前诊断改动的独立验收。

`Worker.run_once` 会把 `_drive` 异常缩成类型和字符串；`_drive` 异常退出还会跳过
beat 汇总。因此仅在最后断言失败时读 errors、beats 和当前投影不足以定位。

## 本片实施范围与边界

仅在原用例作用域观察真实 `_drive`、`_SideChannel.beat`、`EventLog.transaction`，
成功不输出，失败才附有界诊断。保留原60轮、六条断言、夹具、时限、重试及生产代码。
异常只采集类型、SQLite code/name、file/line/function，不采集消息、locals、源码、
SQL文本/参数、环境或正文。事务记录 attempt/acquired/body-return/exit 元数据，真实嵌套进入标为 reuse，
不增加 active 写者；
不能声称知道精确 COMMIT 开始时刻、OS锁 owner 或未观测的其他进程。
测试包装本身会增加少量执行成本、影响线程排程；不能承诺零时序扰动或必然复现原CI。

本机门禁、独立内容验收、正式对象与原生 CI 分别绑定实际结果，不能提前合并为
“根因已修”。恢复 B 本地同步提交 `277e82e265b54e0e8fe1b2fc1a99d747e95306d5`
已通过对象身份核验但尚未推送；恢复产品 apply/review/resume/abort 全部未实施。
[本地对象报告](evidence/m2-ci-2026-11-14/restore-local-object-report.md)只确认既有对象身份。

[公开子集索引](evidence/m2-ci-2026-11-14/subset-index.json)逐件记录来源、字节数与
SHA256；它不是完整封存包，历史脚本以 `.py.txt` 保存原字节。


## 内容验收与本机完整门（提交前）

首候选 `9eb5ced6d9040aa593c3387bd29cd606faf0e115232017aa73dc6bdc1ee3eadc`
在1112文件固定输入上 lock/full/ruff 均 exit0，3196 passed / 8 skipped
（537.43s），运行前后源、HEAD与状态不变。但第二读者实际发现合法
`/tmp → /private/tmp` 路径别名下 caller 丢失、源码帧被归 external，
新增 caller 节点出现1 failed / 10 passed；仅统一 canonical 导入的单节点绿
不抵消首红。旧结论仍为
[FAIL_CONTENT_CANDIDATE_PATH_ALIAS_OPEN](evidence/m2-ci-2026-11-14/content-v1-fail-report.md)。

最终测试源 `766a3496526594009638739bf093fd190d76d4adcd16c61acdf405504a0a4161`
仅在两处 frame 路径比较前 resolve，并新增两参数真实 symlink/junction 回归。
作者修前原源码分支 call 真红、外部副本绿；原用例源码、完整 body AST、六条断言
及60轮保持不变。第二读者在1112源固定副本确认其余1111逐hash继承，独立窄测
3 passed；仅恢复旧两方法负控2 failed / 1 passed，仅恢复旧frames负控
1 failed / 1 passed。独立真实别名探针确认根内恢复实际caller/文件，
根外同字节副本仍为 caller=None、帧external，所测附注/结构数据不泄漏绝对路径
或路径canary。最终结论为
[PASS_CONTENT_V2_NARROW_DELTA_WITH_INHERITED_BOUNDED_EVIDENCE](evidence/m2-ci-2026-11-14/content-v2-report.md)。
旧有界语义、两个真实heartbeat BEGIN及无hold/drive负控按精确同源继承，
不是本轮重跑；审阅脚本的oracle、目录及staticmethod预检错误都单列保留。

最终1112源完整门：`uv lock --check`、`uv run --frozen pytest`、
`uv run --frozen ruff check .` 均 exit0；**3198 passed / 8 skipped（546.63s）**，
命令耗时546.95s，全部源码、HEAD与Git状态前后稳定。输入manifest
`eb856c2981c40af0c841ee15fe1b7b291277630fee9847fbc618ea095798a919`；
[原始门禁结果](evidence/m2-ci-2026-11-14/local-v2-results.json)及日志由公开索引绑定。
门后只回填本节、知识库事实及原字节证据，产品/测试未再改。UI未变，不重复本机UI；
远端原生CI仍执行完整UI门。Windows junction本机未执行，必须由新原生CI另核。

本次内容及本机通过只批准测试侧观测范围，不证明原CI锁错误、#145或#147已修，
不代表最终Git对象、Windows或PR/main的原生CI通过。后续正式SHA及CI身份在PR
交付记录单独绑定，恢复B仍暂停。
