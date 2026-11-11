# G1 通用执行基础的施工规格（SPEC-G1-ADMISSION-2026-11-11）

- 立据：`docs/PRD-v1.md` §5.2.3（通用工具准入）、§5.6（Limits 与预算账本）、§11.1 G1
  （新增网络 / MCP 前；并发委派前完成共享账本）。上一轮交付对账
  （`docs/PRD-REVIEW-DELIVERY-2026-11-11.md` §4）已把这两条登记为**未实现**，本规格是
  第一份把它们拆成可验收工作流的文件。
- 基线：`main @ ffb9b53`（PR #90 之后）。落笔时实测
  **1536 passed / 7 skipped**、`ruff check .` 干净、运行时依赖仍为零
  （`pyproject.toml` 的 `dependencies = []`）。数字只对这次实测负责；各 PR 收工时自己重跑。
- 判定口径（沿用 `docs/SPEC-PRD-REVIEW-2026-11-11.md` §0）：
  **有回归测试、且做过负控（把修复回退后测试会红）才算做完**。
  **要求写进 PRD ≠ 已实现**；判定不了 = unknown + 阻断。
- 范围：§5.2.3 与 §5.6 中**可在本机、无新运行时依赖、不依赖网络 / MCP / 子代理 / Windows
  实机**交付的部分。网络目标 / MCP 工具 / 子任务资源种类、联网外发许可、内容引用分页
  读取等依赖 G2 的项，按 §6 登记为本轮不做。

## 0 施工纪律

1. 只在自己的 worktree 里操作，禁止在主检出 `/Volumes/Date/天枢Agent` 跑 `git` 或改文件。
   本规格的 worktree 是 `/Volumes/Date/ts-wt-g1`，分支 `feat/g1-tool-admission`。
2. **不加第三方运行时依赖**；不抬 Python 版本；不改 `.github/workflows` 的必过检查。
3. 一提交一件事：`feat:` / `test:` / `docs:` 分开，提交必须有正文（改了什么、为什么、
   判据、负控摘要）。
4. **不许改判据让自己变绿**：既有断言只能因为「新要求改变了语义」而改，且必须在 PR body
   写明旧口径、新口径与理由。
5. 收工前：`PYTHONPATH=<worktree>/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest
   -o addopts="" -q` 全绿；`/Volumes/Date/天枢Agent/.venv/bin/ruff check .` 干净。
6. 干不完就如实报：完成了什么、卡在哪、哪些宣称仍然不成立。

## 1 要求 → 现状 → 本轮交付

| PRD 要求（原文要点） | 现状（代码事实） | 本轮交付 |
|---|---|---|
| 「执行侧验证所声明的**完整** schema，包括嵌套对象、数组和约束」（§5.2.3） | `tools.validate_args()` 只校验顶层 `type / required / additionalProperties`；`properties` 里出现的 `items`、`minItems`、`minimum`、`maximum` **从未被执行**，`run_command` 的 `argv` 可以传 `[1]` 而不报错 | `toolschema.py`：封闭关键字子集的编译 + 嵌套校验（带 JSON 路径）+ 约束执行 |
| 「不支持的 dialect / 关键字**拒绝准入**，不忽略，不自动联网解析外部引用」 | 注册期**零校验**：`ToolSpec.input_schema` 是裸 dict，写 `oneOf` / `$ref` / `format` 会被静默忽略，等于让模型面对一个执行侧不认的契约 | 注册期编译：未知关键字 / 非 bool `additionalProperties` / 非 object 根 / `required` 指向未声明属性 / 非法 dialect 一律 `ToolSchemaError`，报出工具名与 JSON 路径 |
| 「任务绑定固定**工具版本、schema 与描述摘要**；更新不得热替换既有批准」（§5.2.3） | `ActionRequest` 已含 `tool_version`、`schema_hash`；**描述摘要没有进摘要**——改一句工具描述，旧审批照样匹配 | `ActionRequest.description_hash`（非空才进规范字典，历史请求摘要逐字节不变）+ 派发时传入；改描述 → 旧批准不再匹配 |
| 「执行上下文由系统提供 task/run/call/attempt、deadline、取消状态和预算账户，模型不可自行指定这些权限身份」（§5.2.3） | 参数里塞 `run_id` / `attempt_id` 会被 `additionalProperties: false` 拒掉，但这是**碰巧**；注册期没有任何规则禁止声明这些属性 | `MODEL_FORBIDDEN_ARGS` 封闭集：注册期拒绝声明、执行期拒绝传入（即使某工具放宽了 `additionalProperties`） |
| 「无 workspace 的研究任务合法，**URL 不伪装成文件路径**」（§5.2.3） | `resources.py` 已把 `xxx://` 交给 `parse_resource()` 且未知 scheme 拒绝；但**路径参数**里给 `https://…` 会被 `Workspace.resolve()` 当相对路径拼进工作区 | 路径参数解析前显式拒绝 URI 形状（`ToolArgumentError`），不静默拼路径 |
| 「replay 只读已保存结果、不访问当前网络或工具清单」（§5.2.3 验收） | `replay.py` 只读事件库；但没有测试钉住「工具清单变了也不影响回放」 | 回放证据测试：改 `SPECS` 描述/删除工具后，历史 run 仍能通过 replay |
| 「请求发送前在共享持久化账本中**原子预占**额度；每个真实 attempt 有独立身份，按最终 usage **幂等结算**」（§5.6） | `loop._Progress` 只有单 Run 的累加计数（`budget_ledger` 存在游标里），没有预占、没有 attempt 身份、没有跨进程/跨 run 的原子性 | **下一 PR（G1b）**，见 §3 |
| 「未知费用保留未决占用，不按零结算」（§5.6） | 已有 `unknown_usage_calls` / `unknown_cost_calls` 计数，但只是计数，不阻断也不留占用 | **下一 PR（G1b）** |

