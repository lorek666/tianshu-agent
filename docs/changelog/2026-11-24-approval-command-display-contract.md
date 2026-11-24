# 2026-11-24 · exec 审批命令展示契约

## 原方案 → 新方案

- **原方案**：`approval.requested` 没有独立的命令展示对象；审批卡无法从事件可靠地逐项展示完整 argv、实际 cwd 与后端。
- **新方案**：exec 审批可带封闭的 `command = {argv: string[], cwd: string, backend: string}`，控制面 `ApprovalView.command` 按 approval_id 回读并完整纯文本展示。非 exec 与历史缺字段为 `null`，历史 exec 缺详情明确告警。

## 为什么

真实探针确认 `ExecPolicy.argv_allowlist` 只约束 `argv[0]`；在同一解释器下，后续参数可能携带实际动作。审批人必须看到完整参数才能作出有意义决定。展示字段不是新的授权来源，也不把 argv[0] 策略扩张成参数白名单。

## 边界与影响

- 授权仍绑定原完整 args、`args_hash` / `action_digest`；执行侧二次核验与审批一次性消费不变。
- 新 exec 审批必须带真实展示详情；畸形对象拒绝写入，历史事件保持兼容读取。
- 实现将同步 C3 schema 的 current/legacy 分支、`event_schema.py`、dispatcher/permissions/control、React 审批卡与 `ui/dist`，并以真实 schema、版本旁表、关闭重开、参数篡改与零 spawn 负控验证。
- 本 PR 只定义契约，不夹带运行时代码、测试或构建产物。
