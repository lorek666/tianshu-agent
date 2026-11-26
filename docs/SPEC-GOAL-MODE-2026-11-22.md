# Goal 目标模式

状态：2026-11-22 契约与分阶段设计；实现范围以交付记录为准。

## 1. 原方案 → 新方案 → 原因 → 影响

原方案的 `Task.goal` 是任务文本，一次 Run 结束不代表用户目标已验收。计划模式
生成可确认的实施计划，但没有跨 Run 的验收条件、轮次限制和完成记录。

新方案在 Task 当前修订上建立 Goal，保存验收条件、执行资格和最大推进轮次。
每次推进创建一个可审计 Run；Run 成功仅表示这一轮完成，Goal 完成必须由用户验收。
复用事件日志、队列、执行侧授权、预算、游标与恢复，不引入第二个事实数据库。

原因：用户需要把多轮工作围绕一个可验收目标组织起来，知道离完成还差什么，
而不是把模型说“完成”或一次对话结束当作目标完成。

影响：C3 注册表与 schema、Goal 控制面、Run 首轮上下文、桌面命令和目标面板。
C1/C2/C4/C5、工具清单和既有 Run 状态不变。

## 2. 分阶段交付

### G1：本次开发范围

- `/goal [目标正文]` 和目标入口；用户填写验收条件与轮次上限。
- 创建 Goal 不调用模型；“推进一轮”才入队，**每轮手动继续**。
- 持久化目标绑定、条件、执行资格、轮次与人工验收结论；重新连接仍可查看。
- 下一轮引用上一轮成功的最终正文，作为历史材料而非授权。
- 用户可用原 Run 控件暂停、恢复、取消当前轮；取消一轮不谎报 Goal 完成。
- 人工逐项勾选验收条件后，确认完成；也可在没有活跃 Run 时结束目标。
- 工作中修改 Task 目标或工作区后，旧 Goal 为 `stale`，必须新建，不能承接旧批准。

### G2：后续，尚未交付

受控自动推进、独立机器验收器、累计目标预算、期限、无进展停机、
失败分类与人工解除阻断、Goal 的 CLI。自动模式必须先新增契约和负控，
不能用提示词或浏览器定时重发模拟，不承诺“无人值守直到成功”。

## 3. 数据与状态

一个 Task 当前修订至多一个未关闭 Goal；每个 Goal 配置不可变。
`goal_id` 唯一，绑定 `task_id + goal_revision`。
验收条件 1–12 条，每条非空且 ≤1024 UTF-8 字节；总目标正文沿 Task 保存。
`max_rounds` 为整数 1–10，默认 3；执行资格固定为既有
`RunExecutionPolicyV1`（`effects|pure_only`），推进时不可扩大。
请求标识为 1–128 位字母、数字、下划线或连字符。

Goal 不另建投影表：从按 seq 排序且校验过的本 Task 事件与 Run 投影重建：

- `ready`：尚未推进；
- `running`：末轮未终结，具体排队、审批、暂停、核查由 Run 状态显示；
- `awaiting_review`：末轮成功、最终正文非空且无未知结果；待人工验收或继续；
- `exhausted`：成功末轮已用尽轮次，仍可验收，不能继续；
- `blocked`：末轮失败、取消、结果未知或最终正文无效，不自动重试；
- `stale`：Task 修订已变；
- `completed|cancelled`：人明确关闭。

轮次按照已提交 `goal.round_started` 计数；一次失败仍占一轮。
Goal 关闭不修改 Task 或 Run 历史，也不回滚已生效的副作用。

## 4. C3 增量

新增三种当前 v1 事件，`actor_kind=human`：

- `goal.created`：既有 Task scope（`run_id=""`），封闭 payload：
  `format_version=1, goal_id, goal_revision, request_id, criteria[],
  max_rounds, execution_policy`。
