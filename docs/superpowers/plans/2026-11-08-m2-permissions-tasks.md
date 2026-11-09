# M2 权限与任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. 下列复选框为执行清单，不是已经通过的验证记录。

**Goal:** 在已有文件工具、凭据和撤销能力上，实现任务身份、执行侧授权、可恢复审批、跨实例租约和安全点暂停/取消。

**Architecture:** SQLite 中的事件仍是事实源，Task/Run/Approval/Grant/Lease 为同库事务更新的可重建投影。Dispatcher 唯一判定策略；受信任的内置文件执行器独立核验授权、资源身份与 epoch。模型生成和人工等待都在数据库事务及执行锁之外。

**Tech Stack:** Python 3.12、stdlib sqlite3、现有 CLI/文件工具、pytest；本计划不引入另一个 agent 框架。

**Spec:** `docs/PRD-v1.md` §4.3–4.5、§5.2–5.4、§7 F6/F8、§8、§11；`docs/TECH-ARCH.md`；`CONTRIBUTING.md`。

**基线:** `main`，读取时 HEAD `7fd5731`。计划编写与月之亮面的 M0–M1 debug 并行；此文不代替 debug 报告，也不授权跳过其阻塞问题。尚未执行 M2 测试。

## Global Constraints

- 保留 macOS 13+（arm64/x64）、Windows x64/arm64 四目标；原生测试未运行时记录未验证，不能写成通过。
- Task 是用户可见单位，`workspaces=[]` 合法；Workspace 是权限、回滚与资源冲突的边界。零目录不能默认为可访问 cwd。
- 用户决定：单机单用户、限定目录默认可写、诊断仅本地、无付费代码签名。本阶段不改变这些决定。
- 事件 append-only；授权消费、grant、intent 与相关投影必须同一次 SQLite 提交。数据库事务不包含外部文件写入，后者仍靠 M1 的证据与恢复协议。
- 本轮只新增计划。不修改共享分支的源码，不搬动 `.tmp/`。执行阶段使用隔离分支/工作树，禁止在其他 agent 工作时切换共享目录分支。
- 合约变更单独 `docs(contract):` 提交/PR；实现 PR 按本文件三组顺序推进，验收后再进入下一组。
- 本阶段支持受信任内置文件工具与 CLI 控制入口；不新增任意 shell、第三方 worker、模型自批或删除整个目录。Rust Host/认证 IPC/OS 隔离属于桌面接线，不能将 Python 检查宣称为已交付的物理沙箱。

## Review Focus

1. 两个不同 Task、两个 `--db`、目录别名或父子目录不能绕过同一资源互斥：由 Task 2 测试。
2. 人工审批后参数、目标内容、工具版本或目标 revision 改变，旧审批不能执行：由 Task 3/4 测试。
3. intent 提交后到打开文件前改目标，旧授权不得在新 epoch 下首次执行：由 Task 4 测试。
4. 等待审批/暂停时重启，不丢已完成工具、不重复计费重跑已提交 turn：由 Task 5 测试。
5. undo/recover 与常规 run 共用资源守卫；unknown 不能因换 Run、重启或重建投影而消失：由 Task 2/4/5 测试。

## 0. Debug 合流与开工门槛

已读取月之亮面的 `docs/DEBUG-M0M1.md`（审查基线 `7fd5731`），其结论为“不能进入 M2”。以下已按报告编号合流；共享目录随后出现的 `fix/m1-hardening` 分支及修复文件不等于修复已验收。管理员收齐修复证据后才解除相应门槛；此前可以编写测试用例与接口设计，不能声称可以安全扩展写入。