## 2 G1a：§5.2.3 通用工具准入（本 PR）

### 2.1 封闭的 schema 子集

新增 `src/tianshu/toolschema.py`。**方言就是本文件列出的关键字，多一个都不认**：

| 位置 | 允许的关键字 |
|---|---|
| 根 | `type`（必须是 `object`）、`properties`、`required`、`additionalProperties`（只允许 `bool`，不支持 schema）、`title`、`description` |
| 子对象 | 同上 |
| 数组 | `type: array`、`items`（单个 schema）、`minItems`、`maxItems` |
| 标量 | `type: string|integer|number|boolean`、`enum`、`minimum`、`maximum`、`minLength`、`maxLength`、`pattern`（`re` 编译失败即注册期拒绝） |

明确**拒绝**（注册期，不是运行期）：`oneOf` / `anyOf` / `allOf` / `not` / `$ref` / `$defs` /
`definitions` / `$schema` / `format` / `multipleOf` / `uniqueItems` / `patternProperties` /
`dependencies` / `if` / `then` / `else` / `default` / `examples`。理由写进文档：这些要么
需要外部解析（`$ref`，PRD 明确禁止联网解析），要么需要一套我们没实现的语义；静默忽略
等于对模型撒谎，说「这个参数合法」而执行侧根本不看。

编译产物 `CompiledSchema` 暴露：
- `validate(args) -> dict`：返回原对象；错误消息带 JSON 路径与「期望 / 实际」，例如
  `参数 argv[0] 期望 string，实际 integer`；
- `digest`：`sha256(canonical_bytes(schema))`（与派发侧 `schema_hash` 同一个算法）。

`validate_args(args, schema)` 保留为公开入口（`loop.py` / `memory_exec.py` 在用），内部改成
「编译（带缓存）+ 校验」，因此调用方一行不改，而行为了执行侧校验。

### 2.2 注册期拒绝

`tools.validate_registry()` 逐个编译 `SPECS`，失败抛 `ToolSchemaError`（继承
`TianshuError`），带工具名、JSON 路径与关键字。模块导入时对**内置**注册表跑一遍
（`_compiled_registry = _validate_builtin_registry()`），所以「注册了一个我们没有的
关键字」在 import 时就是红的，不会等到模型调用才暴露。测试对 `SPECS` 打补丁后必须显式
`validate_registry({...})`，不给「运行时偷偷放行」留后门。

### 2.3 工具身份绑定与旧批准失效

- `ToolSpec` 增加 `description_digest` 与 `identity_digest`（`{tool_id, version, schema 摘要,
  description 摘要}`）。
