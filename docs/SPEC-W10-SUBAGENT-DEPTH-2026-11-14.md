# SPEC · W10 子代理深度（写权限 / 深度>1 / fork 式）

- 日期：2026-11-14 · 基线：main@88ec0ab · 分支：`feat/subagent-depth-write`
- 账本行（`docs/ROADMAP-COMPLETE-AGENT-2026-11-14.md`，随 PR #151 交付）：「子代理深度：写权限 / 深度>1 / fork 式 | 缺口出处 §7 G2 D 系 | 契约影响：C2/C4 评估 | 冲突面 loop.py / worker.py（热）| 外部阻塞无」
- 性质：**授权面改动，高危**——按 AGENTS.md「三、流程门禁」，合并前必须独立复审（验收项见 §7）。

## 0. 实现范围判定（先给结论）

| 缺口 | 判定 | 一句话理由 |
|---|---|---|
| **子代理写权限** | **本 PR 实现** | 不需要动 C4 拒绝编号、`schemas/`、事件 payload 结构或事件类型封闭集；唯一契约面是 C2 工具 envelope（`subtask.spawn` 加可选 `write` 参数 + 版本 1.0→2.0），这是红线 8 既有纪律内的版本化变更（先例：PRD §5.2.2 `memory.propose` 2.0 在实现 PR 里落地） |
| **深度>1** | **只交契约分析，不实现** | C4 拒绝编号 `dispatch.subtask_depth_exceeded` 语义（PRD 832 行）+ `event_schema.py` `task.created.depth` 封闭枚举 `_enum(0, 1)`（981 行）都要改——契约变更必须单独 `docs(contract)` 提交，实现留待契约 PR 合并后 |
| **fork 式（继承上下文）** | **只交契约分析，不实现** | 子 Run 的初始上下文要能被 replay 从子 Run 自己的事件流重建（messages_hash 校验），继承材料必须落进子 Run 的事件（`run.start` payload 加键或等价物）——事件 payload 结构变更按本工单红线不夹带；且 PRD 1076 行把双模式定义为 P2(v2) |

契约分析文档：[CONTRACT-C4-SUBTASK-DEPTH-2026-11-14.md](CONTRACT-C4-SUBTASK-DEPTH-2026-11-14.md)（深度>1 与 fork 两个增量，四段式，独立 `docs(contract):` commit）。

## 1. 现状代码事实（读到的，不是猜的）