- **B1 证据一致性（S1、中等 M1/M2）**：intent/result/receipt 的中断窗口能恢复，重复 recover 不伪造 applied、不重复生成效果；同内容不能证明写者，未知工具版本和缺失写前证据一律 unknown。未决效果稳定阻断后续写入。
- **B2 撤销与写入完整性（S3/S4、中等 M1、L2）**：恢复/撤销使用正确写前状态；缺快照、重复修改同一路径、撤销中断不会误报全部成功。落实原子替换、持久化顺序与全部 preimage 预检；多文件 undo 出错必须如实记录 partial/unknown。发现可能损坏文件的写入顺序须先修，不能仅改名为 M1.5 后继续依赖。
- **B3 执行入口（S2、中等 M4）**：库函数、CLI、undo/recover 的写入口清点并补齐阻断；新增的 M2 授权不得只包住 CLI。身份/并发修复由 M1 修复与 Task 2/4 共用接口，不另造互相独立的锁或阻断库。
- **B4 回归与模型失败语义（中等 M3、L1）**：provider 错误、未知/截断 finish 不得静默算成功；usage 未知按明确保守策略处理。报告中的同内容文件、modify 目标消失、缺 receipt/manifest/blob、写中中断及恢复/撤销交叉反例加入回归。重新运行 `uv run pytest` 和 `uv run ruff check .`，记录真实数量与错误。当前 dev 依赖只声明 pytest；若 ruff 缺失，作为首个实现 PR 的工程依赖补入锁文件，不跳过检查。

`os._exit` 验证仅是进程中断证据，不证明断电安全。Windows、真实断电的未验证状态应继续列在报告中。Debug 只阻断依赖受影响能力的工单；无需让无关 schema/测试设计停工。

## 1. 已读实现与文件归属

- `events.py`：现为 autocommit `EventLog.append()`；加入可组合事务接口，禁止各服务自行新开连接提交半份状态。
- `loop.py`：现为一次 `run(task, workspace_dir, ...)` 同步循环；拆出持久 cursor 与调度控制，不重写模型 adapter。
- `tools.py`：保留参数校验及工具实现；将外部可调用执行口收口到 Dispatcher，handler 不再独立取得调用方提供的 Workspace 权限。
- `workspace.py`：保留目录校验，增加稳定资源身份与多目录绑定，不以显示用的 Unicode/大小写折叠结果作为授权身份。
- `recovery.py`、`snapshot.py`、`receipt.py`：复用 debug 修复后的证据协议，补 workspace/call/attempt 绑定和受控入口。
- `cli.py`：增加 task/approval/run 控制命令；不在命令解析层判定策略。
- 新建 `tasks.py`（Task/Run/cursor）、`leases.py`（资源租约与执行守卫）、`permissions.py`（C4/策略/审批）、`dispatcher.py`（事务派发及独立核验）、`jobs.py`（E1/E3 运行作业）。不为每个类型再建转发模块。
- 精确 JSON Schema 放 `schemas/m2/`，测试按任务分别放 `tests/test_transactions.py`、`test_tasks.py`、`test_leases.py`、`test_permissions.py`、`test_dispatch.py`、`test_run_control.py`、`test_m2_acceptance.py`。

## 2. 实现前固定的接口语义

### 2.1 身份与历史兼容

Task：`task_id, goal, goal_revision, active_run_id, workspace_bindings[]`。
Run：`run_id, task_id, goal_revision, state, outcome?, effect_state, resource_epochs{}, cursor_ref, budget`。调度身份另用 `JobClaim{job_id, run_id, owner_instance_id, claim_epoch}`；claim_epoch 每次认领递增，独立于目标 revision 和资源 epoch。
WorkspaceBinding：`workspace_id, canonical_root, resource_identity, mode, snapshot_policy`。
资源 epoch 由共享租约表管理，不能用 Task 内的 `goal_revision` 代替。一个 Run 可绑定多个资源 epoch；同一物理目录跨 Task 使用同一资源身份。父子目录按重叠资源处理。

旧事件不改写；以 schema_version 区分新负载。旧 run 可只读回放，通过追加 `task.adopted` 为其建立投影；旧 run 继续执行前必须重新授权并获取租约。旧 `run.start` 内的字符串不是自动授予权限的依据。

### 2.2 唯一权威存储与锁

