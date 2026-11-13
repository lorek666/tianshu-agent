# 系统 debug：执行边界与恢复原子性

日期：2026-11-13。受影响基准：`ccff93e79cfeb93e47e5a35012fd92fae366e941`。
本报告是作者的施工证据，不构成独立批准。完整修复 SHA、第二读者亲跑结论与四项 CI 在对应 PR 正文绑定。本机环境为 macOS 26.6.2 arm64 / Python3.12.13，所有数据库、命令产物及故障注入均在临时目录。

## 实际执行路径及覆盖

`CLI/Control → Worker/AgentLoop → Dispatcher.prepare → _authority(claim/run/revision/lease/resource) → PolicyEngine/ApprovalStore → dispatch（审批消费、grant、intent、dispatch 同事务）→ TaskStore.begin_execution 执行守卫 → FileExecutor._verify → mark executing → handler → tool.result+Receipt → recovery/assess/takeover`。

| 入口/模块 | 关键不变量 | 证据 | 本轮结论/缺口 |
|---|---|---|---|
| Dispatcher / FileExecutor | 真实执行侧有票据、摘要、claim、lease 的二次验证 | `test_dispatch.py` direct_execute_requires_admission / prepare_rejects_tampered_args / old_claim_after_takeover / foreign_instance / expired_grant；源码追到 `_verify` | 定向基线通过；不是只依据模型输出准入 |
| ApprovalStore / GrantStore | 批准绑定具体摘要，一次消费、期限及撤销有效 | `test_permissions.py` digest_binds_every_authority_field / consume once / other digest/subject / expiry；`test_dispatch.py::test_concurrent_approval_consumes_once` 为真子进程竞争 | 已有真并发证据通过；未新造“同进程两连接”替身 |
| 路径/租约 | symlink 归一化，父子路径互斥，过期持有者不能越过守卫 | `test_leases.py` symlink_alias_conflicts / parent_child / injected clock / two_processes_one_resource / hard_exit guard release | 已有证据通过；未把 Workspace 当物理沙箱 |
| claim/fence | 老 claim 不能继续写 cursor、turn、dispatch | `test_job_claims.py` epoch fence / live_worker_loses_claim_to_another_process；`test_takeover.py` SIGKILL/接管 | 相关基线通过 |
| EventLog / stores | 事件 append-only，事件与投影同事务，可组合不嵌套 BEGIN | `test_transactions.py` rollback / composable / failed_nested_store；源码读 `transaction`、`append_many` | 现有路径通过；未确认“内层异常被外层吞后仍提交”真实生产链，因此不报 bug |
| 文件恢复 | intent→preimage→write→result+Receipt，三态与重复恢复 | `test_recovery.py` 真实 crashpoint；`test_crash_consistency.py`；`test_undo_recovery.py` | 相关回归通过 |
| 命令恢复/接管 | 已进入执行边界且无结果的命令不能判 none_applied | 新 `test_exec_recovery.py`，真实审批、真实 Python 命令、真实硬退出77 | 确认 AB-01/P1，修复并回归 |
| 恢复自身二次崩溃 | unknown 结果与本地目录/对象阻断不得只落一半 | 新 `test_recovery_atomicity.py`；真实 EventLog 写入后调用 `failpoint` 硬退 | 确认 AB-02/P1，修复并回归 |
| authority 跨库 | 本地已阻断、跨根写失败不能冒充成功，重启补偿有效 | `test_authority_block.py` + 新 before_authority 故障注入（文件/MCP） | 本地原子，authority 提交后补登记；跨库不宣称原子 |
| MCP / memory / subtask | MCP 发外边界；memory erase 独立 unknown；子任务不误挡目录 | `test_mcp_crash.py` / `test_mcp_object_block.py` / `test_memory_dispatch.py` / `test_g2_delegation_recovery.py` | 联动回归通过；未改变各域契约 |
| Host/IPC 与跨用户 | 同用户边界而非物理隔离 | 分配给执行器/IPC审计代理 | 本A/B报告不声称独立验证了Windows/IPC全部路径 |

## AB-01：P1，命令已执行却被恢复为 none_applied

**受影响基准**：上述 SHA，`recovery.py::assess_run/_recover_run`。