- `ActionRequest` 增加 `description_hash: str = ""`；`to_canonical_dict()` **只在非空时**写入
  该键。于是历史请求（库里的旧审批 / grant / 派发记录）摘要逐字节不变，新请求多绑一层。
  这条设计取舍照抄 `resources.py` 的 scheme-prefix 决策：**不能为了新能力把既有授权链作废**。
- 派发侧（`dispatcher.py` 两个 `ActionRequest(...)` 构造点）传
  `spec.description_digest`。效果：改工具描述 → 重建请求的 `action_digest` 变化 →
  `ApprovalStore.find_for()` 匹配不到旧批准 → 必须重新审批。负控：把描述改回去，又匹配上。

### 2.4 执行上下文由系统提供

`MODEL_FORBIDDEN_ARGS`（封闭集）：`run_id` / `task_id` / `call_id` / `attempt_id` /
`job_id` / `dispatch_id` / `claim_epoch` / `lease_epoch` / `goal_revision` /
`owner_instance_id` / `approval_id` / `grant_id` / `budget` / `budget_account` / `deadline`。

- 注册期：任何 `ToolSpec.input_schema.properties` 命中该集合 → `ToolSchemaError`；
- 执行期：`validate_args()` 命中 → `ToolArgumentError`，即使该工具的 schema 声明了
  `additionalProperties: true`（封闭集优先，防止将来放宽时把身份字段带回来）。
- `ToolContext` 已带 `run_id` / `call_id` / `admitted`（含 `attempt_id`）；文档写明「这些
  字段只能来自系统，参数里出现即视为越权尝试」。

### 2.5 资源种类与 URI 不伪装成路径

- 既有：`resources.RESOURCE_KINDS = {file, memory}`，未知 scheme 拒绝。
- 新增：路径参数（`path` / `name` / `cwd` 等）在解析前拒绝 `^[A-Za-z][A-Za-z0-9+.-]*://`
  形状，错误信息说明「URL 不是文件路径」（不给「拼进工作区变成一个奇怪目录」的后路）。
- 文档写明：`network://` / `mcp://` / `subtask://` 属于 G2，届时必须同时补
  `RESOURCE_KINDS`、解析器、外发许可与 §8.1.1 隔离门禁，不得直接复用 file 分支。

### 2.6 replay 只读证据

`tests/test_g1_admission.py` 用一条真实 run：回放通过后，把 `SPECS` 里被调用工具的
`description` 与 `input_schema` 改掉、再从 `SPECS` 删掉该工具，replay 仍然通过——证明
回放读的是事件里保存的 args / 产物哈希，不是当前工具清单。

### 2.7 本 PR 明确不做

- 网络 / MCP / 子任务资源种类的实现（G2）。
- 「大结果以有界摘要 + 内容引用返回，分页读取引用验证任务可见范围」：现有 `read_text`
  只截断 + `truncated` 标记；内容引用分页读取要一起动 context / blob / 工具契约，单独立项。
- 私有内容外发许可：当前**没有外发面**（无网络工具），只能登记为「无从违反」，不写「已验证」。

## 3 G1b：§5.6 共享预算账本（下一 PR）

模块 `src/tianshu/budget.py`，建在事件库同一个 SQLite 文件上（与既有单提交点决策一致）：

- 表 `budget_accounts(account_id, parent_id, scope, limits_json, spent_json, created_at)`
  与 `budget_reservations(reservation_id, account_id, attempt_id, run_id, call_id,
  dimensions_json, state ∈ {pending, settled, released}, created_at, settled_at)`；
  唯一键 `(account_id, attempt_id)` 保证 attempt 身份唯一。
- `reserve(account, attempt, need: dict[str, int])`：单条 `UPDATE … WHERE` 原子扣减
  「已用 + 预占 + need ≤ limit」，返回预占 id；额度不足返回 `BudgetExhausted`（不抛
  「未知」）。维度：`tokens_in` / `tokens_out` / `requests` / `bytes` / `micros` /
  `subtasks` / `deadline_ms`。
- `settle(reservation_id, usage, *, known: bool)`：幂等（同 id 重复调用只有第一次生效，
  后到的回执不重复扣费也不重复释放）；`known=False` 或 usage 缺字段 → **保留未决占用**，
  记 `unknown` 计数，不按零结算。
