# Mission / MCP 生产接缝修复（2026-11-22，本地候选）

## 背景与根因

direct 和桌面入队入口曾在真实 MCP 发现前，用旧 registry 固定
`run.start.tools` 与 Mission 的有效工具。后续即使真实 server 提供同名工具，
模型清单仍被空交集过滤。共享 Mission 又把 Session generation 当成稳定身份，
导致相同 schema 在新 Session 被错误排除。控制面本地发现还可能发生在 SQLite
写事务内。

## 实现与边界

- 新 effects Run 明确选择 MCP 且未提供共享 Mission 时，先追加封闭 payload v1
  的 `run.mission_pending`，保存配置、已知工具身份、选中 server、显式工具限制、
  模板正文与编译后输入。claim 后在事务外只发现选中 server，再于首游标和首次
  `model.intent` 前固定唯一 `run.mission`。
- pending 仅可加入选中 server、profile 与显式 tool_ids 交集内的新工具；已知同 ID
  的 schema 或描述改变不升级。request_id 在 pending 与冻结后都核对原请求配置。
- Mission 的 MCP 版本排除 Session generation 和远程会话 nonce；稳定远程身份仍
  包含 URL 与凭据环境变量名称。实际 ToolSpec、审批、Grant、Receipt 和派发继续
  绑定具体 Session 版本，旧批准不能跨 generation 复用。
- 共享 Mission 保留固定候选，不加入新发现工具。当前空 registry 下，只有 effects、
  Task 明确选择 server 且未被显式 `run.start.tools` 排除的 MCP 固定身份可留在
  `effective_tools` 上限；模型和派发仍与当前真实 registry 身份取交集。已知身份
  漂移、pure_only、未选 server 均不能利用离线候选扩大路由。
- pending 与已冻结输入均使用保存的模板和消息；暂停后恢复不重编。历史无
  Mission/pending 的 Run 保持原解释，历史含 generation 的快照不改写。

新增事件在代码集合、语义校验、JSON Schema 与最小合法事件夹具同步登记。
对应契约分别由 `a1f66cc` 与 `911dd3d` 的独立 `docs(contract)` 提交定义。

## 验证口径

原始红绿日志保存在本工作树 ignored 目录
`.omc/artifacts/mission-mcp-integration/`，失败原件保留：

- `red-semantic.log`：真实 stdio 新 Session、相同 schema 的共享 Mission 路由失败；
  schema 漂移负控保持拒绝。
- `red-pending.log`：direct/queued 首轮工具缺失，以及事务内实际 server 启动断言。
- `red-pending-retry-finalized.log`：有效工具为空后，原 request_id 参数重试被误判冲突。
- `red-remote-semantic.log`：真实 HTTP 新 Session 的随机传输身份污染 Mission 版本。
- `red-shared-empty.log`：真实前序 Session 生成的共享 Mission 在新空 registry 的
  direct/queued 入口均丢失固定 MCP 候选。

正向回归经过真实 stdio/HTTP 协议、执行侧审批与 `tool.result`；
负控覆盖 schema 漂移、历史 generation、未选 server、显式 tools、profile、
pure_only、输入模板变化及 request_id 配置变化。HTTP 夹具统一使用同根
`events.db` / `blobs` / data_root；旧 `main.db` 配合同级保留材料目录所触发的
所有者拒绝和异根 legacy-layout 拒绝均留在过程日志，不放宽产品守卫。

本分支验证仅覆盖 Mission、MCP、执行准入、事件、控制面及受影响循环域；
完整组合候选的本机全量、独立第二读者和 hosted required CI 由整合方重新绑定。
Goal G1/V2 的入口接缝由整合方统一适配，不把本分支结果当作 Goal 交付证明。

## 未验证与保留限制

真实 Seatbelt 测试中的脱组后代仍可逃离 process group；process_tree 未 enforced，
完整自动四边界 command/MCP 继续拒绝。部分边界受管正向路径不代表完整自动隔离
交付。本机协议测试不证明其他平台用户设备、真实付费模型或原生桌面体验。