**触发条件**：真实 `run_command` 已执行，但 `tool.result` 尚未提交时进程崩溃。命令不会生成单文件快照条目，旧逻辑却套用“清单为空即未执行”。

**契约与影响**：PRD 红线5/11要求未知副作用不得误判为未执行或安全重放。基准实际 `effect.txt` 已写入 `ACTUALLY EXECUTED`，`assess_run.blocks_takeover=False`，`recover_run.none_applied=['exec-crash']`、`ok=True`。

**最小原始探针**：

```sh
uv run --frozen python /tmp/tianshu-debug-20261113/audit-ab/exec_recovery_probe.py
```

证据：`/tmp/tianshu-debug-20261113/ab-exec-recovery-red.log`。内部子进程 crashpoint 退出77，外层因上述真实断言失败退出1。

**正式回归**：`tests/test_exec_recovery.py` 13例：

- after_intent / after_write × 空/缺失/损坏清单；用真实文件字节区分是否执行。
- after_result / after_receipt 已结算不重分类、不再执行，保留1条结果+1条Receipt。
- 无派发行、旧 boundary_protocol、未知派发状态均保守 unknown。
- 多 workspace 恢复参数不能把阻断移到无关目录。
- 同 call_id 的无关文件快照不得替代命令派发行的工作区证据。
- 重复 recover 不增加事件、不重复执行。

**修复**：只接受带持久化执行前边界协议、状态仍在 intent/revoked/rejected 的派发行作为“尚未启动”的证据。executing、缺记录、旧协议或未知状态均 unknown 并阻断；不编造Receipt。命令阻断按派发时 `workspace_root`，不采用文件快照条目。

**残余边界**：命令允许的可执行程序可以对工作区之外产生影响；`LocalExecBackend.isolation='none'` 不变。恢复只能拒绝安全重放并要求人工核对，不能自动确定任意子进程的全部副作用、撤销命令或替用户完成外部系统对账。

## AB-02：P1，恢复二次崩溃永久漏掉阻断

**受影响基准**：上述 SHA，`recovery.py::_settle/_recover_run/_settle_mcp_pending`。

**触发条件**：恢复写入 `tool.result(effect_state=unknown)` 后、稍后写 `recovery.blocked` 前再次崩溃。下一次恢复用“已有result”过滤pending，导致 `ok=True`、无阻断。

**契约与影响**：未知副作用必须阻止后续写入；不能先把调用当已结算，再丢失其必需的阻断事实。

**最小原始探针**：

```sh
uv run --frozen python /tmp/tianshu-debug-20261113/audit-ab/recovery_interrupted_probe.py
```

有效红证据：`/tmp/tianshu-debug-20261113/ab-recovery-interrupted-red-confirmed.log`。两次早期脚本的C3夹具错误不计作bug证据。

**故障注入边界如实说明**：这里包装真实 `log.append_many`，先执行原方法，再调用项目已有 `failpoint('after_result')`→`os._exit(77)`；它是测试边界注入，不伪称 recovery 原生产路径已存在该 checkpoint。真实SQLite提交/回滚与重启均未替换。

**正式回归**：`tests/test_recovery_atomicity.py` 10例：

- 文件/MCP × 缺失/损坏清单 × after_result/before_authority，共8例。
- unknown结果、目录阻断、MCP对象阻断同生同死；前一种注入回滚整个本地短事务；后一种已提交本地事实但未写authority，下一次恢复完成补登记。
- 文件/MCP既有孤立unknown结果2例：升级恢复追加缺失阻断，不改写原结果；重复恢复幂等；后续人工unblock/unblock_object不被复活。

**修复**：

1. `_settle` 在同一短事务提交结果、所需本地目录/对象阻断与dispatch终态。
2. 每个unknown分支在调用 `_settle` 前给出阻断归属；不再统一在整轮恢复末尾才写block。
3. authority写在本地事务提交后，失败报告problem；崩溃后现有mirror补登记。
4. 补偿旧版孤立 recovered unknown：不改结果，只追加缺少的block；历史对应call_id已经有block，或结果之后有人明确unblock目标时不复活。

