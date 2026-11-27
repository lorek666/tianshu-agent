# 驱动作业与 Run 终态同提交

候选基线为 `9d5a02da23abda7a3b2ebf83ccdff26a83040a2b`。

父 Run 通过正常审批派子任务后等待。子任务已有已保存的终止回合时，旧驱动先提交
`job.finished`，再提交 Run 结论和委派结算。中途进程退出会留下一个不可由 Worker
自动认领的 `done` 作业，子 Run 仍是 `running`，父作业仍在等待。

现在正常驱动沿既有归属核验，在同一个 SQLite 事务提交作业和 Run 终态、子任务结算
与父作业唤醒。`run_worker_once` 接收通过独立连接打开的同库 `JobStore` 时，驱动把它的
身份和时钟绑定到 `TaskStore` 所在连接，沿用原认领与收尾逻辑。

终态事务中的 SQLite `OperationalError` 会一起回滚作业与 Run 的终态。低层驱动会报告
收尾写入错误，保留 Run 供同一实例显式续跑或由 Worker 接管。已保存普通终止回合时，
Worker 和 Daemon 在心跳收拢后交还仍在运行的作业，供下一轮复用回合继续收尾。
Worker 把写库错误交给调用者，Daemon 的驱动循环仍按既有连续失败门禁处理持续故障。
历史半终态的恢复，以及暂停、待批、对账时的收敛分别沿既有路径。

`tests/test_g2_delegation_exec.py::test_child_finish_is_one_transaction` 在现有预算结算
接缝调用故障注入器。子进程真实退出 `77` 后，旧代码留下 `done`，候选保留 `claimed`。
候选的后续 Worker 使用同一个作业 ID 和新的 claim epoch，复用已保存的终止回合，模型调用数
为零。Run 终结、委派终结和父作业唤醒各提交一次，父预算中的委派预占完成结算。

同文件的同库双连接用例核对了原作业身份和结束时钟。一次错误用例在父作业唤醒后
注入 `OperationalError`，分别走 Worker、Daemon 的续跑处理和低层 `resume_run`。
三条路径都先保留非终态及预算预占，再使用已有终止回合完成收尾，恢复时模型调用数为零。
原冻结树 `e918a0a` 在对应回归用例上失败。离线注入的模型错误负控仍以 `failed` 收尾。

既有限制不由本候选修复，冻结基线与候选均重现：外部 `JobStore` 若使用与 `Stack`
不同的实例 ID，`TaskStore._assert_writer` 仍会误判接管；Worker 认领后停止推进，子 Run
停在 `created`、子作业等待、父 Run 等待。同库双连接结论仅限同一实例身份，注入时钟可以
不同。暂停或取消若在最后安全点之后、终态事务之前才写入，终态提交仍可能清掉这个请求，
子 Run 以 `succeeded` 收尾；该控制竞态尚无远端工单。

本地定向用例、受影响域回归、锁文件与 Ruff、Tier 1 本机裸全量、新树独立复审及主控
replay 已验证。远端 required CI 仍须绑定最终交付候选。
