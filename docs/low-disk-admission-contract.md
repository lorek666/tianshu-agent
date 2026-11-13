# 低磁盘 Run 准入契约

**待实施契约 · 2026-11-14。** 本文件细化 [PRD §8.4](PRD-v1.md#84-快照保留策略d5-定稿) 的低磁盘保护。产品实现尚未交付，LD-01–06 验收全部为 `NOT_RUN`；合入本文不等于产品交付。源码事实固定于提交 [`016910970d381a66ab12a94a5c88080a8cbf7437`](https://github.com/lorek666/tianshu-agent/commit/016910970d381a66ab12a94a5c88080a8cbf7437)（tree `810d5de91e279244559bc55c3e749ddefc0a3c2b`），下列源码链接均指向该版本。

## 1. 目标、既有要求与存在性依据

[PRD §8.4](PRD-v1.md#84-快照保留策略d5-定稿) 要求“剩余空间 <2 GiB 时拒绝新的副作用 Run”，并禁止自动删除 partial/unknown 或未完成对账的快照。[§8.5](PRD-v1.md#85-数据生命周期备份与隐私新增发布要求) 要求空间不足时保全材料并明确拒绝。本契约不交付恢复、容量额度、保留期/GC、物理删除或“放弃回滚”流程。

固定基线尚无本契约的磁盘探测或 Run 纯读资格字段。实现须复用真实 `tools.ToolSpec.side_effect_level`、`ToolRegistry.spec/manifest`，不建工具名黑名单或使用手写 manifest。以下资格与保护均为待实施要求。

事实：`AgentLoop.start` 与 `ControlPlane._run_start` 均调用 `TaskStore.start_run`；子 Run 绕过该方法直接 `_insert_run_locked`；worker/daemon 使用 `resume_run`。`can_write` 只用于 workspace 能力及租约，不回答未来会不会调用记忆、网络或 MCP；零 workspace 控制记忆写合法。当前 `_drive` 会创建空快照清单。（[loop:768–865,912–923](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L768-L865)；[control:425–453](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/control.py#L425-L453)；[tasks:979–1069,1906–2019](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tasks.py#L979-L1069)；[dispatcher:2153–2231](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/dispatcher.py#L2153-L2231)）

## 2. 已选语义与未采用方案

**采用创建前固定的显式执行资格。** 受信入口在创建前选择 `pure_only` 或 `effects`，资格固定于该 Run 的 `run.start`。`effects` 表示本 Run 可以尝试有副作用工具，不是宣称已经产生副作用；先检查空间，再创建/入队。`pure_only` 可在低于阈值时创建并执行，但执行侧永久禁止该 Run 的有副作用工具；要改为可写须结束/取消后新开 Run，不能升级原 Run。

**未采用首次副作用时再准入。** Run 可先创建、读/聊天；首次合法非 pure 调用之前，同事务追加一次持久准入事实，随后才准许该 Run 执行副作用。能自动保留“先读后写”的体验，但这会新增资格转态与事件、恢复/重建规则；“新 Run”将被解释成“首次进入副作用资格的既有 Run”。这不是在 Dispatcher 多查一次磁盘，也不是无契约变更的实现细节。

普通入口默认 effects，并提供真实可达的 pure_only 选项。普通/摘要模型及必要内部运行材料不算本条 C2 业务副作用。选择创建前固定资格，是为了在 `run.start` 前落实“拒绝新的”，避免新增首次副作用资格状态机；代价是选择 effects 后最终仅做读取的新 Run 也须通过准入。入口须向用户说明这一含义。

## 3. 资格、工具与模型边界

- 新建接口接收严格版本化 `RunExecutionPolicyV1 {format_version:1, mode:pure_only|effects}`；来源仅受信 CLI/控制面/内核策略，不从 goal 文本、模型/MCP 输出、caller 的 `approved` 或任意 payload 键推断。它只增加限制，不授予 C4 权限。
- 每条新 `run.start` 必含该对象，由统一创建层校验和写入；不能允许 caller 注入未经检查的同名字段。仅为 `run.start` 登记精确 payload v2，保留旧 v0/v1 的原读取含义；逐类型 current/known/write/history-schema 选择作最小调整，其他事件保持原版本，不能全局升 v2 或补造历史准入事实。现有 event_versions 整数旁表可与事件同事务记录 v2，无须数据库迁移或另建 policy 事实表。（[event_schema:58–59,891–910,1132–1135,1258–1278,1374–1395](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/event_schema.py#L58-L59)；[events:178–184,495–499](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/events.py#L178-L184)）
- 不增加 Run 状态、不改既有 created/queued/running/paused/waiting_approval/reconciling/终态及 effect_state 含义；投影/游标不能单独授资格。重开/rebuild 从原 `run.start` 精确读取；未来版本、坏字段、未知 mode 拒绝执行，不兜底成 effects。
- pure 由当前真实 registry 中该工具精确身份的 `side_effect_level == pure` 判定。pure_only 的模型清单与执行允许面取原能力范围和 pure 的交集；schema/版本/身份改变按原规则拒绝，不因资格而扩工具、目录、记忆或预算权限。
- `list_files/read_text/read_content/memory.search/get/subtask.status/result` 保持纯读；`subtask.status/result` 仍走 Dispatcher 核父 Run/作用域，不能因为走派发就被归为副作用。（[tools:716–1218](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tools.py#L716-L1218)；[loop:382–399](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L382-L399)；[dispatcher:1879–1960](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/dispatcher.py#L1879-L1960)）
- 文件创建/覆盖/删除/恢复、run_command、全部记忆写（含无 workspace 控制面）、network.fetch、MCP tools/call、subtask.spawn/cancel 都按真实非 pure spec 判定。MCP readOnlyHint 不降级；子任务只读不把父 spawn 变成 pure。（[tool_registry:252–263](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tool_registry.py#L252-L263)）
- 此磁盘契约的 pure_only 允许普通/摘要模型调用及必要事件、游标、账本、内容卸载/可重建读索引写入；它不是“零磁盘写”“零联网”或“免费”。模型仍先持久化 intent、执行既有能力/预算/取消门及未知费用规则，不因模型清单纯读而绕过。记忆生命周期写入不属于这些运行材料。（[loop:1316–1337,1451–1466,1665–1725,1795–1812](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L1316-L1337)）
- pure_only 不创建每 Run 的 snapshot manifest/preimage，不取 workspace 写租约；Task 的 writable 绑定保持原事实但在该 Run 被资格限制。既有缺快照且已有文件 intent 的恢复规则不得放宽或补空清单洗成 none_applied。（[snapshot:136–203](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/snapshot.py#L136-L203)；[recovery:526–540](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/recovery.py#L526-L540)）

## 4. Run 创建、子 Run、resume 与执行封口

1. 共享创建策略必须覆盖 `TaskStore.start_run/_insert_run_locked`、AgentLoop.start、ControlPlane._run_start、Dispatcher.open_control_run、create_subtask。统一核心自行探测并决定，不把调用方传来的布尔值/测量值当许可；不能只在 CLI/UI 加判断。
2. effects 在真实 `run.start`/Run 行、active_run_id 变更、入队/claim、Run 专属快照、模型调用、审批请求前完成准入；失败无该新 Run/Job/租约/快照/审批及副作用调用。既有 Task/全局 stack 初始化可已存在，不虚称整库零写；控制 Run 要把检查前移到 create_task 前避免留空壳。
3. pure_only 无此 2 GiB 阈值门；合法读、对话、子 Run 可运行，前提是必要审计能正常持久化。空间实际耗尽或审计失败仍明确失败，不能无日志继续模型/工具。普通入口默认 effects 以保持原有可写能力；CLI/RPC/UI 必须有真实可达的 pure_only 选项，不能自动按 goal 猜模式或低盘静默降级。
4. `create_subtask` 固定产生 pure_only 子 Run，并把资格与子 Task/Run/Job/预算及父 spawned 事实同事务绑定；保留 depth、无写/exec/外发/MCP/嵌套委派、无记忆写门。父 spawn 属 effects，低盘不得以“孩子只读”绕过父执行保护；已排队 pure_only 孩子的运行不继承父写资格。
5. `resume_run`、worker、daemon 和直接 `_drive` 保持原 Run/资格/预算/claim 规则，不新建 Run、不改资格、不清预算、不人为换 claim_epoch。所有既有 Run（含已声明 effects 与 legacy）在原安全、预算、取消、审计条件满足时，均可恢复纯读和普通/摘要模型；不得仅因低盘/空间未知阻止其转 running 或发新模型。pure_only 仍禁止非 pure；新非 pure 派发按第 7/9 项现测保护。暂停、取消、只读 recover/状态查看保留原规则。
6. 旧 v0/v1 Run 没有资格事实，不补写/推算为已批准。保留历史读取/replay；其纯读/模型恢复与已声明 effects 按第 5 项成对验收，不冒称“新 Run 准入已过”；新非 pure 派发按第 7/9 项当前执行前保护。此为显式兼容分支，无新持久 lazy 资格，不能用于创建缺字段的新 Run。
7. Dispatcher 先按固定真实 ToolSpec 精确分类：仅非 pure 工具施 pure_only 资格门与当前空间门（停止控制的空间例外仅见第 9 项）；仍走 Dispatcher 的 pure 工具不受这两门阻挡，保留原身份、父 Run/作用域、C4、预算和审计校验。非 pure 在 prepare 请求审批前核资格/空间，dispatch 在审批/grant 消费及 tool.intent 前按当前实际目标复核；pure_only 永久拒绝非 pure，effects/legacy 低盘或未知拒发普通非 pure 且不消费审批。C4 批准、request.state=admitted、伪造 AdmittedCall 均不能跳过。
8. FileExecutor/MemoryExecutor、文件 begin_execution、记忆执行守卫、网络发送和 MCP send boundary 必须按第 7/9 项复核所属 Run 资格与当前空间；`tools.execute` 保持只读公共入口。公共直接派发/控制记忆/undo 新控制 Run 均受同门，无快照配置不等于豁免。未绑 Run 的传输/底层存储是内部原语，不能当产品执行入口交付旁路。（[tools:1484–1539,1652–1681](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tools.py#L1484-L1539)；[memory_exec:481–509](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/memory_exec.py#L481-L509)；[tasks:1127–1198](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tasks.py#L1127-L1198)）
9. 既有 Run 的核心 pause/cancel/revoke 保留原停止控制权限、预算与审计规则，不受本空间门阻挡；effects 与 legacy 下对已有子任务的 subtask.cancel 同样保留原规则且不受空间门阻挡。subtask.cancel 的真实 spec 仍为非 pure，pure_only 不能调用它；核心停止入口不因此受限。取消不得开启新副作用 Run，也不得给 delete/erase/undo 扩大“腾空间”豁免。
10. `build_stack` 会在 Run 创建前 start_all MCP。pure_only 路径不为本 Run 启动 MCP；创建新 effects Run 时，在这类装配前先只读预检，真正 start_run 仍复查。既有 Run 继续纯读/模型须可避开本 Run 新 MCP 启动装配，不能把此预检当 resume 全面拒绝门。已存在常驻 server 或显式 mcp list 不属于本 Run，不能声称门禁隔离其进程或探测其远端磁盘；所有产品 tools/call 仍必须过 Run/Dispatcher。（[loop:3277–3298](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L3277-L3298)；[mcp:611–663,807–825](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/mcp.py#L611-L663)）

## 5. 空间判据、探测位置与时点

- 常量 `MIN_FREE_BYTES = 2 * 1024**3 = 2147483648`。每个必需位置当前用户可用字节 `<` 此值拒绝；**恰好等于允许通过空间这一个条件**，不保证后续写入成功。整数严格非 bool、非负；测不出与真实 0 区分。
- 创建 effects Run 时检查实际 `EventLog.db_path` 所在卷（含 WAL/journal）、log.blob_dir、实际 SnapshotStore 的 root/blob/manifest、内容卸载目录、MemoryProjection.blob_dir、本次 Run 声明可写的 workspace 根，并纳入实际权威根的 `authority.db`（含 WAL/journal）及独立实际 `data_root/execution.guard`。权威根按本次调用真实配置选址：显式 root（若有）或现读 `TIANSHU_AUTHORITY_ROOT`/默认选择；执行守卫按 TaskStore/Dispatcher 等实际实例传入根取址，不能假设与事件 DB 同卷。使用真实实例/调用位置，readonly workspace 不作写目标门。（[authority:125–147,188–199,375–428](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/authority.py#L125-L147)；[tasks:588–634](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/tasks.py#L588-L634)；[leases:745–766](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/leases.py#L745-L766)；[loop:3239–3274](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L3239-L3274)）
- 多位置全部满足，不能用 A 卷的空间替 B 卷、求和或取“最大剩余”。同卷可在同次判定去重但保留位置映射；不能仅查 data_root、CWD、HOME 或默认 events.db 替代完整集合；`:memory:` 不伪造磁盘 DB 位置，仍检查实际落盘材料/目标。没有某类材料时明确标不适用；需要该位置却未知不得跳过。（[events:375–383](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/events.py#L375-L383)；[loop:3239–3274](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/loop.py#L3239-L3274)；[memory:788–825](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/memory.py#L788-L825)；[snapshot:136–143](https://github.com/lorek666/tianshu-agent/blob/016910970d381a66ab12a94a5c88080a8cbf7437/src/tianshu/snapshot.py#L136-L143)）
- 标准库 Python 3.12 `shutil.disk_usage` 是首选薄探测器；不引运行时依赖、外部 df/wmic 命令或新平台 SDK。先 canonicalize，缺失目标（含尚不存在的 authority 根/DB、执行守卫根/文件）取最近的真实既有目录，不 mkdir/造文件后探测；坏路径、权限/网络盘错误、不可判定卷或非法 free 值均 fail-closed，给出独立的“空间未知”原因而不是当成无限容量/0。
- 取值在准入调用内现测，不使用上次启动缓存、调用者填写的 free 或磁盘用量预测；写事务只包现有必要状态提交，不持长事务等外部 I/O。阈值检查不是空间预留，也不引跨进程全盘容量锁；两个 Run 都可能通过后被外部写满，ENOSPC/审计失败仍按真实阶段保全并报错。
- 每次新的普通非 pure 动作（停止控制按 §4.9）在审批前/消费前检查上述当前实际材料位置（含 authority 与独立执行守卫）和真实目标：已有文件查实际目标/父目录，待创建文件查最终 canonical 父目录，run_command 查真实 cwd 与声明可写根。嵌套挂载/目标位置变化不能沿用 workspace 根旧测量；身份/授权仍由原边界复核。执行期间外部重挂载/进程域外写入不在本片保证内。
- 远端 MCP、任意命令可能写到的全部外部位置无法由本地探测证明；只对本程序掌握的本地持久材料、声明 workspace/cwd 与实际文件目标承诺。不能因远端未知把本地测量写成“所有副作用绝不会满盘”。
- 执行复查是对已有 Run 的当前安全保护，**不能替代第 4 节的创建前准入**。新 effects 创建检查不通过不得创建该 Run；已有 Run 的普通非 pure 执行检查不通过不得派发，pure/模型恢复与停止控制仍分别按 §4.5–7/§4.9。已经执行/不明结果不改成 none_applied，不盲重试，不清快照/Receipt/未决 intent；必要写证据失败也不伪称已持久化。

## 6. 最小验收（全部 NOT_RUN）

- LD-01：真实 registry/schema 绑定 pure/非 pure 分类，含零目录记忆、动态 MCP、网络、subtask.spawn/cancel/status/result；低盘/未知时 Dispatcher 的 pure status/result 正控仍通过原校验，另一父 Run/不存在目标仍拒绝，pure_only 非 pure 拒绝；删执行侧资格守卫、误将全部 Dispatcher 调用过门或伪造 payload/票据的负控须红。
- LD-02：真实 CLI 与 ControlPlane 各走 start；注入 free=2147483647/2147483648/2147483649、0、bool/负值/探测异常。effects 拒绝在 run.start/Job/租约/快照/审批/模型前；pure_only 低盘可完成读和脚本对话、零写工具/零 Run 快照，mode 可从重开/rebuild 验证。
- LD-03：分别直接调用 TaskStore.start_run、子 Run 创建、open_control_run/控制记忆/undo、Dispatcher 与执行器；破坏一处接线会真红，不用一个 CLI 用例代表全部入口。子 Run 低盘纯读可运行，父 spawn/新控制写拒绝。
- LD-04：DB、payload、snapshot、memory blob、两个 workspace、`TIANSHU_AUTHORITY_ROOT`/显式 authority 根及独立 `data_root/execution.guard` 分置受控不同卷，逐个仅一处低盘/未知都拒绝新 effects/普通非 pure；遗漏 authority 或守卫任一位置的单独负控须红，并核配置现读/真实实例选址，不能仅改 DB/data_root 后仍查旧根。缺目录探测前无 mkdir/造 DB/守卫文件，readonly 卷低盘不误挡纯读、目标嵌套挂载不借根卷通过。单独原生 smoke 核 stdlib 取当前用户 free，不用本机读数证明另一平台。
- LD-05：已声明 effects 与 legacy v0/v1 成对覆盖先准入/既有 Run 后低盘或未知、审批后低盘、暂停/进程重开、worker/daemon/直接 drive：原安全/预算/取消/审计满足时仍转 running、完成纯读与脚本普通/摘要模型；新普通非 pure 拒发不消费审批、不换 Run/资格/claim 规则、不清预算，恢复空间后按原规则可执行。两类的核心停止/已有子任务取消均保留原控制规则；pure_only 非 pure cancel 仍拒绝、核心停止可用。legacy 无伪造资格，旧结果/unknown/receipt 不变；把恢复改为全面拒绝模型的负控须红。
- LD-06：新 run.start 精确版本与真实 schema 双向对账、未知/坏策略拒绝、历史夹具与 replay/rebuild；真实双子进程屏障验证竞争/重开不靠进程内许可。硬退/ENOSPC 窗口保全，无占位 skip；不宣称物理断电或空间预留已验。

## 7. 实施范围与交付条件

本契约采用创建前固定资格、默认 effects 与显式 pure_only；既有 effects 和 legacy 的纯读/普通及摘要模型恢复保留原规则。执行保护须同时落实纯读 Dispatcher 调用、停止控制例外、真实 authority 与 execution.guard 位置，以及 `run.start` 的逐类型 payload v2；不得凭文档通过宣称这些行为已经存在。

产品实现另走 PR，覆盖事件/错误封闭集与真实 schema、Run 创建层、控制面/worker、Loop/Dispatcher/执行器及快照恢复，并按 LD-01–06 提供真实入口、正负控、恢复和平台证据。实现须由第二读者绑定最终完整提交 SHA 独立复审，再通过该提交的原生 CI。未运行的验收保持 `NOT_RUN`；恢复 apply、容量/GC、真实付费任务、十小时长跑及普通用户发行仍属其他交付范围。
