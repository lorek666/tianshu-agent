# 手动上下文压缩与计划模式契约

状态：2026-11-20 新增契约；实现与验证状态另见交付记录，不以本文代替交付证据。

## 原方案 → 新方案 → 原因 → 影响

原方案只有普通执行与 `pure_only` 执行资格，语义摘要仅有内部自动触发路径。
新方案提供 `/compact`、`/plan` 与明确的计划确认入口，复用现有 Run、作业、
模型调用账本、执行侧 `pure_only` 与事件回放，不新增运行时依赖。
原因是用户需要主动整理长上下文，以及可检查、确认后才执行的计划。
影响 `workflow`、TaskStore、AgentLoop、CLI、控制面、回放、C3 schema 与桌面界面。

## 持久化与 C3

新增 `run.workflow` v1，封闭字段：
`format_version=1`、`mode`（`plan|compact|execute`）、`request_id`、
`source_run_id`（字符串或 null）、`source_text`、`source_seqs`（正整数数组）、
`source_digest`（正文 UTF-8 SHA256）、`source_goal`。
事件与 `run.start`、入队同事务提交；每个 Run 至多一个，且必须在首次模型调用之前。
历史 `run.start` v0/v1/v2、历史 Run 的提示与回放含义不变。
来源正文是有来源的派生材料，不提高信任等级，不构成授权。
摘要不删除或改写源事件。回放读已保存的材料，不重新调模型或查当前索引。

## 请求身份与重试

`run.workflow` v1 记录已选来源，保持原样。另增 `run.workflow_request`，仅使用
payload v1，没有 v0 解释。封闭字段是 `request_id`（1–128 位 ASCII 字母、数字、
下划线或连字符）与 `requested_source_run_id`（非空字符串或 null）。事件信封的
`task_id` / `run_id` 指向接受该请求的任务和 Run；仅人触发的控制面可写。

- `/compact` 每次新建 Run 时，在 `run.workflow` 之后、入队之前同事务追加一条
  请求身份事件。`requested_source_run_id=null` 表示自动选源，字符串表示调用者
  显式指定来源且必须等于 `run.workflow.source_run_id`。同一编号重试先匹配原参数，
  再返回已绑定的 Run；即使有更新的候选来源也不改选。显式与自动不能混用同一编号。
- `plan.execute` 用新编号重复确认同一有效计划时，先核对原执行 Run 冻结的参数，
  再在同一控制事务追加一条请求身份事件，返回原 Run。此时来源参数须为原计划的
  Run ID；新编号即使在执行 Run 已结束后也能绑定。追加不创建作业、不重新派发工具、
  不改变已结束 Run 的提示或回放。所有成功绑定的编号都保留给该请求；输出文件、
  执行资格、附件、所请求模型档案或追问参数不一致均拒绝。
- 旧 `run.workflow` v1 仍可读取、执行和回放。只有在旧压缩 Run 收到相同编号重试时，
  因无法证明原选源方式，拒绝复用并提示用新编号。旧计划确认首次绑定的编号仍有效；
  没有持久化的历史别名无法恢复。请求身份不授权任何工具执行。

## `/compact`

任务空闲时，以指定或最近一次已结束 Run 的完整可重建消息为来源，按字节预算
编译为有界材料，再创建独立 `pure_only` Run；只调用模型生成交接摘要，不派发工具。
原始目标、约束、决定、进展、待办、重要路径和未决结果应在摘要中保留。
执行中或暂停中的任务明确返回 busy，不取消、篡改或重放原 Run。
失败、取消、空白或超限输出不成为后续上下文。
成功压缩结果供后续同任务 Run 使用；追加要求只附加新增部分，避免把已压缩的
历史目标全文再次注入。替换目标、跨任务来源、损坏来源均不复用。
模型调用仍遵守现有 intent/result、预算、费用、取消与崩溃恢复约束。

## 计划模式

`run.start(mode=plan)` / `/plan` / 界面模式选择创建独立 `pure_only` Run，
允许只读分析，输出可阅读的实施计划；写文件、执行命令、外发及其他非 pure
工具仍由既有执行侧规则拒绝，不能只靠提示词约束。
计划生成成功后，用户可继续补充要求并重新规划，或明确点击「确认计划并执行」
（`/execute`）创建新的 `effects` Run。确认绑定具体计划 Run 与目标修订；
跨任务、未完成、失败、被替换、旧修订计划均拒绝，重复确认不得重复入队。
确认计划不等于批准计划中的每项工具副作用，既有逐次工具审批继续生效。

## 控制面、CLI 与界面

- `run.start` 新增可选 `mode=execute|plan`、`request_id`。
- `run.compact(task_id, source_run_id?, request_id)` 只入队，立即返回 Run。
- `plan.execute(plan_run_id, goal_revision, request_id)` 只入队，立即返回 Run。
- 同任务重复 `request_id` 返回同一 Run；不同参数复用相同标识拒绝。
- 特殊 RunView 增加 `workflow`（模式、来源 Run）；普通历史视图保持兼容。
- CLI 提供 `run plan`、`run compact`、`run execute-plan`，与 RPC 共用实现。
- 桌面输入区识别独立 `/compact`、`/plan`、`/execute`，不会把控制命令当作
  修改任务目标的普通文本；含正文的 `/plan 要求` 作为计划任务输入。
- 用户可看到计划内容、待确认状态、压缩进度与失败原因；不展示内部摘要或请求标识。
