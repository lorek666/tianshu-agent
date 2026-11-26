# 对话界面 Skill 命令 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新建和追问输入框中通过 `/` 发现并调用本地 SKILL.md。

**Architecture:** 后端有界发现和准备技能，前端共用菜单与准备客户端。
沿现有任务目标保存文本快照，不修改模型工具面、事件形状、恢复和授权判定。

**Tech Stack:** Python 3.12 标准库、React、TypeScript、现有 pytest/Vitest。

**Spec:** [SPEC-SKILL-COMMANDS-2026-11-22.md](../../SPEC-SKILL-COMMANDS-2026-11-22.md)

## Global Constraints

- Python 保持 `>=3.12,<3.13`，零新增第三方运行时依赖。
- 只读取 `~/.agents/skills` 和所选工作区 `.agents/skills`，禁止自动执行脚本和权限升级。
- 沿用当前隔离工作树和新分支；不修改其他 Agent 的工作树、索引、分支。
- 契约文档单独 `docs(contract):` 提交；实现及证据另存，暂不推送。
- 上限、RPC V1 和元数据解析按 spec 精确执行，错误不降级成普通用户任务。

## Review Focus

- 同名跨目录和内容更新：不能误调用另一技能或选择后静默升级。
- 符号链接、FIFO、超长文件：不能越界装载或使控制面无界等待。
- 菜单请求乱序及中文输入法：不能覆盖新工作区技能或误发送消息。
- 准备失败和原计划模式：不能先修订任务或静默切换执行模式。
- 删除技能后的恢复及 replay：使用已持久化目标，不依赖本地文件仍存在。

### Task 1: 本地发现、确定性准备与控制面

**Files:**
- Create: `src/tianshu/skills.py`
- Modify: `src/tianshu/control.py`
- Create: `schemas/skill.json`
- Create: `tests/test_skills.py`
- Modify: `docs/M5-desktop.md`

**Interfaces:**
- Consumes: `workspace_roots: list[str]`；测试显式注入临时 `user_root: Path`。
- Produces: `list_skills(workspace_roots, *, user_root=None) -> dict`；
  `prepare_skill(workspace_roots, skill, input_text, *, user_root=None) -> dict`，
  以及 `ControlPlane(..., skills_root: Path | None = None)` 的只读 RPC 映射。

- [x] 写真实临时目录用例，观察旧实现缺少 RPC/模块而失败；再覆盖 spec 中元数据和文件边界。
- [x] 实现标准库模块及版本化结果，分别映射 `skill.list`、`skill.prepare`。
- [x] 测试同名拒绝、旧 id 失效、确定性快照及列表不返回正文。
- [x] 真实 `task.create`/`run.start`/模型驱动，删除源文件后核对 replay；
  恶意技能不能通过计划模式写入，原审批/工具清单不变。
- [x] 运行 `uv run --frozen pytest tests/test_skills.py tests/test_control.py tests/test_workflow.py`。

### Task 2: 共用命令菜单及两个对话输入框

**Files:**
- Create: `ui/src/api/skills.ts`
- Create: `ui/src/components/SkillCommandMenu.tsx`
- Modify: `ui/src/lib/commands.ts`
- Modify: `ui/src/components/WelcomeComposer.tsx`
- Modify: `ui/src/components/TaskWorkspaceView.tsx`
- Modify: `ui/src/App.tsx`
- Modify: `ui/src/demo/host.ts`
- Modify: `ui/src/styles.css`
- Modify: `ui/src/lib/__tests__/commands.test.ts`
- Create: `ui/src/__tests__/skill-commands.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 RPC V1 描述对象与准备结果；App 提供 `SkillsApi`。
- Produces: `createSkillsApi(host)`；共用 `SkillCommandMenu`；
  `parseComposerCommand` 的 `skills`/`skill` 分支及 `prepareComposerText`。

- [x] 先写命令解析红测；保持旧命令优先，未知名称经后端验证，不作为普通目标发送。
- [x] 写两个真实输入组件的选择及准备失败红测，使用临时内存 Host 仅替代 IPC 边界。
- [x] 实现菜单，匹配原有字体、色彩和布局，左对齐名称/描述、来源可辨，不重做页面。
- [x] 挂接 App 客户端和演示空列表；成功准备后才调用原有 onSubmit/onFollowUpSubmit。
- [x] 覆盖异步乱序、Escape、方向键、Tab/Enter、Shift+Enter 和输入法，不改变原运行模式。
- [x] 运行前端 typecheck 与完整 test。

### Task 3: 冻结、对账及本地提交

**Files:**
- Modify: `docs/KNOWLEDGE.md`
- Create: `docs/changelog/2026-11-22-skill-slash-integration.md`
- Modify: `ui/dist/`（仅构建生成）

**Interfaces:**
- Consumes: Task 1/2 的实际代码、测试日志和 spec。
- Produces: 本地可审查提交、未验证边界和可追溯证据。

- [x] 对照 spec 检查所有验收，不把未做的功能标成已交付。
- [x] 运行受影响 Python 域、`uv lock --check`、`uv run --frozen ruff check .`；
  若未修改高危核心，维持 Tier 2 范围并保留明确依据。
- [x] 运行冻结安装、前端 typecheck/test/build，检查并收录 ui/dist 差异。
- [x] 核对文件范围和工作区隔离；按实际结果更新知识库、变更碎片及此计划。
- [x] 完成分支复核和本地提交；报告未推送、未取得新 GitHub CI 的边界。
