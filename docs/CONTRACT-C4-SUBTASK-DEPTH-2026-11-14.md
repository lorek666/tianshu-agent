# 契约增量分析 · 子代理深度>1 与 fork 式（W10 契约部分）

- 日期：2026-11-14 · 基线：main@88ec0ab · 状态：**分析稿，未实施**
- 依据：AGENTS.md「契约变更单独提交：`docs(contract):` 单独 commit / 单独 PR，body 写『原方案 → 新方案 → 为什么 → 影响哪些代码』」；账本 W10（随 PR #151 交付）。
- 本文档只做契约增量分析，**不改变任何现行行为**。按 CONTRIBUTING §4，真正的契约变更须以本分析为基础走单独契约 PR，实现 PR 不得夹带。

## A. 增量一：子代理深度>1

### A.1 原方案（现行契约）

- **C4**：`dispatch.subtask_depth_exceeded` 语义为「深度不为 0 的任务不能再委派：委派只支持一层」（PRD §5.4.1 拒绝编号表 832 行；`schemas/c4/rules.json` 165 行同文；`src/tianshu/rules.py` `RULE_IDS` 封闭集含此编号）。
- **事件 payload**：`task.created.depth` 是封闭枚举 `_enum(0, 1)`（`src/tianshu/event_schema.py` 981 行），注释「子任务的 depth 恒为 1」。
- **产生路径**：`Dispatcher._authority_subtask` 门 3（`src/tianshu/dispatcher.py` 3060–3067 行）在弹批准**之前**拒绝；`TaskStore.create_subtask` 2010–2013 行存储层二次拒绝；`SUBTASK_MAX_DEPTH = 1`（`src/tianshu/tasks.py` 215 行）；子任务创建恒写 `max_subtasks=0` + `depth=1`。
- **测试锚**：`tests/test_g2_delegation_spawn.py::test_depth_one_task_cannot_delegate`、`test_child_task_is_read_only_and_depth_one`；`tests/test_rule_ids.py` 把 `RULE_IDS` 与 `schemas/c4/rules.json` 绑在一起（含「有无真实产生路径」归类）。

### A.2 新方案（提案，待契约 PR 定稿）

1. 深度上限从固定 1 变为**有限层级**：建议 `SUBTASK_MAX_DEPTH` 提升为常量 N（N≥2 的具体值与预算联动，见 A.3）；`task.created.depth` 枚举扩为 `_enum(0..N)`。
2. `create_subtask` 的子任务字段改为：`depth = parent.depth + 1`；`max_subtasks` 不再恒为 0，而是**由父任务的委派能力逐层递减**（根任务 `max_subtasks` 是整棵子树的预算池口径，或改为每层独立声明——两个子方案需在契约 PR 里二选一，本文不替契约 PR 做决定）。
3. `dispatch.subtask_depth_exceeded` 语义改写为「深度已达上限的任务不能再委派」（`rules.json` 描述同步）；编号**不改名**（历史日志兼容，符合「历史编号不改名」先例）。
4. 深度与既有门的交叉：门 2（`c4.subtask_not_configured`）与门 3（深度）的先后、`_check_subtask_quota` 的「每根任务累计 4」是否改为「每根子树累计」，必须在契约 PR 里明确。
5. 子 Run 的记忆写入恒拒（`dispatcher.py` 2821 行按 `task.depth or run.parent_run_id`）在深度>1 下**保持不变**（任何深度的子 Run 都不得写记忆）。

### A.3 为什么（动机与约束）

- 账本 W10 的缺口出处是 KNOWLEDGE §7 G2 行「仍不成立：子任务只读、深度 1、单机同 Worker 池…」——深度 1 是已知能力边界，不是安全属性；放开到有限层级配合既有逐次人工批准（每次 spawn 都要人批）与共享预算（`subtasks` 维度预占），风险面在既有门内。
- **不能静默放开**：深度语义同时写在 C4 编号表、`rules.json`、`event_schema` 封闭枚举三处，任何一处单独改都会被 `test_rule_ids.py` / `test_event_schema.py` 抓红——这正是封闭集纪律的目的，所以必须走契约 PR 三处同改。
- 旧库兼容：已存在的 `depth=1` 行必须继续可读；新枚举只加不减，`detect_version` 的 v0/v1/v2 口径（低磁盘 T1 先例：format_version 严格整数）不受影响。

### A.4 影响哪些代码 / 测试

