# MCP Phase1：观察窗跨越多个阶段；原 Windows 根因尚未确定

结论：精确 c2cf 源码及本机探针证明，原 `done.wait(2)` 可能同时等待 Run 装配、审批、真实 MCP 调用、unknown 落盘和 Run 收尾；它不能单独识别发送期限是否失守。两个有界真实屏障均可在真实 `_send` 仍抛出 `McpTimeout` 的情况下触发原第 163 行 `returned=False`。这两种人为充分机制不构成原 Windows CI 根因的证明；原故障阶段仍为 `NOT_ESTABLISHED`。

## 身份与范围

- 源提交：`c2cf1c71c65f46dea161e41b140df99d137a5686`。
- Git tree：`35bc297ce3125be4fd8db6fc4087196507a94045`。
- 精确归档：`source.tar`，SHA256 `99d2709d82aa513d85ff21d55411b7e3d43edfc0adc7097905d77686d0cfe02e`。
- 原失败：run `43576167008` attempt 1，Windows shard1 job `132390772345`，`tests/test_mcp_transport_timeout.py:163`。原日志 SHA256 `36fa0f28dbfea5fe207a2ca627926adef3580bb94a9325a2b4b24795550ed475` 保持不变。
- 本机环境：macOS 26.6.2 arm64、Python 3.12.13、SQLite 3.50.4。只运行一次 observe、一次 gate-approval、一次 gate-unknown；没有原 Windows 运行时的阶段观测。
- 原归档 1143 文件 hash 全部不变。live 仍 clean，HEAD `a079455ee5aac0b4458cf8166511ab8abd9f79e6`。没有改 live、产品、测试、2 秒观察值或 200ms MCP 配置，没有 CI 重跑、付费调用或提交推送。

## 源码已经证明的事实

1. `_server_config` 的子进程在写完 `tools/list` 响应后发送 TCP `ready`，随后等待控制 socket；它没有等到审批或 `tools/call`。
2. `AgentLoop.start → Dispatcher.prepare_run → _prepare_new_run → McpClients.start_all` 在创建 Run、状态/cursor、模型、审批及 resume 前启动 MCP。
3. 原 worker 的 `done.set()` 位于整个 `engine.start → approve_pending → engine.resume_run → log.close` 之后。主线程在 `ready` 后等待该 done；调度先后没有额外屏障保证，故不能断言每次都从某一完整业务阶段起算。
4. 原 call 8.84 秒包含 finally 中 `_release` 的 `worker.join(5)` 与 `clients.close_all()`，不能用总耗时反推原始卡点。

对应文件：`source/tests/test_mcp_transport_timeout.py`、`source/src/tianshu/loop.py`、`source/src/tianshu/dispatcher.py`、`source/src/tianshu/mcp.py`。原日志没有这些阶段边界或卡点栈。

## 三次单次探针

外部 pytest 插件 `boundary_plugin.py` 只包裹真实方法、记录 monotonic 元数据，委派原方法一次。原测试源码和断言保持原样，真实 MCP 仍走子进程 stdin/stdout，控制 socket 只作屏障/清理。每次都保留 `.run.json` 中的完整命令、cwd、平台、插件 hash 和日志 hash，以及原始 `.log` 与完整时间线 `.json`。

- **observe**：setup/call/teardown 全通过，`1 passed in 0.63s`。原等待开始后约 94.312ms 才进入批准，139.367ms 才进入 `call_tool`；真实 `_send` 在约 222.492ms 后抛 `McpTimeout`（含 `_fail` 约 22.131ms）；unknown 提交位于等待开始后 362.841–369.988ms，worker 在约 408.024ms 结束，`returned=True`。原 unknown、无 Receipt、两次 replay 事件不变断言全部执行并通过。
- **gate-approval**：在真实 `ApprovalStore.decide` 原方法调用前用 Event 屏障阻塞。原 2 秒自然到期时真实 `call_tool` 尚未进入；原 finally 释放诊断屏障后，真实 `_send` 在约 224.125ms 后抛 `McpTimeout`，随后完成 unknown 落盘。setup/teardown passed，call 仅在原第 163 行失败，`returned=False`。
- **gate-unknown**：真实 `_send` 在约 224.211ms 后已抛 `McpTimeout`；随后在 unknown `_commit_result` 原方法调用前用 Event 屏障阻塞。原 2 秒自然到期仍为 `returned=False`；释放后真实提交并退出。setup/teardown passed，call 仅在原第 163 行失败。

两类屏障均最多等待 15 秒；原 finally 触发后释放屏障，并有界等待 worker 10 秒，期间保持真实 server 不读 stdin，然后执行原 `_release`。没有通过 sleep 制造阶段时序。三个 worker 均结束，元数据 deque 丢失数均为 0。

两次刻意红的原 call 在第 163 行已停止，不能宣称其后原断言执行过。插件只读核对每次真实 DB 都有唯一 `tool.result = failed/unknown/mcp.timeout`，无 Receipt。额外将两个 DB 复制到 `replay-check/`，对副本执行真实 replay 两次；各 33 条事件均完全不变，结果在 `replay-check.json`。额外证据不把原 call 改成绿色。

## 尚未确定与不应推出的结论

- 原 Windows 2 秒内究竟卡在准备、审批、发送、`_fail`、unknown 落盘、收尾或调度，均未确定。
- 本机约 224ms 的真实 `_send` 结果只证明这三次运行的期限行为，不能证明原 Windows 同样如此。
- 未证明 SQLite 持锁、Windows 特有问题、flaky、既有基线问题，也未证明与其它历史失败同因。
- 原红与两类刻意红均保留；没有重试将它们解释成通过。

## 最窄设计建议（尚未实施，等 root 核证）

建议只调整这个测试的观测边界：在真实目标 `call_tool` 入口和退出（包含抛异常的 finally）设置独立的 started/finished 事件并记录 monotonic 时刻，包装器仍只调用原方法一次。2 秒预算应直接核对这段真实 transport 调用，而不是从工具发现的 `ready` 起等待整个 worker；若主线程收到 started 的调度较晚，入口/退出时间戳仍能校验实际区间。

审批与预备执行应另有明确、有界的到达断言；worker 收尾仍使用原 `_release`/`join(5)` 与 `close_all`，不能把 transport finished 冒充整个 Run 完成。必须保留原实际 DB 的唯一 unknown/mcp.timeout、无 Receipt、两次 replay 不变断言，也保留真实背压及 `call_timeout_ms=200`。这可以消除已证明的阶段混淆；它本身仍不定位原 Windows 失败，也不授权任何产品期限改动。

## 证据入口

- `facts.json`：紧凑身份、时刻、阶段结果、DB 与完整性数据。
- `index.json`：本轮小型证据文件的路径、长度与 SHA256；不重复打包 1143 个源文件。
- `observe.json` / `gate-approval.json` / `gate-unknown.json`：完整边界序列。
- 同名 `.log` / `.run.json`：原始 pytest 输出、准确命令与运行身份。
- `source-before.json`：精确归档逐文件 hash；`finish_phase1.py` 已复核 1143 文件无变化。
- `boundary_plugin.py` / `run_probe.py` / `finish_phase1.py`：可审查的外部观测与汇总实现。runner 会拒绝覆盖已有 mode 的证据；独立复现应使用新证据目录，不能覆盖这三次原件。
