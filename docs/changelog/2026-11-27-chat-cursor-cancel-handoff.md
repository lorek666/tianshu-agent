# 缺游标 Chat 的取消交接竞态

普通 Chat 丢失 `run_cursors` 后，Worker 已不能回到模型循环安全点。若 Daemon 的取消请求
先于 `claims.park` 到达，旧路径会先挂起作业，再把取消留给不会到来的执行者，留下
`running + waiting + cancel` 且没有 `run.end`。

- `JobStore.park(defer_to_cancel=True)` 在同一写事务内复核取消位；取消先到时保留当前
  claim，不写 `job.parked`。该分支显式校验 claim 与作业存在，不用类型忽略掩盖空值。
- Worker 仅对缺游标路径启用该交接，并在保留 claim 后调用执行侧 `confirm_control`。
  取消核对、`run.end`、作业终结与租约交还仍在同一事务；失败整体回滚。
- 缺游标取消从持久化模型、工具与 Receipt 证据重算效果。带来源的 execute 工作流与普通
  Chat 使用同一核清；`unknown`、未结算派发、缺少 Receipt 的 applied 结果继续阻断并保留
  现场，已知 applied 不重发模型请求。
- 游标缺失后的 `run.end` 同时汇总已持久化摘要调用。已有游标时继续使用预算账本，避免
  重复计费；缺失 usage/cost 保持未知，不记成零。
- UI 在取消待确认时保留禁用的“取消中…”入口、警告与恢复说明，终态后移除；对应
  `ui/dist` 已重建。

旧竞态测试的屏障没有到达，是因为夹具用错误的 blob 目录打开事件库，Worker 在进入
`_settle` 前已因 Mission 快照缺失退出；该红灯无效。有效负控在冻结副本撤销生产交接修复后，
真实 Daemon 取消先到、屏障放行 park 的探针稳定非零，并复现上述状态。定向回归使用真实
Worker/Daemon 子进程与 stdin 屏障，覆盖取消先到、park 先到、unknown 阻断和 `run.end`
写失败回滚。

- Responses 可从已持久化模型结果派生 resume 游标，但派生游标不代表副作用已核清。
  取消请求与 Worker 安全点的 `confirm_control` 都从持久化 intent、result、Receipt 和
  block 证据重算效果，不能因游标重新出现而绕过核清。
- 已挂起且缺游标的 Run 才在同一事务内核清后就地终结；有有效游标的挂起父 Run 保留
  `running + control_requested=cancel`，继续向子任务传播并等待 Worker 安全点交接。
  游标存在性只查询 SQLite 行，不加载或水合游标内容。
- Windows 的中文测试夹具与子进程 IPC 显式使用 UTF-8；超时用例继续断言“超时不重试”，
  不把重试或重跑当成修复。

最终代码树 `ca3881b` 的本机裸全量已通过，原始日志与隔离环境记录随候选验证证据保留。
独立第二读者以 `483d9c9..ca3881b` 的精确增量完成复审，结论为“已修”；Responses 直接绕过
核清与 parked 提前终结两类负控均有效复现后转绿。GitHub required CI 由主控后续绑定。