发布 CLI 所有执行命令使用技术架构规定的同一用户数据目录与同一 authority 数据库；工作区不能决定授权库的位置。旧 `--db` 保留为历史只读检查/显式迁入入口，不允许用新数据库绕过已经存在的工作区阻断。测试用应用启动配置注入一个整体 `data_root`，两进程测试共享它。

M2 初版采用一个跨进程 `execution_guard` 串行化**受控文件操作、undo 和派发相关的 epoch 切换**，不在模型请求或人工等待期间持锁。锁位于 authority 数据目录；macOS/Windows 分别实现本机可靠文件锁，进程结束由 OS 释放。此取舍牺牲不同目录的同时写入，减少首版死锁与失效 owner 竞态；以后细化锁不改变授权契约。

租约记录 `resource_identity, owner_run_id, owner_instance_id, resource_epoch, heartbeat_at, expires_at, process_identity`。建议默认 TTL 30 秒、heartbeat 5 秒，作为本计划的配置默认值。TTL 到期不自动证明旧 worker 已停止：接管必须取得 execution_guard、核实 owner，并先核对未决副作用；拿不到锁或效果 unknown 时不接管。PID 相同不足以认定同一进程，身份包含启动标识。

进入实际文件操作前再次验证 revision/epoch/grant/审批/路径/目标前置状态，并登记 `execution.started`；它是“已在执行”的线性化点。目标变更也在同一 guard 内递增 revision/epoch。取消可先持久记录请求，但已经越过该点的有限文件操作按 in-flight 处理，允许完成并核查；取消不承诺撤销已有修改。

### 2.3 C4 与工具动作

请求字段至少为 `request_id, task_id, run_id, job_claim, goal_revision, resource_epochs, call_id, attempt_id, tool_id, tool_version, schema_hash, action, canonical_resources[], args_hash, preconditions, policy_version, prompt_version, contract_version`。`job_claim` 绑定当前 owner 与 claim_epoch，派发及执行前均重新核验；旧 claim 不能仅凭尚未过期的 grant 发起新效果。
Approval 绑定该请求的**动作摘要**及 `approval_id, state, decided_by, expires_at`；Grant 增加 `grant_id, subject, request_digest, remaining_uses, expires_at, issuer`。模型不能指定 `decided_by` 或 issuer。批准来源只能是明确的本地 human CLI 控制路径；不声称防同 OS 用户篡改。

> **执行期修订（2026-11-09，M2 PR C Task 6）**：原写法"Approval 绑定该请求摘要"
> 与本节末"不会形成每次拿新的 epoch、每次审批立即作废的循环"互相矛盾——进程
> 逐命令启动时每个 `resume` 都是新 claim_epoch，请求摘要必变，审批必然重来
> （已实测复现，见 `docs/REVIEW-PRA.md` §4.6）。改为**两个摘要**：
> `request.digest` 含 owner/claim_epoch，用于执行前核验；`Approval.digest` 只含
> 动作字段（见 `docs/PRD-v1.md` §5.4）。审批仍一次性消费、执行仍要求当前
> claim 有效，改目标/改参数/改绑定照样 superseded。影响 `permissions.py`、
> `dispatcher.py`、`loop.py` 与审批相关用例。

摘要采用 RFC 8785 + SHA-256 的版本化函数。现有 `canonical_bytes()` 继续服务旧日志哈希，不直接改写导致历史 replay 失效。新 `action_digest()` 使用独立经过标准向量验证的实现；实现者优先选纯 Python 依赖并固定版本，通过四目标依赖门禁，再用 RFC 向量验收，禁止仅把 `sort_keys=True` 标称为完整 RFC 8785。