1. **子任务字段在创建时定死为只读**（`src/tianshu/tasks.py` `create_subtask`，1983–2097 行）：父任务全部绑定逐个改写 `mode="readonly"`（2030–2038 行）、无 exec 策略、`memory_scopes=[]`、`outbound_allow=()`、`mcp_servers=[]`、`max_subtasks=0`、`depth=SUBTASK_MAX_DEPTH(=1)`；子 Run `execution_policy=PURE_ONLY`（2067 行）。
2. **深度门在两处**：派发层 `dispatcher.py` 3060–3067 行（`_authority_subtask` 门 3，`dispatch.subtask_depth_exceeded`）与存储层 `tasks.py` 2010–2013 行（`create_subtask` 直接 `TianshuError`）。
3. **事件 schema 钉死深度封闭集**：`event_schema.py` 981 行 `"depth": _enum(0, 1)`（`task.created` payload），977 行注释「子任务的 depth 恒为 1」。
4. **C4 编号语义**：`rules.py` / `schemas/c4/rules.json` / PRD 832 行——「深度不为 0 的任务不能再委派：委派只支持一层」。
5. **PURE_ONLY 的三重只读效果**：工具清单过滤（`loop.py` 737–745、806–807 行：非 pure 工具不进模型清单）；派发时 `require_effects_allowed` 拒绝（`tasks.py` 1059–1061 → `dispatch.run_effects_disallowed`，`dispatcher.py` 1221–1240 行）；绑定本身只读。
6. **记忆写入对子 Run 恒拒**：`dispatcher.py` 2821–2824 行（`task.depth or run.parent_run_id` → `c4.memory_scope_denied`）。
7. **审批与 Run 绑定是结构性的**：`permissions.py` 审批表含 `run_id` 列（369、397–403 行）；`find_for` 按 `request.run_id` 过滤（1139–1160 行）；`consume` 校验 Run 绑定（1061–1137 行）；`c4.approval_run_mismatch` 编号已存在。否决按 `(run_id, subject_digest)` 作用域（1186–1215 行）。
8. **派发 = 判定点唯一**：`loop._advance` 2099–2123 行构造 `ActionRequest` → `dispatcher.prepare`（判定 + 审批）→ `dispatch`（1010–1087 行：同一事务里 `_authority` 执行侧重判 → `_consume_authority` 消费审批 → `tool.intent` 落库）→ 执行守卫内执行 → result + Receipt。父 Run 与子 Run 走**同一条代码路径**（worker 只认领作业，`worker.py` 12–13 行模块注释明确「不许自己写文件」）。
9. **子 Run 由既有 Worker 驱动**（`worker.py` 392–433 行 `_drive`）：`RUN_WAITING_APPROVAL` / `RUN_PAUSED` 的 Run worker 不碰（399–407 行），交给人决定；驱动用的 engine 与生产装配同一份（`_engine`，355–381 行）。
10. **恢复认领**：`loop.py` `_claim_for_resume` 3144–3148 行——`waiting`（等人）作业由显式 `run resume` 直接认领，不经轮询，避免审批被作废。
11. **租约互斥**：`leases.py` 421–448 行「同一棵树不能被两个持有者同时写」；stale 接管必须过 `takeover_reconcile`（`dispatcher.py` 2161 起，`loop._acquire_leases` 749–779 行）。父 Run 挂起等子任务时**不释放**租约（`_park_for_children` 只 park 作业，1063–1102 行）。
12. **`subtask.spawn` 工具 envelope**（`tools.py` 1117–1150 行）：version `"1.0"`，description 写明「派生一个**只读**子任务…不会弹嵌套审批」，schema `additionalProperties: false`，参数只有 `subtask_id` / `goal` / `max_turns` / `max_tokens`。
13. **PRD 钉死的对账面**：832 行（深度=1 编号语义）、1076 行（isolated/fork 双模式是 P2 定义）、1307 行（`RU-D1@1.0.0` 验收样例：子任务写文件/出网被拒、规则编号进事件）、526–528 行（D3 是评测证据，子任务仍只读）。
14. **占用面**：在途 PR #140/#141/#146/#150/#151/#152/#154 已逐一比对（`gh pr view --json files`）。本 PR 触碰 `tools.py` / `tasks.py` / `dispatcher.py` + 新测试 + 三份新/追加文档；`loop.py` 本 PR **零改动**（PR #154 在 loop.py 只动 3241–3267 行 `_embedding_provider_from_env` 装配段，与本 PR 无交集）；`worker.py` 零改动；`KNOWLEDGE.md` 只追加条目（合并时保留双方）。

## 2. 设计与授权论证（写权限）

### 2.1 模型可见面：`subtask.spawn` 2.0

`subtask.spawn` 增加可选布尔参数 `write`（缺省 false），版本 `1.0 → 2.0`（红线 8：schema 变化即新工具；在途旧审批因 `tool_version`/`schema_hash` 进入摘要而自动 superseded，不自动升级——这正是该纪律设计的变更通道）。

