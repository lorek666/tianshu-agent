# 手动压缩上下文与计划模式

日期：2026-11-21。状态：实现候选；尚未合入主干或发布。

## 原因与实现

用户需要主动压缩长上下文，以及先审阅计划、明确确认后再执行的入口。
原有确定性裁剪、内部自动摘要和只读资格分别保留，新增工作流复用既有
Task/Run、预算、作业队列、执行侧授权与事件回放。

- 桌面识别 `/compact`、`/plan`、`/execute`；有计划/执行选择、可读计划正文和确认按钮。
- CLI 提供 `run plan|compact|execute-plan`，复用控制面的原子入队与幂等校验。
- `run.workflow` v1 保存来源材料，旧 `run.start` 版本及旧 Run 回放含义保持不变。
- 计划强制只读；压缩强制无工具；确认计划新建 `effects` Run，工具审批照常进行。
- 结果落库后崩溃直接消费；未知模型结果不重发；压缩失败保留源事件与旧成功摘要。

契约单独提交于 `2c62128`，见[工作流契约](../compact-plan-contract.md)。
随后仅同步 main `53424e3e91f0d97732be8ff262baedb57313fe9a` 的模型用量契约文档，
契约分支 merge 为 `34777d1b1872db1dbd04a65fe1728c2dc3e3dc58`。PRD 同位置新增的
两项独立决定均保留；相对已审冻结对象仅增加文档，没有非文档代码或测试变化。
最终提交前再次同步 main `71eb57ef1544d126cc929b9172f5463a14999359` 的 Responses
续接契约文档，契约分支 merge 为 `93ba90e89378a9aec37c1b7b626761f5611eb904`。
本次也仅涉及文档；运行时代码未增加 Responses 支持，不借契约同步扩大本轮交付范围。

## 验证与证据绑定

原工作流代码/测试冻结树为 `bd5672298f5282bfe82b15418d76648fe7da4aaa`。
最终全量发现 IPC P1 后重新打开，修复候选冻结树为
`d161e060ceefe240a930bc5cbef8d775ca8eb4b6`。其子树为：

- `src`：`89f02d55c02911da08606b7988790dc6dddfb4b4`
- `tests`：`5c5c6269d2573230ab5fd9797b836f985a7b9ba1`
- `schemas`：`3773f5617a944bde97a9e42e0e766d096ff6dcc8`
- `ui`（含 dist）：`918e8ad439a37d9841049c16bd07aad1801a655d`

定向工作流/CLI/控制面/C3/上下文回归、`uv lock --check`、
`uv run --frozen ruff check .`，以及 UI frozen install、类型、全套测试和构建已通过。
独立第二读者已完成真实 adapter、执行器、双进程屏障、硬退出恢复与负控复审；
IPC 增量也已完成独立红绿、恢复旧读取行为的负控与受影响域回归，
非文档代码树限定范围 PASS，见[独立报告](../REVIEW-COMPACT-PLAN-2026-11-21.md)。

复审发现并修复：原 Run 合法撤销使旧来源和全局 state 失效；压缩后丢失计划模式；
启动失败重试丢模式；不可确认的计划误记成功。内存演示也已改为明确拒绝不支持的工作流。
完整套件另发现 G2 镜像测试仍冻结旧事件类型总数，修正期望值后，同域红绿已验证，
逐项 schema/enum/payload 一致性检查保持不变。

原冻结对象的首次最终完整 pytest 已运行结束，exit 1：
`test_measure_slo_smoke_and_run_soak_real_subprocesses` 在 suite2 游标重连的认证握手阶段
收到 EOF / `ConnectionClosed`。同树未改代码的定向复跑 exit 0，但不计为修复或全量通过；
独立探针进一步确认 P1：旧 reader 在 poll/取 fd/read 窗口挂起，真实 abort 关闭旧 fd，
accept 自然复用后，旧 read 截走新连接认证帧头 `00000028`，新客户端在同一
`answer_challenge` 路径 EOF。先完成旧 read 再 accept 的正控可连接。
首次全量现场已被测试 finally 清理，不能断言那次 EOF 必由此机制触发。

最小修复仅将二次 closed 检查、取 fd 与单次 read 纳入已有状态锁，不持锁等待 poll，
不改 Windows pipe 路径或原有期限。正式 `tests/test_ipc_fd_lifecycle.py` 已在旧实现
真实见红、修复后转绿；IPC/Daemon/SLO 受影响域 exit 0。探针曾错误要求 EOF 不留下
空字节读取记录，现仅禁止非空消费，仍强制新客户端完成认证、无 accept 错误和线程收尾。
新树的最终 `uv run --frozen pytest` 已完整结束，exit 0；
`uv lock --check`、`uv run --frozen ruff check .` 与 diff 检查也通过。
后续两次 main 同步均只增加文档，非文档代码树与该验证对象一致，不重跑相同代码的全量。
原始完整日志、定向复跑、探针错误、此前中断套件与定向红测均保留于本地忽略目录
`.omc/artifacts/compact-plan/`，没有用新绿覆盖旧红。
契约 PR [#207](https://github.com/lorek666/tianshu-agent/pull/207) 与实现 PR
[#208](https://github.com/lorek666/tianshu-agent/pull/208) 已分别建立。
远端 required CI 的旧契约候选 run `44858922013` 已全部 success，但不代替后续候选结果。
两个 PR 的最终 head/base、合成 merge、CI run 与完整结果以对应 PR 正文及 Actions 记录为准。

## 边界

压缩仅限空闲任务，材料预算 64 KiB、摘要上限 8 KiB UTF-8；摘要质量及费用收益不作保证。
计划不允许非 pure 工具。工作流用真实执行器和可控模型传输替身验收，未使用真实付费模型。
用户设备体验及远端跨平台 CI 状态不得从本机结果推断。