- 目录内读取、新建：匹配只读/可写 binding 后自动放行。
- `modify_text` 目前是整文件替换：按 PRD“覆盖同名文件算删除类”的保守规则需要审批；未来局部编辑工具可单独建语义。审批展示 diff/目标与风险，绑定执行前哈希。
- 新增 `delete_file` 只支持已授权目录内的普通文件，审批 + preimage 后删除；无递归删除、无任意 shell。`after_state=absent` 纳入 M1 证据恢复与撤销测试。
- 目录外请求先进入授权请求，不直接穿过 Workspace 校验；批准一次操作不等于永久增加 Task 作用范围。用户显式扩充 workspace binding 会触发 revision/fencing。
- `undo` 属于写/删操作，使用同样授权和租约；不以“用户调用 CLI”隐式取得目录外权力。`recover` 的只读核查与可能触发的修复写入分开，后者必须重新授权。
- 未实现的外发、提权、付款工具不暴露给模型；遇到对应未支持动作明确拒绝，不仅返回一个无法履行的 REQUIRE_ELEVATION。

### 2.4 生命周期与保存点

等待状态保存 `messages_ref, model_turn_ref, pending_proposals[], completed_call_ids[], budget_ledger_ref, approval_ids[], goal_revision, resource_epochs`。不能靠重新请求模型猜回尚未执行的工具。任何 worker 写入 cursor、已提交模型结果、Run 终态及 job heartbeat/finish，均在同库事务核验 JobClaim 的 owner 与 claim_epoch；旧 worker 即使还活着也不得改新 owner 的状态。旧调用若实际已产生效果，交由独立的 M1 核查路径收集证据，不允许旧 claim 自行宣告任务成功。
`recover` 仅核查已派发效果；`resume` 才继续任务。重试失败任务创建新 Run；恢复同一 Run 不清空预算，不重复派发已完成 call。
`WAITING_APPROVAL` 与 `PAUSED` 均不因收到 approve 直接开始文件操作。WAITING_APPROVAL 时仍由存活的作业 owner 保留租约并续 heartbeat，但不持 execution_guard/数据库事务；批准后 resume 使用仍有效的同一 epoch，因此不会形成“每次 resume 换 epoch、每次审批立即作废”的循环。PAUSED 在安全点释放租约，resume 必须重新获取。进程死亡、租约过期、暂停后恢复造成 epoch 改变时，旧请求先 superseded；新 owner 先获取并保持新租约，再展示绑定新 epoch 的审批请求。原先人审不能暗中重新绑定。等待审批占用目录最长受审批 TTL 约束，过期后释放已完成对账的资源并进入 WAITING_USER。
`partial` 且已完全对账可由明确处置解除阻断；`unknown` 未有核查证据不得解除。blocked 是可回放的资源状态，不是一条只在 CLI 启动时检查的字符串。

## 3. 交付顺序：三个 PR，六个可独立验收任务

### PR A — 状态基础与资源身份（Task 1–2）

#### Task 1：可组合事务、Task/Run、0..n 目录

**Files:** Modify `events.py`, `workspace.py`, `replay.py`, `cli.py`; Create `tasks.py`, `schemas/m2/task.json`, `schemas/m2/run.json`; Test `test_transactions.py`, `test_tasks.py`。

**Interfaces:** `EventLog.transaction() -> context manager[EventTransaction]`；`EventTransaction.append(...) -> int`（与当前 append 字段兼容）；`TaskStore(log)`；`create_task(goal: str, bindings: list[WorkspaceBinding]) -> Task`；`start_run(task_id: str, budget: dict) -> Run`；`load_run(run_id: str) -> Run`。此时只创建状态，未接入新写操作。

- [ ] 先写 `test_transaction_rolls_back_events_and_projection`：同一事务写事件和投影后抛错，二者均无记录；异常退出后连接可继续使用。`test_rebuild_task_from_events`：清空投影后重建，字段等于清空前。`test_zero_workspace_has_no_file_access`：空 bindings 不隐式加入 cwd。
- [ ] 运行 `uv run pytest tests/test_transactions.py tests/test_tasks.py`，记录因缺功能失败的断言。
- [ ] 实现 `BEGIN IMMEDIATE` 事务入口与有界 busy 等待；外部显式事务中的 append 不单独 commit，禁止嵌套 BEGIN；事件和投影同连接。事件 seq 是全库序号，单个 Run 在并发下只要求严格递增，不能假设无别的 Run 插入。
- [ ] 完成历史迁移回放、空目录任务、读 A 写 B 的 binding/schema 测试；两目录同名文件的快照与 receipt 不串用。
- [ ] 重跑针对测试、旧 replay 用例和 lint，均通过后提交 `feat(m2): add transactional task and run state`。