- `write` 缺省或 false：行为与今天**逐字节一致**（只读子任务、PURE_ONLY），`RU-D1@1.0.0` 评测样例与 PRD 1307 行钉死的行为不变。
- `write=true`：请求「子任务可以写文件」。**授权上限恒为父任务自身**：子任务绑定 = 父任务绑定逐条保留原 mode（父可写的根子任务才可写；父只读的根子任务仍然只读）——子任务永远不可能拿到父任务没有的目录或模式。实现上这不是"新授权"，是把 2030–2038 行的强制只读改写改为**保留父授权**。
- 不给子任务的（即使 `write=true`）：exec（`exec_policy=None`）、出网（`outbound_allow=()`）、MCP（`mcp_servers=[]`）、记忆写入（`memory_scopes=[]` + `dispatcher.py` 2821 行按父链恒拒）、再委派（`max_subtasks=0` + depth 门）。写权限只指**任务绑定目录内的文件写**。
- spawn 审批不变：`c4.subtask_spawn`（人工批准）仍是 spawn 的判定结果；`write` 进入 spawn 的 `args_hash` / 审批 digest——人批准 spawn 时看到的就是"要派一个可写子任务"这件事本身。

### 2.2 子 Run 执行资格：EFFECTS

`write=true` 时子 Run `execution_policy=EFFECTS`（既有封闭集既有值，`run.start` payload 结构不变——`execution_policy` 键本来就在，写既有枚举值）。效果：

1. 子 Run 的模型清单含文件写工具（`loop.py` 737–745 行按 `side_effect_level == "pure"` 过滤，EFFECTS 不过滤——本 PR 不改 loop.py，行为自动成立）。
2. 子 Run 驱动时照常建写前清单（`loop.py` 939–950 行：EFFECTS 且无 intent 才 `init_run`，不改）。
3. 每个写动作在派发时过 `require_effects_allowed` + 磁盘准入 + 效果空间检查（`dispatcher.py` 1221–1240 行，不改）。

### 2.3 授权论证：每一次写动作走哪道门、谁判、在哪判

写权限的每一次调用与父 Run 的写动作**完全同构**，没有任何子代理专属通道。按时间序：