**为什么 source diff 约370行并非无关重构**：净增约134行。约80行来自删除 `_settle` 分散早退分支、收敛成统一短事务；约95行是经授权的旧数据补偿；约50行是exec专用判据/结算；其余是10余个unknown调用点传递root、MCP对象同批提交及单调用归属替代原“最终聚合block”。没有扩大事务覆盖磁盘观测、孤儿blob清理、authority或其他外部IO。

**残余边界**：主事件库与authority是两个SQLite库，仍不跨库原子。若进程在本地提交后、authority登记前死亡，跨数据根全局阻断要等该数据根再次恢复/启动mirror后可见；本数据根已阻断，不能把此窗口声称为消除。完全没有可归属的历史intent/dispatch/snapshot时不能重建原动作的所有目标；报告problem或按显式恢复工作区保守阻断，不声称精确核实。

## 命令与原始证据索引

均位于 `/tmp/tianshu-debug-20261113/`，同名 `.json` 记录退出码和环境：

| 标签 | 命令/结果 |
|---|---|
| `ab-existing-invariants` | permissions / dispatch / leases / job_claims / failclosed / transactions / takeover / authority_block / undo_recovery：183 passed，8.38s，exit0 |
| `ab-exec-recovery-red` | AB01原始探针：真实执行后误判none_applied，exit1 |
| `ab-recovery-interrupted-red-confirmed` | AB02原始探针：已有unknown却无block，exit1 |
| `ab-regression-red-valid` | 原生产代码的正式新增回归：9 failed、5 passed，exit1；失败均为所述行为断言 |
| `ab-regression-green` | 第一轮14新增回归通过，exit0；作用域ruff通过 |
| `ab-history-red` | 原先仅防新窗口的修复对旧数据补偿：2 failed、8 passed，exit1；证明旧孤立结果用例区分缺失补偿 |
| `ab-manifest-red` | 无关同call_id快照能误导命令阻断归属，真实断言失败exit1；随后禁止exec使用文件快照归属 |
| `ab-recovery-targeted` | 文件/MCP/记忆/子任务/撤销/接管/authority等236 passed，21.78s，exit0 |
| `ab-final-targeted` | 最终三文件固定后的关联回归：237 passed，22.46s，exit0；scope ruff与diff-check通过 |

正式执行命令（可从任意本项目工作树运行）：

```sh
uv run --frozen pytest tests/test_exec_recovery.py tests/test_recovery_atomicity.py -o addopts= -q -rs
uv run --frozen pytest tests/test_exec_recovery.py tests/test_recovery_atomicity.py tests/test_recovery.py tests/test_crash_consistency.py tests/test_mcp_crash.py tests/test_mcp_object_block.py tests/test_memory_dispatch.py tests/test_g2_delegation_recovery.py tests/test_undo_recovery.py tests/test_failclosed.py tests/test_takeover.py tests/test_authority_block.py tests/test_debug_regressions.py -o addopts= -q -rs
uv run --frozen ruff check src/tianshu/recovery.py tests/test_exec_recovery.py tests/test_recovery_atomicity.py
git diff --check -- src/tianshu/recovery.py tests/test_exec_recovery.py tests/test_recovery_atomicity.py
```

## 未确认与排除项

- `EventLog.event_versions` 被物理删除后可能与真实legacy数据库难区分：只读源码发现，未形成可靠真实版本迁移复现，不算确认缺陷；未修改。
- 可组合事务的内层异常被外层人为吞掉可能提交部分状态：未找到生产调用链，不把抽象可疑点报为bug；未修改。
- prepare请求投影与事件潜在提交缝：未形成用户影响证据，不报确认缺陷。
- `net.fetch` 成功/失败按 SPEC-G2-NETWORK §2.2 均是none_applied，外发成本另记；经父代理/D-E域对账，不把它混同于exec缺陷或改变网络契约。
- 同uid、路径约束、无物理沙箱是已声明限制，不写成“已解决”。
- `docs/exec-run-command.md` 仍写CI只有macOS+Ubuntu；已交主代理负责纠正文档并追加恢复说明。


最终分域关联结果为 **237 passed / 22.46s**，Ruff与diff-check通过。完整项目门禁与当前提交云端结果独立记录，不累加重叠测试组。