#### Task 2：资源租约、authority 与跨进程守卫

**Files:** Create `leases.py`, `jobs.py`（仅存储与认领接口，调度循环在 Task 6）; Modify `tasks.py`, `workspace.py`, `cli.py`; Test `test_leases.py`, `test_job_claims.py`。

**Interfaces:** `LeaseStore(log)`；`acquire(run_id: str, resources: list[ResourceIdentity], owner: ProcessIdentity) -> LeaseSet`；`heartbeat(lease_set: LeaseSet) -> None`；`release(lease_set: LeaseSet) -> None`；`execution_guard(data_root: Path) -> context manager`；`assert_current(run_id: str, revision: int, epochs: dict[str, int]) -> None`。`ResourceIdentity` 在 `workspace.py` 定义规范根路径及可用的 OS 目录标识。`jobs.py` 定义 `JobClaim` 和 `JobStore(log)` 的 `claim_next(owner: ProcessIdentity) -> JobClaim | None`、`heartbeat(claim: JobClaim) -> None`、`finish(claim: JobClaim, outcome: str) -> None`。`tasks.py` 在本任务声明 `RunCursor` 数据类型（字段见 §2.4），提供 `save_cursor(run_id: str, cursor: RunCursor, claim: JobClaim) -> None` 与 `commit_model_turn(run_id: str, turn: dict, claim: JobClaim) -> None`，不能靠调用方检查后无条件写入；Task 5 再接入实际运行与控制行为。

- [ ] 先写 `test_two_processes_one_resource_owner`、`test_parent_child_and_alias_conflict`、`test_changed_db_cannot_escape_authority`、`test_stale_owner_cannot_reacquire_with_old_epoch`。用 subprocess 和 barrier/event 同步，不用 sleep 猜时序；检查只有一个 owner 及资源无重复写。
- [ ] 运行 `uv run pytest tests/test_leases.py tests/test_job_claims.py`，确认失败指向租约/身份能力缺失。`test_old_claim_cannot_mutate_after_takeover` 覆盖旧 worker 活着但过期、新 owner 接管后，旧 heartbeat/finish/cursor/model-result 全部拒绝。
- [ ] 在 execution_guard 内以同库事务原子获取全部 resources 或全部失败；未知/blocked 资源不发写租约。重叠目录同样冲突，暂停/失效进程不能凭旧 epoch 获准。发布 authority 根目录与测试注入分开。Job 的认领/完成同样以 owner+claim_epoch 的条件更新并记事件，Task 5 可直接复用此存储接口，不依赖 Task 6 尚未实现的调度循环。
- [ ] 用显式时钟注入测试 TTL/heartbeat；模拟 PID 复用与活着但过期 owner，确认旧写操作不能在新 owner 获准后恢复。验证 OS 锁在子进程硬退后释放，但 unknown 阻断仍保留。
- [ ] 两平台原生进程测试分别记录，目标未运行不能标全绿；目标测试和全部回归通过后提交 `feat(m2): fence workspace access across processes`。

### PR B — 审批与受控派发（Task 3–4，依赖 PR A）

#### Task 3：策略、绑定摘要和审批存储

**Files:** Create `permissions.py`, `schemas/m2/approval.json`, `schemas/m2/grant.json`; Modify `tools.py`, `cli.py`; Test `test_permissions.py`。

