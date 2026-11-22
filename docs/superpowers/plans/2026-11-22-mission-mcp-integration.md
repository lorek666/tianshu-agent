# Mission MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 direct、桌面入队及共享 Mission 使用实际发现的固定 MCP schema，同时保留独立 Session 的执行绑定。

**Architecture:** 新 Run 显式选择 MCP 且没有共享 Mission 时，先保存封闭 `run.mission_pending` 输入材料；取得 claim 后在事务外发现，再于首游标与模型 intent 前固定唯一 Mission。Mission 使用稳定语义工具版本，审批与执行继续绑定含 generation 的版本。

**Tech Stack:** Python 3.12，标准库，SQLite，pytest，uv。

**Spec:** `docs/SPEC-AGENT-LOOP-2026-11-22.md` §3.2、§5、§10。

## Global Constraints

- 事件 append-only；新增事件仅 payload v1，不补写历史 Run。
- 模型、网络、命令、探针和人工等待均不得置于 SQLite 写事务内。
- Python `>=3.12,<3.13`，零新增第三方运行时依赖。
- Mission 不授予权限，共享 Mission 的工具候选不可扩充。
- 已知工具身份变化不得自动升级，Session generation 保留执行侧绑定。

## Review Focus

- queued 请求重试在 pending 阶段更换 task_kind、criteria 或 tool_ids，必须冲突。
- 已知同 ID 工具发现新 schema，必须从有效集合排除。
- 未选中 server、profile 排除工具及显式 tool_ids 之外工具不得扩充。
- 冻结后环境、模板或发现结果改变，不得重编首轮输入。
- 历史无 pending/mission 的 Run 继续旧解释；损坏 pending 不得降级为历史。

### Task 1: Register the pending-input contract

**Files:** `docs/PRD-v1.md`、`docs/SPEC-AGENT-LOOP-2026-11-22.md`。

**Interfaces:** 契约规定 pending 保存初始输入、已知工具身份及选中 server；最终 Mission 在首次模型 intent 前唯一固定。

- [x] 独立提交 `docs(contract):`，写清旧方案、新方案、理由和影响模块。

### Task 2: Separate semantic routing identity

**Files:** `src/tianshu/tool_registry.py`、`src/tianshu/mission.py`、`tests/test_mission_mcp.py`。

**Interfaces:** `McpToolBinding.semantic_tool_version`；Mission 的 `tool_identity(spec, registry=None)` 使用稳定版本；执行 `ToolSpec.version` 不变。

- [x] 写跨 generation 相同 schema 及 schema 漂移回归，运行并观察真实红。
- [x] 最小实现并运行同组测试转绿。

### Task 3: Finalize pending Mission after discovery

**Files:** `src/tianshu/mission.py`、`loop.py`、`control.py`、`event_schema.py`、`events.py`、`schemas/c3/event.json`、事件夹具、`tests/test_mission_mcp.py`。

**Interfaces:** `prepare_run_mission(...)` 保存 pending 或立即冻结；`finalize_pending_mission(...)` 在发现后幂等冻结；`pending_mission_for(...)` 校验重试配置。

- [x] 写 direct/queued 真实 stdio 发现、事务外探针及 request_id 冲突红测。
- [x] 写共享 Mission、不选中 server、显式 tool_ids、profile、已知漂移及冻结恢复回归。
- [x] 实现 pending 的封闭契约、事件注册与入口接缝，确认定向组转绿。
- [x] 同步知识库能力边界与独立 changelog。
- [ ] 运行 Mission、MCP、执行准入、事件、控制面和工作流域回归及 Ruff/lock/diff；完整候选的全量与独立复审由 root 执行。
