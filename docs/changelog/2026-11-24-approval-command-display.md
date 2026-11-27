# 2026-11-24 · exec 审批命令展示与派发前参数复核

## 背景

`ExecPolicy.argv_allowlist` 只约束 `argv[0]`，同一解释器后的参数仍可能携带实际动作。审批人必须看到完整 argv、实际 cwd 与后端，才能判断这条命令是否值得批准。

独立复审同时发现：`dispatch()` 读取权威 `args_payload` 后，原先要到执行器才发现参数摘要不匹配，审批可能在此前被消费。展示字段本身不授权，但不能让“展示更完整”的实现掩盖派发顺序缺陷。

## 实现

- `approval.requested.command` 使用封闭对象 `{argv, cwd, backend}`，仅进入 C3 current v1 与对应 JSON schema；冻结的 legacy v0 保持原形状并拒绝该字段。历史缺字段仍可读，畸形对象与未来版本不静默回落。
- dispatcher 从已校验 argv、解析后的实际 cwd 和装配后端生成展示值；控制面按 approval_id 回读，UI 逐项纯文本完整展示，不截断、不猜测、不注入 HTML。
- 普通 MCP 调用按 `c4.mcp_call` 展示，保留端点及审批操作。只有命令审批缺字段时才提示拒绝并重发。
- `dispatch()` 在消费审批、签发 grant、写 `tool.intent` 前重新计算权威 args 摘要；不匹配时记录拒绝并返回真实 `ToolResult("rejected", "none_applied", ...)`，主循环可正常补记结果。
- 已完成派发在权威参数重哈希前幂等返回原 `tool.result`，避免终态脱敏后的 `args_payload` 被误判为篡改；同 `call_id` 换请求、pending 参数篡改和事务内竞态仍拒绝。
- 授权仍只认原 request、`args_hash` / `action_digest`；伪造展示值不能放行篡改后的真实参数。

## 验证

- 真实 schema 参数化 v0/v1：可选、封闭、完整、额外字段拒绝、v2 拒绝、禁止 legacy 回落。
- 真实 EventLog：版本旁表、关闭重开、畸形 payload 不落盘。
- argv/cwd 篡改与伪造展示负控：零 spawn、零 `tool.intent`、零 `tool.result`，审批保持 approved 且未消费。
- Python 定向回归、UI typecheck/test/build 与 `ui/dist` 重建通过。
- 本机 Vite 真实浏览器组件验收通过：完整 argv 逐项纯文本显示，含首尾空格与 HTML 片段的参数未注入脚本；历史缺详情与畸形详情均显示拒绝/重发告警，非 exec 审批不出现命令区。
- `uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 通过。
- 整合到 Goal V2 和 MCP 结果持久化后，复审复现了普通 MCP 卡片误报缺少命令详情的回归。新用例验证普通 MCP 审批仍可操作，旧命令审批仍显示缺字段提示。有限授权回归也已对齐派发的幂等语义，确认重复请求读取原结果时不追加事件、不消耗配额、也不再次写入工作区。整合后的全量及最终验证以 PR #234 的候选记录为准。

独立第二读者先给出 PARTIAL，指出拒绝路径误返回 `DispatchDecision`；修复后复核通过。最终复审又复现并关闭终态脱敏后的幂等重放 P1，确认 pending 篡改仍在授权前拒绝。复审绑定完整 code/test/UI tree（`src` / `tests` / `schemas` / `ui` 的 tracked + untracked 实际字节，按仓库相对路径排序）SHA256：

```text
e59d080d25a96fe2278e4dc7d2fca8f09adc7f4a6c13d1dd81de1dc9712d8127
```

## 边界

本改动改善审批可见性与派发前完整性，不把 `argv[0]` 白名单扩张成参数白名单，也不改变默认 `local` 后端 `isolation="none"` 的事实。解释器允许后的任意代码能力仍需通过策略收紧或选用已验证沙箱；不能靠审批卡展示冒充隔离。