| # | 门 | 谁判 | 在哪判（执行侧结构） |
|---|---|---|---|
| 1 | spawn 能力声明（`max_subtasks>0`）+ 深度 + slug 唯一 + 限额 | 派发层 | `dispatcher._authority_subtask` 门 1–6（弹批准**之前**） |
| 2 | spawn 人工批准（`c4.subtask_spawn`） | 人 | `approvals.decide`（HumanIdentity），模型无审批工具 |
| 3 | spawn 执行时父链核验 + 预算预占 | 执行侧 | `_dispatch_subtask_spawn` 同一事务 `_verify_resource_execution` + `_reserve_subtask` |
| 4 | 子写动作目标解析 + 策略判定 | 派发层 PolicyEngine | `prepare`：绑定覆盖 + `c4.auto`（新建）/`c4.overwrite`/`c4.delete`（需审批）/**`c4.readonly_binding`**（父该根本就只读时——恒不可绕） |
| 5 | 子写动作人工批准 | 人 | 审批绑定**子 Run** 的 `task_id/run_id/goal_revision/lease_epoch/call_id/args_hash/...` 摘要；人 `approvals approve` 后 `run resume <child_run_id>` |
| 6 | 派发时执行侧重判 + 消费审批 + intent 落库 | 执行侧 | `dispatch()` 同一事务：`_authority` 重判 → `_consume_authority`（DB 唯一约束保证一次性）→ `tool.intent` |
| 7 | 守卫内执行 + Receipt | 执行侧 | `execution_guard` + `begin_execution`；result/Receipt 同事务落库，崩溃按 intent 无 result 恢复 |

**结构保证（不是约定）**：

- **父不能代批子**：审批行绑 `run_id`，`find_for`/`consume`/否决缓存全部按 `request.run_id` 过滤——父 Run 的派发请求在子 Run 的批准里查不到（`c4.approval_run_mismatch`），反之亦然。且「批准」这个动作只有人经控制面能做，模型（包括父模型）没有任何工具能批准或消费审批。
- **一次性消费**：`consume` 由数据库唯一约束保证（PRD §5.4），第二次使用 → `dispatch.approval_unusable`。
- **digest 二次核验**：审批摘要覆盖 `tool_version/schema_hash/args_hash/goal_revision/lease_epoch/...`；改参数/改目标 → 摘要变 → `c4.approval_mismatch`；工具 envelope 变（正是本 PR 的 2.0）→ 旧审批自动失效。
- **无「继承父权限」**：子 Run 的授权面 = 子 Task 的绑定/白名单（子 Task 行 + `task.created` 事件是权威记录），与父 Run 的授权判定互相独立；fork 若未来落地也只继承上下文（见契约分析 §B）。
- **无免审通道**：本 PR 没有给任何路径添加「自动批准」「跳过审批」分支；`c4.auto` 仅对「目录内新建」维持既有语义，与父 Run 相同。

### 2.4 为什么不是「grant-intersection 之外还要拒绝 write=true」

父任务没有可写绑定时，`write=true` 的 spawn 不拒绝而是**如实降级**（子任务拿到的就是父的只读绑定）：子任务后续写动作以 `c4.readonly_binding` 被拒、编号进事件——诚实且可审计。给这个组合新造一个拒绝编号 = 新增 C4 封闭集成员（要三处同改 + 契约 PR），本 PR 不做。子任务拿到的实际模式以子 Task 的 `task.created` 事件 `workspaces[].mode` 为权威，不新增 payload 键。

### 2.5 租约与「父挂起时子写」的串行化

父挂起等子任务时仍持有工作区写租约（事实 11）。可写子 Run 认领后 `resume_run` 需要同根写租约：租约未过期 → `LeaseConflict` → worker 交还作业（既有 contended/requeue 路径）；过期 → stale 接管 + `takeover_reconcile`（父挂起时游标已落库、无未决 intent，对账通过）。这是既有「同一棵树同一时刻只有一个写者」纪律的直接延续，不改租约生命周期。测试用注入时钟推进过 TTL（不 sleep）。

## 3. 判据表（每条一条 pytest）

| ID | 判据 | 断言要点 |
|---|---|---|
| W10-c1 | 默认 spawn 与今天逐字节一致 | 不带 `write` 的 spawn：子 Task 全部绑定 readonly、子 Run PURE_ONLY、depth=1、max_subtasks=0、exec/outbound/mcp/memory 全空 |
| W10-c2 | write=true 且父有可写根 → 子任务拿可写绑定 | 子 Task 绑定 mode 与父逐条一致（父可写根→可写，父只读根→仍只读）；子 Run `execution_policy.mode == "effects"`；`task.created` 事件 workspaces 与之一致（无新增 payload 键） |
| W10-c3 | write=true 但父全只读 → 如实降级 | 子 Task 全部 readonly（不得凭空升级）；子 Run 仍 PURE_ONLY |
| W10-c4 | 子写动作未审批被拒（编号钉死） | 可写子 Run 内模型发起覆盖类写 → 子 Run 停 `waiting_approval`、`approval.requested` 落在**子 Run** 名下；未批准就把该派发请求送 dispatch → `c4.approval_state` 且 `effect_state=none_applied`；人不批 → 文件永不变 |
| W10-c5 | 审批一次性消费 | 成功派发后批准为 `consumed`；同一批准二次 `consume` → `ApprovalError`；`find_for` 对 consumed 不可见 → 同内容第二次调用必须**重新**获得一张新批准（旧批准不可复用） |
| W10-c6 | 父不能代批子 | 子 Run 的已批准审批对父 Run 的同内容写请求**不可见**（`find_for` 按 Run 过滤 → None）；父 Run prepare 同内容动作只能得到**自己 Run 的一张新待批**（≠ 子的批准 id）；子的批准保持 approved 未消费 |
| W10-c7 | digest 不符拒绝 | 批准后改参数（写入内容）构造新请求 → 旧批准的 `action_digest` ≠ 新请求的 `action_digest`、`find_for` 为 None、prepare 只能发起新审批——改了内容的动作绝不可能挂在旧批准下执行 |
| W10-c7b | 批准过期拒绝 | 注入时钟推进过审批 TTL 再派发 → `c4.approval_expired`，`none_applied` |
| W10-c8 | 子写动作端到端真的写盘并产出 Receipt | 批准+resume 后文件内容更新；`tool.result(status=succeeded, effect_state=applied)`；Receipt 可独立核对；子任务收束、父被唤醒后 `subtask.result` 正常回填 |
| W10-c9 | write=true 的子任务仍不能出网/MCP/exec/记忆写/再委派 | network.fetch → `c4.outbound_not_allowed`；memory 写 → `c4.memory_scope_denied`；exec → exec 策略拒绝；子任务内 spawn → `dispatch.subtask_depth_exceeded`（深度门不变） |
| W10-c10 | 否决终局作用于子 Run | 人拒绝子 Run 的写审批后同内容重发 → `c4.approval_rejected`（直接拒，不再弹审批） |
| W10-c11 | 深度门与默认只读回归不破 | 既有 `test_g2_delegation_spawn.py` 全绿（尤其 `test_child_task_is_read_only_and_depth_one`、`test_depth_one_task_cannot_delegate`） |
| W10-c12 | 工具 schema 契约绑定 | 注册表 `subtask.spawn` version=="2.0" 且 input_schema 含 `write`（模型清单只投影 name/description/input_schema，版本经 `schema_hash`+`tool_version` 进派发与审批摘要）；非布尔 `write` 在准入层被 schema 校验拒绝；`validate_registry()` 通过 |

## 4. 负控清单（变异 → 必须红）

| ID | 防御 | 变异 | 预期红 |
|---|---|---|---|
| W10-n1 | 父不能代批子（审批绑 Run，两层联动：动作摘要含 run_id/task_id + 查找按 run_id 过滤） | 同时拆两层：`ActionRequest.action_fields` 里去掉 run_id/task_id + `ApprovalStore._find` 去掉 `AND run_id=?`（单拆 SQL 过滤不红——摘要层独立承重，这也是本次负控实测到的结构事实） | W10-c6 红：父 Run 同 call_id 同内容请求的 `find_for` 直接命中**子 Run 已批准的批准**、prepare 变 admitted——审批跨 Run 复用 = 授权面被击穿 |
| W10-n2 | 默认只读（不写 `write` 时恒只读） | `create_subtask` 无视 write 参数、无条件保留父 mode | W10-c1 红（默认 spawn 的子任务拿到可写绑定——违反 RU-D1@1.0.0 钉死行为） |
| W10-n3 | 深度门（既有防御，验证未被本 PR 削弱） | 拆掉 `_authority_subtask` 门 3（depth 判定） | `test_depth_one_task_cannot_delegate` 红：编号化拒绝 `dispatch.subtask_depth_exceeded` 消失，裸 `TianshuError`（存储层 `create_subtask` 的深度门仍兜底拒绝——两层中派发层这扇钉住"弹批准之前、带编号"的语义） |

负控实跑红绿原件见 PR body；还原以 `git diff` 空为准。

## 5. 文档对账点（PRD 是禁改文件——#141/#146 占用）

| PRD 陈述 | 本 PR 后是否仍真 | 处置 |
|---|---|---|
| 832 行 `dispatch.subtask_depth_exceeded`「委派只支持一层」 | 真（本 PR 未改深度语义） | 契约分析文档交付增量方案；实现待契约 PR |
| 1076 行 isolated/fork 双模式（P2） | 真（仍未实现，未宣称） | 契约分析文档给出可行落地形状与前置条件 |
| 1307 行 RU-D1@1.0.0「子任务写文件/出网被拒」 | 真（默认 spawn 不变；该评测不传 `write`） | SPEC §2.1 论证 + W10-c1 判据钉死 |
| 526–528 行「子任务仍只读」 | **部分变陈旧**：默认仍只读，但新增 opt-in 写声明 | 如实登记为「PRD 待更新项」：后续 PRD 维护 PR（#141/#146 合并后）应把该陈述改写为「默认只读；`subtask.spawn` 2.0 起可显式声明文件写（上限=父绑定，逐动作审批）」。本 PR 不改 PRD，不宣称已改 |
| `schemas/c4/rules.json` / `rules.py` 封闭集 | 不变（无新增编号） | W10-c 系全部复用既有编号 |
| `tool_manifest()` / `schemas/` 契约测试 | `subtask.spawn` version 2.0 + 新参数 | 相关契约断言随本 PR 更新（测试侧），`schemas/` 目录零改动 |

## 6. 边界与未决

1. **fork 与深度>1 未实现**：见契约分析文档；两者都不能在本 PR 里偷跑。
2. **父/子同时可写的串行化成本**：父挂起等子任务期间，子 Run 首次写之前要等父租约 TTL 过期（默认 30s）或父显式释放。正确性无缺口（租约互斥 + 接管对账），延迟如实登记为已知行为；是否「父挂起等子时主动释放写租约」留待独立评估（会改租约生命周期语义，不在本 PR）。
3. **`write=true` 不做路径级白名单**：按父绑定整根保留 mode（`outbound_allow` 式的 `write_allow` 是新契约面，未做）。
4. **子任务写权限不做 UI 展示**：审批/事件面已可审计（`approval.requested` 在子 Run 名下），UI 不在本 PR。
5. **真实模型复跑未做**：全部判据走脚本模型（与 D2/D3 同口径），不冒充真实模型分数。
6. **Windows 未验证**：本机门禁 + CI 矩阵为准，不宣称桌面体验。

## 7. 高危判定与独立复审验收项（给第二读者）

**判定：高危**（授权面改动）。按 AGENTS.md「三、流程门禁」，合并前由第二读者按以下验收项逐条对照（不读作者自查结论，给可复现探针证据）：

1. 未审批的子 Run 写动作被拒（未批即派发 → `c4.approval_state`；批准过期 → `c4.approval_expired`），`tool.result(effect_state=none_applied)`，无磁盘变化；e2e 不批准则子 Run 停在 `waiting_approval` 不推进。
2. 审批一次性消费：同一批准二次使用 → `dispatch.approval_unusable`；`consumed` 由 DB 约束背书。
3. 父不能代批子：子 Run 审批对父 Run 请求不可见/不可用；负控 W10-n1 变异证明该判定是 load-bearing。
4. digest 二次核验：改参数后旧批准 → `c4.approval_mismatch`；`tool_version/schema_hash` 进入摘要（2.0 使旧审批失效）。
5. 默认（无 `write`）spawn 的子任务与基线 88ec0ab 行为一致（RU-D1@1.0.0 相关既有判据复跑绿）。
6. `write=true` 的授权天花板 = 父绑定：父只读根 + write=true → 子任务拿不到可写绑定、写动作 `c4.readonly_binding`。
7. 子写动作的事件链完整：`tool.intent`（含 approval_id/grant_id）→ `tool.result` + Receipt → 可回放（replay 子 Run 绿）。
8. 记忆/出网/MCP/exec/再委派在 write=true 下仍被拒（W10-c9 全部编号）。
9. 事件日志 append-only：全链路无 UPDATE/DELETE；`create_subtask` 四件套仍同一事务。
10. 深度门未被削弱：负控 W10-n3 红→绿记录。

## 8. 实测回填（提交前如实填写）

- 基线：main@88ec0ab；本机 macOS（Apple Silicon，Python 3.12，uv 冻结环境）。
- 判据：`tests/test_g2_delegation_write.py` 13 条（W10-c1/c2/c3/c4/c5/c6/c7/c7b/c8/c9×2/c10/c12）全部通过；W10-c11 由既有 delegation 七文件回归承担（82 项绿）。
- 负控三条（W10-n1/n2/n3）：变异态实测红、还原后实测绿，测试名与变异点见 §4；W10-n1 的单拆 SQL 过滤不红、必须连摘要层一起拆——这一结构事实已如实记入 §4。
- 门禁数字（lock/ruff/全量 pytest）：见 PR body（以提交时实跑为准）。
- `loop.py` / `worker.py` / `schemas/` / 事件 payload：零改动（`git diff --stat` 可核）。