**Interfaces:** `ActionRequest`、`Approval`、`Grant` 定义在 `permissions.py`；`action_digest(request: ActionRequest) -> str`；`PolicyEngine.evaluate(request: ActionRequest) -> PolicyDecision`；`ApprovalStore(log).request(request: ActionRequest) -> Approval`；`decide(approval_id: str, decision: str, actor: HumanIdentity) -> Approval`。`HumanIdentity` 由 CLI 控制端创建，不能来自模型 arguments。

- [ ] 先写动作分类表测试：内读/新建自动；整文件覆盖/删除/目录外操作需审批；未支持动作拒绝。`test_digest_binds_every_authority_field` 逐一改变参数、资源、revision、epoch、版本和执行前状态，摘要必须变化。
- [ ] 运行 `uv run pytest tests/test_permissions.py`，确认红灯。
- [ ] 固定 Approval 状态与迁移：proposed→pending→approved/rejected/expired/revoked/superseded，approved 仅在 Task 4 派发事务中 consumed。自动规则直接生成有审计来源的 grant，不伪造人工 approved。审批 TTL 默认 10 分钟，与可续租的 30 秒 lease TTL 分开；只要 owner 持续续租且 epoch 未变，审批不因一轮 lease TTL 流逝而失效。执行 grant TTL 默认最多 30 秒，签发时不得超过当前资源租约期限，派发前仍须重新核验。
- [ ] 加入拒绝、撤销、过期、模型伪造审批人、批准后目标文件变化、规则优先级测试；canonicalization 用独立 RFC 测试向量，不以实现自身输出当期望值。
- [ ] 定向测试/lint 通过，提交 `feat(m2): persist approval-bound permissions`。仅审批存储完成不代表工具已受保护。

#### Task 4：唯一派发入口、一次消费、所有文件入口接线

**Files:** Create `dispatcher.py`; Modify `loop.py`, `tools.py`, `recovery.py`, `snapshot.py`, `receipt.py`, `cli.py`; Test `test_dispatch.py`。

**Interfaces:** `Dispatcher(log, tasks, leases, policy, approvals, executor)`；`prepare(request: ActionRequest, claim: JobClaim) -> DispatchDecision`（waiting/rejected/admitted）；`dispatch(request_id: str, claim: JobClaim) -> ToolResult`；`FileExecutor.execute(admitted: AdmittedCall) -> ToolResult`。`AdmittedCall` 携带已绑定的 JobClaim，`FileExecutor` 定义在 `tools.py`，独立从 authority 核验其当前有效性，不能仅信任传入对象的 `approved=True`。undo/recover 的修复写建立自己的控制 Run/JobClaim，并单独记录所操作的历史 Run，不能借用旧 Run 的已失效身份。

- [x] 先写 `test_consume_and_intent_are_atomic`、`test_concurrent_approval_consumes_once`、`test_revoke_between_intent_and_open`、`test_direct_execute_requires_admission`、`test_undo_and_recovery_obey_resource_block`；检查拒绝时文件字节不变且留拒绝事件。
- [x] 运行 `uv run pytest tests/test_dispatch.py`，确认红灯。
- [x] 在一个 SQLite 写事务中完成当前状态、JobClaim、resource epoch核验、审批唯一消费、grant 和 tool.intent 提交。`call_id` 是内部逻辑身份，provider ID 另列，attempt 每次派发更新；消费表唯一约束不能跨 run 误碰 provider 的短 ID。
- [x] 在 execution_guard 内、实际打开文件前，再核验 authority、JobClaim、grant来源/绑定/TTL/消费次数、revision/epoch、目录和 preconditions，记 execution.started。撤销/变更先获得守卫则拒绝旧操作；执行先开始则完整记为 in-flight，不承诺取消回滚。拒绝和未知都保留可核查状态。补旧 claim 在接管后直接调用 dispatch/executor 被拒绝的测试。
- [x] 接入四个既有工具及 `delete_file`，并把 undo/recover 的写路径收口；工具只能使用已解析且核验的目标。结果/receipt 提交沿用 B1 的修复协议，不能新造第二份日志。
- [x] 测试“批准后改文件”“undo 同时另一 Task 写”“两个相同调用并发”“epoch 改变前后屏障”“删除后可恢复”；运行全部回归/lint，通过后提交 `feat(m2): enforce permission at every file dispatch`。