- `goal.round_started`：本轮 Run scope，封闭 payload：
  `format_version=1, goal_id, round, request_id, source_run_id`（首轮 null）。
  与 `run.start` 和作业入队同事务，在首次模型 intent 之前；
  `run.start.task` 保存包含条件和历史正文的本轮输入快照。
- `goal.closed`：Task scope，封闭 payload：
  `format_version=1, goal_id, request_id, outcome=completed|cancelled,
  confirmed_criteria[]`（0 起、无重复的索引；完成时必须完整覆盖）。

注册表、`schemas/c3/event.json` 与 PRD §5.3 同步。
不把新类型伪造为旧 v0 事件；不更新/删除历史事件。
Goal Run 从持久化 `run.start.task` 恢复首轮；已存在 model intent 而游标丢失时拒绝
续跑，结果未知不重发。历史普通 Run 提示内容与回放含义保持原样。

## 5. 控制面

方法仅供本机人机界面，不加入模型工具：

- `goal.get(task_id) -> {goal: GoalView|null}`：返回最新 Goal，纯读。
- `goal.create(task_id, goal_revision, criteria, max_rounds=3,
  execution_policy, request_id) -> {goal}`：空闲且当前修订匹配时创建。
- `goal.advance(goal_id, request_id) -> {goal, run}`：只入队一轮；
  前轮须成功且可消费、Task 空闲、修订匹配、轮次未超限。
- `goal.close(goal_id, request_id, outcome, confirmed_criteria=[]) -> {goal}`：
  空闲时可取消；完成须匹配当前修订、末轮成功且所有条件已由人确认。

`GoalView` 为封闭 v1：`format_version, goal_id, task_id, goal_revision,
criteria, max_rounds, execution_policy, rounds_started, latest_run_id,
status, confirmed_criteria`。通过独立 RPC 读取，不扩大首屏 `state` 响应。
不存在 → `not_found`；形状/范围非法 → `bad_params`；
状态/修订/幂等参数冲突 → `conflict`。
创建、推进、关闭均在 `EventLog.transaction()` 内校验并提交。
相同操作与 request_id 重试返回原对象；参数变化拒绝；并发点击不得重复入队。
轮次推进只承接**最新 Goal 自己的**前轮，不能引用跨 Task、普通 Run 或旧 Goal。

## 6. 界面语义

新建页面选择“目标”或输入 `/goal 正文` 后展示验收条件、最大轮次。
裸 `/goal` 只切换模式。已有 Task 的 `/goal [说明]` 打开 Goal 配置；
说明作为配置草稿，不静默 `task.revise`、不立刻开跑。
`/goal` 优先于同名 Skill；`/skill goal` 仍可显式调用该技能。
目标面板显示“每轮手动继续”、条件、已用轮次、状态和末轮入口。
操作失败保留输入；切换 Task/修订或卸载后迟到响应不得覆盖新页面。
Goal 的推进、验收和关闭只操作面板所展示的 Goal，不靠当前选择的历史 Run 猜测。
演示模式明确返回“不支持目标操作”，不伪造目标持久化能力。

## 7. 验收与边界

- 真控制面 + AgentLoop 证明创建不调用模型、推进只入队、条件进入上下文、
  续轮使用已保存正文、replay 通过；重开库和重建 Task/Run 后结果一致。
- 参数、修订、幂等、事务回滚、轮次耗尽、失败/空正文/未知结果拒绝负控。
- 两个真实子进程经屏障并发推进，只有一个新 Run 和一个计数。
- 已有审批与 `pure_only` 不能因 Goal 放宽；模型自称完成不关闭 Goal。
- UI 验证内置命令优先级、创建/推进/逐项验收、失败保留输入、换任务迟到响应。
- Tier 1：本机全量 pytest、lock、Ruff、前端安装/类型/测试/构建、独立第二读者。
  本地结果不替代 GitHub required CI；不宣称已验证真实付费模型或其他平台设备。
