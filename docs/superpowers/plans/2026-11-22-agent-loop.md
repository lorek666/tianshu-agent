# Agent 主循环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将主循环规格落实为可运行、可恢复且有执行侧边界的产品能力。

**Architecture:** 复用事件日志、Run/Job、Dispatcher 与预算账户。Mission 固定输入，
Plan/Verification 保存可核对状态，Goal 协调器串行驱动阶段；授权与隔离独立把关。

**Tech Stack:** Python 3.12 标准库、SQLite、React/TypeScript，保持冻结依赖。

**Spec:** [Agent 主循环规格](../../SPEC-AGENT-LOOP-2026-11-22.md)

**执行状态（2026-11-23）：** Task 1–5 已集成本地实现及定向红绿；
Task 6 的 UI、dist 及前轮本地门禁已完成，但首轮完整原生 CI 真实失败。
修复候选 `94fd94e` 已通过新的完整本机回归、独立增量复审及文档对账，
required CI 绑定新的最终 PR 对象单独验收。
勾选表示该施工项的本地证据，不代表合入、发布或 P0-E 的完整平台隔离通过。
精确冻结和未决范围见[实施记录](../../changelog/2026-11-23-agent-loop-implementation.md)。

## Global Constraints

- 事件 append-only；状态与事件同事务，外部执行不在写事务内。
- Mission/Plan/Skill 不授权；未知效果不重放，人工验收不洗白未知结果。
- 新契约先文档独立提交；同一 Task 最多一个活跃 Run。
- 一会话一工作树；并行实现不共享索引，不使用 stash。
- 定向红绿与真实负控；冻结后全量 Python、静态门禁及独立复审。
- UI 改动执行冻结安装、类型、测试、构建并提交 dist。
- 未经原生证据验证的平台继续 unsupported，不用配置代替隔离证明。

## Review Focus

- 历史 Run 不因新提示模板改变输入或 Responses scope。
- 旧页面及并发请求不能覆盖新图、验收或授权。
- 代码变化和外部写者使验证证据失效。
- 崩溃、重连及重复唤醒不重复执行、计轮或计费。
- 自动模式到限、否决、未知结果和隔离不足时确定性停机。

## Task 1: Mission 与提示快照

**Files:** `src/tianshu/mission.py`、提示/入口/replay 模块、
`schemas/mission.json`、`tests/test_mission.py`。

**Interfaces:** 提供版本化校验、冻结、读取 Mission 及有效工具交集；
既有入口保持兼容，后续阶段通过明确 Mission 绑定使用。

- [x] 写新任务通用提示、快照损坏、路由交集与历史回放红测。
- [x] 实现不可变 Mission、入口绑定和恢复/回放读取。
- [x] 运行定向测试及提示/workflow/replay 回归；提交代码与测试。

## Task 2: Plan 与独立 Verification

**Files:** `src/tianshu/plans.py`、`src/tianshu/verification.py`、
`src/tianshu/agent_control.py`、对应 schema 与测试。

**Interfaces:** Plan 按 Mission 摘要绑定不可变图/检查清单；检查产生权威结果；
提供确认、CAS 重规划、真实人工决定、指纹核查及完成门。

- [x] 写 DAG、覆盖、跨作用域、过期证据与人工验收竞争红测。
- [x] 实现图校验、事件派生视图、确定性文件/来源/命令检查。
- [x] 接入控制面及真实执行链，验证不旁路 Dispatcher。
- [x] 运行受影响域回归并提交。

## Task 3: 有限授权

**Files:** `src/tianshu/consent.py`、permissions/dispatcher 接缝、
对应 schema/控制面及定向测试。

**Interfaces:** 人类签发有限规则；派发时原子消费并派生单动作 Grant；
最终守卫重核，使用 `execution.started` 作为撤销线性化点。

- [x] 写精确匹配、TTL、重启失效及真实双进程消费/撤销红测。
- [x] 接入默认拒绝、显式否决、目录/命令限制及撤销。
- [x] 定向红绿、授权/派发回归并提交。

## Task 4: 执行对象隔离准入

**Files:** execution subject/准入模块、命令与 MCP 接缝、隔离测试。

**Interfaces:** 自动命令和本地 MCP 启动前绑定实际 subject 与探针；
覆盖会话开始、发现、调用、停止及恢复；不静默降级 local。

- [x] 写 local none、伪造证据、MCP 发现前启动拒绝及生命周期红测。
- [x] 实现命令/MCP 统一准入，接入可证明的本机后端。
- [x] 原生平台能力据实启用；无证据对象保留 unsupported。
- [x] 运行隔离/MCP/命令回归并提交。

## Task 5: Goal V2 协调与预算

**Files:** Goal V2 模块、`worker.py`/`daemon.py`、预算父链、
`agent_control.py` 与闭环测试。

**Interfaces:** 消费已确认 Plan；每轮一个步骤及串行验证；
持久化阶段、边界、阻断/恢复和幂等阶段身份。

- [x] 写真实驱动闭环、累计模型请求/预算、无进展及恢复红测。
- [x] 实现手动/有限自动、累计账户、停机门和权威完成条件。
- [x] 接入 Job/Worker，保持 G1、Run 和子任务契约。
- [x] 运行 Goal/Worker/恢复/预算回归并提交。

## Task 6: 桌面入口与交付验证

**Files:** `ui/src/api`、Goal/Plan/验收/授权视图及 UI 测试、
`ui/dist`、知识库与独立 changelog。

- [x] 写创建、确认、人工验收、暂停/恢复、有限授权 UI 红测。
- [x] 接入真实 RPC，显示每轮目标/证据/剩余工作及阻断。
- [x] 执行 UI 四项门禁；冻结代码树。
- [x] 在原生 CI 修复候选上重做全量 Python、lock/Ruff/diff 与独立第二读者探针，登记未决项。
- [x] 更新文档，分清本地验证、远端 CI、原生平台与待验范围。

## 执行说明

用户已明确要求继续完成功能，按本计划持续执行，不重复请求实施许可。
独立模块可在各自工作树按 parallel-agents 技能并行；集成、门禁和复审串行。
Windows 原生机制、MCP 生命周期和输入闭包探针结论须写入交付记录，
无法满足边界时不得把失败改成通过。