> 执行记录（2026-11-09）：`src/tianshu/dispatcher.py` + `tests/test_dispatch.py`（27 例）
> 已落地，全量 273 passed / `ruff check .` 通过。**未完成项详见 `docs/REVIEW-PRA.md` §4**：
> (1) `loop.run()` 仍是 M0 遗留同步循环，写入靠唯一一处显式开关
> `allow_legacy_unauthenticated_write` 放行，Task 5 接上 Task/Run/Job 后删除；
> (2) 目录外写入（策略层给 `approve`）在执行侧明确拒绝为
> `c4.outside_workspace_unsupported`，M2 不支持；(3) CLI 的 `undo --approve`
> 把"人在这条命令上敲 --approve"当作批准，因为审批摘要绑定 claim_epoch，
> 跨进程两次调用会天然换 epoch；(4) 本 PR 仍无 Windows 原生证据。

### PR C — 暂停恢复、用户改目标与作业生命周期（Task 5–6，依赖 PR B）

#### Task 5：保存 cursor 与用户控制

**Files:** Modify `tasks.py`, `loop.py`, `cli.py`, `replay.py`; Test `test_run_control.py`。

**Interfaces:** `request_pause(run_id: str) -> Run`；`request_cancel(run_id: str) -> Run`；`revise_task(task_id: str, goal: str, bindings: list[WorkspaceBinding]) -> Task`；`resume_run(run_id: str) -> RunResult`；复用 Task 2 已定义的 `RunCursor` 存储接口。控制路径先写请求事件，执行器在安全点确认状态。

- [x] 先写 `test_pause_waits_for_current_effect`、`test_approved_while_paused_does_not_execute`、`test_resume_never_repeats_completed_calls`、`test_goal_change_fences_pending_and_prepared_calls`、`test_live_approval_resume_keeps_epoch`、`test_reclaimed_approval_can_complete_after_one_reapproval`；以固定 ScriptedModel 响应加阻塞屏障验证派发次数和文件结果。后两条用注入时钟推进超过 30 秒且小于 10 分钟，模拟 owner 续租或一次接管，断言批准后显式 resume 恰好执行一次，而非反复重审。
- [x] 运行 `uv run pytest tests/test_run_control.py`，确认红灯。
- [x] 在模型请求前后、工具实际派发前后检查控制状态；模型流中取消丢弃未提交工具提议，但已经发生的模型费用仍入账。checkpoint 保存已提交 turn、工具队列、预算与审批引用；PAUSED/WAITING 状态不能记成 run.end 成功。
- [x] CLI 提供 `task create/show/revise`、`run start/status/pause/resume/cancel`、`approval list/approve/reject/revoke`；旧 `run --task --workspace` 是创建 Task 后 start 的兼容入口，不绕服务层。
- [x] 覆盖等待审批时改目标、intent 后改目标、执行中改目标；已发生/未知效果沿用 M1核查，新 Run 在相关资源解阻前不得写。目标更改保留旧 Run 历史；资源绑定改变不扩张旧授权。
- [x] 定向测试及旧用例全部通过，提交 `feat(m2): persist resumable run control`。