- 父子：子账户 `parent_id` 指向父；`reserve()` 沿链同时对每一级扣减（同一事务），
  任一级不足即整体拒绝；父汇总子结果不二次计入（结算只发生一次）。
- 取消 / deadline：`cancel(account)` 把状态传播到所有未结算预占，之后 `reserve()` 一律
  拒绝（`BudgetCancelled`），已发送请求只如实报告「已生效 / 未知」，不当作退款。
- 崩溃核对：`reconcile()` 把 `pending` 且对应 attempt 有执行证据的保留为未决；无证据的
  也不清零，标记 `unverified` 并保留占用。
- 接线：`loop.run()` 的每次模型 attempt 前 `reserve(估算)`，拿到 usage 后 `settle(实际)`；
  预算耗尽 → 只保存状态并通知，**不再发起任何模型调用**（含重试 / 摘要 / 路由）。
- 验收测试：多线程抢最后一份额度只放行一次；重复结算不重复扣；预占后「崩溃」可核对；
  未知费用不记零；父额度耗尽时子账户与摘要调用均拒发；取消确认后无新请求。

## 4 测试与负控计划（G1a）

| 测试 | 钉住什么 | 负控（回退后必须红） |
|---|---|---|
| `test_nested_invalid_args_rejected_before_dispatch` | `run_command.argv=[1]`、`read_text.path=123` 执行前被拒，消息带路径 | 回退到旧 `validate_args` → 不报错 |
| `test_schema_min_items_and_bounds_enforced` | `minItems` / `minimum` / `maximum` 真的执行 | 同上 |
| `test_registry_rejects_unsupported_keywords` | `oneOf` / `$ref` / `format` 注册期报错并指出关键字 | 回退 → 静默通过 |
| `test_registry_rejects_identity_properties` | schema 里声明 `run_id` 等 → 注册期拒绝 | 删除封闭集检查 → 通过 |
| `test_description_change_invalidates_old_approval` | 改描述后旧批准不再匹配；改回去又匹配 | 不传 `description_hash` → 仍然匹配 |
| `test_uri_like_path_is_not_a_file` | `read_text(path="https://…")` 报「不是文件路径」 | 回退 → 被拼成工作区内路径 |
| `test_replay_ignores_current_tool_registry` | 改/删 `SPECS` 后 replay 仍通过 | 让 replay 读 `SPECS` → 红 |
| `test_legacy_request_digest_unchanged` | 无 `description_hash` 的旧请求摘要与钉住值一致 | 无条件写入该键 → 旧摘要变化 |

## 5 合并顺序

1. **PR A（本规格 + G1a）**：分支 `feat/g1-tool-admission`，提交拆成
   `docs(spec):` / `feat(admission):` / `test(admission):`。
2. **PR B（G1b 预算账本）**：分支 `feat/g1-budget-ledger`，从 PR A 合入后的 `main` 派生。
3. 两个 PR 合完后再出一个 `docs(g1):` PR：把实测数字写进
   `docs/PRD-REVIEW-DELIVERY-2026-11-11.md`、`docs/KNOWLEDGE.md`、`README.md`。

## 6 本轮明确不做（如实登记）

- 网络目标 / MCP 工具 / 子任务三种资源种类与各自的外发许可（依赖 G2）。
- 内容引用分页读取、`read_ref` 类工具。
- 跨进程的**真并发**预算竞争只在本机多连接 / 多线程下验证；Windows 命名管道场景（issue #50）
  不在本轮。
- §8.1.1 隔离门禁对新增能力的适用（本轮不新增高自主执行能力，因此无新增门禁项）。

## 7 验收清单（PR 收工逐条回报）

- [ ] `pytest` 全量通过，数字如实记录（含 skip 数与平台）。
- [ ] `ruff check .` 干净。
- [ ] 每条新增测试都写出负控结果（回退哪一处、红了哪条）。
- [ ] 既有判据未被改动；若有改动，PR body 写清旧口径 / 新口径 / 理由。
- [ ] 文档更新：本规格、`docs/tool-admission.md`；不改 PRD 正文（PRD 是要求，不是实现记录）。