| 对象 | 变更 |
|---|---|
| `src/tianshu/rules.py` + `schemas/c4/rules.json` | `dispatch.subtask_depth_exceeded` 描述改写（编号保留）；PRD §5.4.1 表同步 |
| `src/tianshu/event_schema.py` | `task.created.depth` 枚举扩层；`subtask.spawned` payload 如需记录「本层上限」则加键不加类型 |
| `src/tianshu/tasks.py` | `SUBTASK_MAX_DEPTH`、`create_subtask`（depth 递推 + max_subtasks 口径）、子树累计限额 |
| `src/tianshu/dispatcher.py` | `_authority_subtask` 门 3 判定式（`depth >= SUBTASK_MAX_DEPTH`）；`_check_subtask_quota` 子树口径 |
| `docs/PRD-v1.md` | 832 行编号语义、§7 P2/G2 相关陈述 |
| 测试 | `test_g2_delegation_spawn.py` 两条深度锚改写；新增第 2 层 spawn→再 spawn 端到端、预算父子链跨两层、取消传播跨两层、fence 跨两层；负控：拆掉新深度判定 → 越层委派不被拒 |
| 验收 | 两层委派的共享预算竞争（真并发 + 屏障）、crashpoint 恢复（父/孙两级收尾幂等）、replay 跨两层 |

## B. 增量二：fork 式（继承上下文）

### B.1 原方案（现行契约）

- PRD §7 P2 1076 行：「子 agent 双模式：`isolated`（只收任务单）/ `fork`（继承上下文），默认 isolated」——**定义在，未实现**。
- 现行 spawn 只有 isolated：子 Run 首轮消息 = 子 Task 的 goal（`run.start.payload["task"]`）；replay 只读**子 Run 自己的事件**重建消息序列并核 `messages_hash`（`src/tianshu/replay.py` 136–174 行）。
- 红线 3：上下文是可再生视图；红线 1：事件日志 append-only。

### B.2 新方案（提案形状，待契约 PR 定稿）

1. `subtask.spawn` 增加 `mode: isolated|fork`（缺省 isolated）。**fork 只继承上下文，绝不继承权限**：子 Task 的绑定/白名单/记忆作用域判定与 fresh 完全一致（隔离的是授权面，共享的是只读上下文材料）。
2. 继承材料必须**先持久化再编译**（§7.1 摘要先例）：fork 边界 = 父 Run 的一个 `seq` 上界；子 Run 把「父上下文视图 + source_seqs + budget_version」作为派生材料落进**子 Run 自己的事件流**（`run.start` payload 加键，或等价的已注册事件内 payload 键——**不新增事件族**，与记忆工具「不新增事件族」先例同口径）。
3. replay 只读子 Run 事件 + 内容引用重建 fork 初始上下文，`messages_hash` 校验不变；刷新 fork 上下文 = 追加新版本，不改历史。
4. fork 上下文的 token 计入子 Run 自己的预算账户（§5.6/§5.7 口径），不冒充可验证上界。

### B.3 为什么（动机与约束）

- fork 的价值是省去子任务重复读父上下文的轮次；但「继承上下文」若实现成运行时跨 Run 编译（不落盘），崩溃恢复与 replay 将无法重建 `messages_hash`——红线 11（崩溃后无法判定状态 = bug）与红线 3 会同时被破坏。所以**持久化边界是 fork 可实现的前提**，而它需要事件 payload 结构变更（加键）。
- 权限不继承是硬约束：子 Task 的授权字段在创建时定死（现行 `create_subtask`），fork 只改「首轮上下文里有什么」，不改「能做什么」。父 Run 不得代批子 Run 的审批（W10 SPEC §2.3）在 fork 下同样成立。

### B.4 影响哪些代码 / 测试

| 对象 | 变更 |
|---|---|
| `src/tianshu/tools.py` | `subtask.spawn` schema 加 `mode`（与 A 的版本变更可同版本或独立版本，契约 PR 定） |
| `src/tianshu/tasks.py` | `create_subtask` 接收 fork 边界（父 Run seq 上界）并落 `run.start` payload |
| `src/tianshu/event_schema.py` | `run.start` payload 新键（加键不加类型）；`subtask.spawned` 如需记 mode 则加键 |
| `src/tianshu/loop.py` / `context.py` | 子 Run 首轮编译读取已持久化的 fork 材料；预算记账 |
| `src/tianshu/replay.py` | fork Run 的初始消息重建分支 |
| 测试 | replay 重建 fork 上下文逐字节一致；fork 子 Run 授权面与 fresh 一致（负控：fork 后尝试越权 → 拒绝编号不变）；断网/崩溃恢复后 fork 材料不重算 |

### B.5 为什么本 PR 不实现

1. 事件 payload 结构变更（`run.start` 加键）按工单红线不夹带进实现 PR。
2. PRD 把双模式定义为 P2(v2)；W10 的实现范围判定权在本 SPEC §0，fork 的授权风险低但 replay/恢复正确性验证面大，值得独立工单按契约 PR → 实现 PR 分步交付。

## C. 交付顺序建议

1. 本分析随 W10 实现 PR 交付（单独 `docs(contract):` commit，不夹带代码）。
2. 契约 PR：按本文四段式改 PRD §5.4.1（深度语义）与 §7（双模式），同步 `rules.json` / `event_schema` / `schemas/`（#141/#146 合并后）。
3. 实现 PR：深度>1 与 fork 各自独立（或合并为一个「子代理深度」实现 PR），按 §A.4 / §B.4 的测试面交付，负控随附。