> 执行记录（2026-11-09）：`tests/test_run_control.py`（20 例）+ 全量 **296 passed** /
> `ruff check .` 通过。关键取舍与未完成项写在 `docs/REVIEW-PRA.md` §4.4：
> (1) 控制请求与生效状态分成两个字段（`control_requested` / `state`），确认请求
> 与清空请求是同一次提交；(2) `loop.run()` 已改接 Task/Run/Job 与派发，
> `tools.execute()` 的遗留写入开关**已删除**；(3) 新增 `AgentLoop` 与
> `build_stack`，CLI 与测试共用同一装配路径；(4) 等待审批期间的**心跳循环**
> 仍是 Task 6 的 worker，本任务只提供 `heartbeat_once` 并被测试显式调用；
> (5) 多目录任务里相对路径解析到"第一个可写绑定"是 M2 的已知限制。
>
> 补记（2026-11-09，独立复审后）：PR #12 合并后复审发现 5 个阻断项（取消在安全点
> 炸 JobLost 且无 `run.end`、外来 live owner 没被拦住、暂停顶掉取消、效果计数重复
> 累加、复用 call_id 被静默吞掉），全部已在 PR #13 修复并加回归测试；详见
> `docs/REVIEW-PRA.md` §4.5。

#### Task 6：作业表与端到端验收

**Files:** Create `tests/test_m2_acceptance.py`, `docs/M2-verification.md`; Modify `jobs.py`, `cli.py`, `README.md`。

**Interfaces:** 消费 Task 2 的 `JobStore`/`JobClaim`，新增 `run_worker_once(store: JobStore, owner: ProcessIdentity) -> bool` 驱动认领→执行/等待→结算。Job 为本地 E1/E3 Run 调度，字段 `job_id, run_id, state, owner_instance_id, claim_epoch, process_identity, heartbeat_at, deadline_at, cancel_requested, output_ref`。所有心跳/完成提交传 JobClaim，不能以裸 job_id 提交。v2 的生成作业 E2 继续不实现。

- [ ] 先写 `test_two_workers_claim_once`、`test_job_restart_preserves_pending_approval`、`test_recover_is_not_resume`、`test_read_a_write_b_and_chat_without_workspace`。CLI 子进程调用真实 service 路径，不只用内存 mock 证明并发。
- [ ] 运行 `uv run pytest tests/test_m2_acceptance.py`，确认红灯。
- [ ] 以同库事务认领作业并关联 Run；心跳仅延长正确 owner，进程死亡或租约失效转核查流程。foreground CLI 与 worker 用同一执行入口，排队/暂停/审批等待不持数据库事务或 execution_guard。
- [ ] 运行全部 `uv run pytest` 与 `uv run ruff check .`；保留 M0/M1回归与100次注入，新增跨进程授权/租约屏障测试。无需为这些机械验收再次调用付费模型。
- [ ] 用临时夹具演示：零目录对话、A只读/B可写、拒绝覆盖、审批后改参数、双实例争用、改目标旧调用失效、暂停后重启、unknown 阻断 undo/写入。报告逐项结果和未验证平台。
- [ ] 完成独立 verifier pass 后提交 `feat(m2): coordinate resumable local jobs`，创建 PR C；不以计划复选框代替测试输出。

## 4. M2 总验收与最终交接

必须同时成立：C4绕过/过期/伪造被拒绝；删除和整文件替换有审批证据；同一审批不被双消费；跨Task/跨进程/父子目录不重复写；目标变更后旧授权失效；暂停等待状态重启可恢复；已完成调用不重发；unknown 阻断所有受控写入口；旧日志仍可回放；四目标证据状态透明。

交付 `docs/M2-verification.md`，逐项关联测试名、运行命令、实际结果、基线commit及 debug 修复编号。M0–M1 debug 报告由月之亮面交付，管理员统一判断进入 M2 的最小修复集；本计划不得自作“debug 已通过”的结论。

执行前文档协调：PRD“目录内修改自动”与“覆盖同名文件需审批”按 §2.3 的工具级分类落表；Task内单一 lease_epoch 与多目录 resource_epochs 的语义通过独立契约提交统一。D6 已由用户讨论确定，PRD末尾旧“待定”行应改为既有决定，不重新向用户提问。

参考：[SQLite transactions](https://www.sqlite.org/lang_transaction.html)、[SQLite isolation](https://www.sqlite.org/isolation.html)。SQLite 单 writer 与原子事务只覆盖数据库记录，不覆盖文件系统副作用；实现和验收不得混淆两者。
