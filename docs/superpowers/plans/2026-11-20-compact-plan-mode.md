# 手动压缩与计划模式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付桌面与 CLI 可用的 `/compact` 和需明确确认的计划模式。

**Architecture:** 特殊操作使用既有持久化 Run 与后台作业。
`run.workflow` 保存模式与来源材料，计划和压缩强制 `pure_only`；
确认计划创建新 `effects` Run。历史事件与普通 Run 行为兼容。

**Tech Stack:** Python 3.12 标准库、React、TypeScript、pytest、Vitest。

**Spec:** [compact-plan-contract.md](../../compact-plan-contract.md)

## Global Constraints

- 零第三方运行时依赖；Python `>=3.12,<3.13`。
- 事件 append-only，状态与事件同事务，事务经 `EventLog.transaction()`。
- 授权只在执行侧判定；计划确认不替代逐次工具审批。
- 保留 `run.start` 历史版本，不改已有事件。
- 一会话一分支一独立 worktree；契约文档与实现分别提交、分别 PR。

## Review Focus

- 计划生成中、失败或目标已变时，确认必须拒绝且不产生新作业。
- 重复请求与重复确认不能导致重复调用和重复副作用。
- 恶意模型在计划/压缩时提出写工具，执行端仍须拒绝。
- 摘要失败、重启、游标丢失不应丢源事件或盲目重发模型请求。
- 已压缩的旧目标不应在追加要求时重新全文注入。

### Task 1: 持久化工作流与后端入口

**Files:** `src/tianshu/{workflow,control,tasks,loop,replay,event_schema,events}.py`、
`schemas/c3/event.json`、`tests/test_workflow.py`。

**Interfaces:** `prepare_workflow` 构造来源材料；`workflow_messages` 重建初始提示；
`run.compact`、`plan.execute` 与扩展的 `run.start` 返回既有 RunEnvelope。

- [x] 先写计划只读、确认绑定、压缩复用、重放、幂等和非法来源测试，运行见红。
- [x] 实现版本化模式事件、原子入队、来源校验、模型路径和回放。
- [x] 定向与受影响域测试转绿，保留红绿日志。

### Task 2: CLI 与桌面操作

**Files:** `src/tianshu/cli.py`、`ui/src/{App.tsx,api/*,components/*,lib/commands.ts}`、
相关 pytest/Vitest 测试和 `ui/dist`。

**Interfaces:** CLI `run plan|compact|execute-plan`；UI `/plan|compact|execute`、
计划/执行切换、计划确认按钮，共用后端权威判定。

- [x] 先写命令解析、真实表单交互和 CLI 测试，运行见红。
- [x] 接入真实 RPC、模式选择、确认与错误状态，不改现有整体布局。
- [x] Python 定向回归与 UI typecheck/test/build 转绿。

### Task 3: 最终验证与独立复审

**Files:** `docs/KNOWLEDGE.md`、`README.md`、`docs/context-summaries.md`、
`docs/changelog/2026-11-21-compact-plan-mode.md`。

- [x] 更新能力边界与使用说明，核对链接和事实。
- [x] 冻结代码树，执行 `uv lock --check`、ruff、全量 pytest 与 UI 全套。
- [x] 按 Tier 1 要求，由独立第二读者执行负控与探针复审（IPC P1 修复后已重新绑定）。
- [ ] 分别提交契约与实现，推送独立 PR；精确记录实际通过与未决门禁。
