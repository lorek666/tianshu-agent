# 879 固定候选：AUTH / 真实任务 / 父版本 fence 窄诊断

2026-11-14；`/root/review_low_disk_integration`。**只读诊断，不是批准或新 delta 复审。**

输入是已封存 879 项副本，清单 SHA256：
`edcef0a5cd71a1320338d20590a3e74c0540cbcc6505fa585754fb6427947441`。
首 full 原日志 SHA256：
`de7e3308456792005ceb5f963d0cee7fa080b750f6bc510a06064d1fea372df3`，
真实结果 41 failed / 3141 passed / 8 skipped；前后 map 均等于 879 清单。本轮只解释
AUTH 12F、eval 4F，以及 root 后续指定的 parent fence 1F；未重跑原文件、full 或既有矩阵。

## 判断

**不能将这组失败整体称为“旧审计断言过时”。**

1. **10 个 AUTH Worker 失败 + 4 个 eval 失败：同一上游过滤改变原执行审计路径。**
   `loop.py:1873–1897` 在合法工具/schema 通过后，把 pure_only 的非 pure 提案直接
   改成 `tool.not_dispatched`；没有进入 Dispatcher，原 `tool.result=rejected` 与
   `dispatch.rejected`（记忆 `c4.memory_scope_denied`、文件只读与禁止外发）不会产生。
   因而 Worker 在“result 恰1条”处失败，RU-D1 缺两种真实 C4 拒绝，安全判据变 unsafe，
   负控的原样正控也先红。当前没有观察到成功写入，但不能只把断言改成接受任意未派发。

2. **parent fence 证明它还影响控制/费用行为，不能仅按事件名称迁移处理。**
   父目标修订后，写提案在进入 `_authority` 的父版本检查前被过滤；最小探针得到 child
   **succeeded、模型2调用**，而原要求为 **cancelled、模型1调用、dispatch.parent_fenced**。
   本例收尾仍把 subtask 置 fenced、summary/result_ref 为空、父无 subtask.finished，
   没有证据表明旧结果被回填；具体回归是派发前 fence/安全点取消和额外模型调用。

3. **另2个 MemoryExecutor 失败：外层新增防御使旧夹具提前拒绝，属于可接受加固。**
   `_begin_memory_execution` 相对 a022 只新增 `_authority` 和 `_check_run_execution`
   两行，且都在 execution.started 前。正常夹具先抛 `DispatchDenied(c4.memory_scope_denied)`，
   根本到不了旧测试内层的 `pytest.raises(MemoryDispatchRefused)`。
   原 AUTH 独立下层并未被删：a022 与 879 的 `MemoryExecutor._verify` 函数逐字节一致，
   SHA256 `d39e4f56704e7bdafba95cc630bdba67151cf8d128801fafa7c780ceb8db5c94`。
   不能为了旧夹具删掉这两行，也不能把“外层已经拒绝”冒充“内层独立核验通过”。

T2 契约 §4.4/7/8 要求保留子任务原权限、父身份/作用域及执行器独立检查；pure manifest
仅收窄模型清单，不能替代这些执行侧不变量。原 AUTH01 已合修复明确要求 Dispatcher 和
MemoryExecutor 独立核真实 child 身份，拒绝写及嵌套审批。

## 最窄修复建议与必须保留的断言

- **产品只删除/调整上述冗余提案前置过滤**，保留 `_run_tool_manifest` 的 pure 投影。
  语法/schema 合法提案仍走原 Dispatcher authority → parent fence → C4 审计 → policy 门；
  Dispatcher / begin_execution / MemoryExecutor 的资格、空间和身份守卫均保留。
  无效参数、内部未暴露工具等原有 A7 `tool.not_dispatched` 路径不动，不伪造 C4 拒绝事件。
- **AUTH 的10个 Worker 断言保留**：真实 rejected/none_applied、具体 memory rule、零 intent /
  审批、记忆数量与原对象不变、子不 waiting、父 pending；root 可写与 child 可读正控保留。
- **eval 的真实拒绝和负控保留**：文件/外发两种 C4 拒绝实际发生；子无审批、无 applied /
  非纯读成功、run.end 非 applied；只读原样正控先绿，注入成功写再红。不要把 checker
  改成“任何 not_dispatched 都算安全”或改脚本自述来制造通过。
- **fence 断言保留**：先命中 dispatch.parent_fenced、cancelled、1次模型；subtask fenced、
  无summary/result_ref/父 finished；已发生模型调用照实结算，subtask 预占释放而非退款。
- **只适配2个内层夹具**：真实 request/grant/dispatch/intent，取得真实 execution_guard 与
  execution.started/ExecutionPermit 时，仅故障注入上游 `_authority`、`_check_run_execution`；
  调用 executor 前恢复上游方法，严格断言内层 `MemoryDispatchRefused` 的 child rule/原因，
  且无记忆落地。不能手造 permit、删生产守卫、只放宽异常类别或只在外层断言拒绝。

## 本轮最小独立材料

`probes/current_path_probe.py` 仅在未改的879源上运行3个离线场景，命令 exit0：

- 一个 child 的合法 memory.propose 与 create_report 只落两条 not_dispatched，0 result /
  dispatch.rejected / intent / approval，mem0、无文件，直接说明审计路径变化。
- 父改目标后一个写提案复现 succeeded/2调用；同时核无结果回填，不把范围夸大。
- 一个真实 legacy admitted 夹具：正常 begin 在 execution.started 前拒绝；仅上游两门
  故障注入后获得真实 permit，原 MemoryExecutor 仍独立抛 child拒绝，mem0。

这是3个诊断场景，**不是重跑原17项或宣布修复通过**。仅 ScriptedModel，无付费、HTTP/API、
CI 或进程并发探针。完整 argv/脚本hash/退出码/原输出见 `logs/current-path.command.json`、
`logs/current-path.stdout.txt` 和 `logs/current-path-results.json`；真实DB材料在 `runs/`。
基线函数差异见 `logs/auth-baseline-binding.json`。

等待原作者的新固定 delta 后再独立核：最小过滤改动身份、原 AUTH/eval/fence 行为恢复、
内层真实 permit 夹具覆盖，以及 pure manifest/执行资格拒绝仍有效。当前不对未收到的修复
提前批准，879文档证据 PASS 与本诊断分开保留。
