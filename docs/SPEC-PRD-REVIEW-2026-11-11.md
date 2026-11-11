# 工程评审新增要求的施工规格（SPEC-PRD-REVIEW-2026-11-11）

- 立据：`docs/PRD-v1.md` 的 2026-11-11 工程评审合并（§1.4、§3.4、§5.2.1、§5.3.5、§5.6、
  §7.1、§8.1.1、§8.5、§10.2–§10.6、§11.1、§13.6，附录 B 第 9–14 条）。该批新增内容目前
  只存在于工作区（`docs/prd-engineering-review-20261111` 分支，基线 `540c543`），**尚未合入
  `main`**；本规格先把要求拆成可验收工作流，再由 W0 把 PRD 新增章节合入 `main`（保留
  `main` 上已有的记忆契约章节）。
- 基线：`main @ da84d92`（PR #75 之后）。本规格落笔时实测
  **1316 passed / 6 skipped（116.83s）**、`ruff check .` 干净、运行时依赖仍为零。
  数字只对这次实测负责；各工作流收工时必须自己重跑，不抄这里的数。
- 判定口径（沿用 `docs/SPEC-RESIDUALS-M0M1.md` §0）：
  **有回归测试、且做过负控（把修复回退后测试会红）才算做完**。
  **要求写进 PRD ≠ 已实现、契约已冻结或验收已通过**；读不懂 ≠ 没写过；判定不了 = unknown + 阻断。
- 范围：PRD 新增要求中**可在本机、无新运行时依赖、不依赖网络 / MCP / 子代理能力、不依赖
  Windows 实机**交付的部分（W1–W5）。其余按 §6 明确登记为后续轮次，不冒充完成。

## 0 施工纪律（所有人必须遵守）

1. **只在自己的 worktree 里操作**，禁止在 `/Volumes/Date/天枢Agent`（主检出）跑 `git` 或改
   文件。本规格的 worktree 是 `/Volumes/Date/ts-wt-prd` 派生出的各分支。
2. **不要碰另一个会话正在改的文件**。并行会话的分支是 `feat/memory-lifecycle`（记忆契约
   v1.2 落地），其施工规格 `docs/SPEC-RESIDUALS-MEMORY.md` §0 声明占用：
   `src/tianshu/memory.py`、`memory_exec.py`、`dispatcher.py`、`tools.py`、`loop.py`、
   `permissions.py`、`rules.py`、`host.py`、`recovery.py`、`schemas/c4/rules.json`、
   `tests/conftest.py`、`tests/test_memory_*.py`、`docs/KNOWLEDGE.md`、`README.md`、
   `docs/PRD-v1.md`、`docs/F10-memory-design.md`、`src/tianshu/events.py`*、`src/tianshu/errors.py`。
   带 * 的是本轮 W5 **必须**改到的文件（`events.py`）；W5 先落，改法限定在
   「新增校验钩子 + 新表」，不重排既有逻辑，冲突留给 Lead 统一解。其余文件本轮一律不改。
3. **不加第三方运行时依赖**（`pyproject.toml` 的 `dependencies = []` 不动）；不抬 Python 版本；
   不改 `.github/workflows` 的必过检查。
4. **一提交一件事**：`feat:` / `fix:` / `test:` / `docs:` / `docs(contract):` 分开。提交必须有
   正文：改了什么、为什么、判据、负控摘要。契约文件（`schemas/`）变化用单独的
   `docs(contract):` 提交，并在 body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」。
5. **不许改判据让自己变绿**：既有测试、既有 `Check` 口径、既有断言只能因为「新要求改变了
   语义」而改，且必须在该 PR body 里写明旧口径、新口径与理由。未经允许改既有判据的 PR
   直接拒。
6. **收工前**：`PYTHONPATH=<你的 worktree>/src /Volumes/Date/天枢Agent/.venv/bin/python -m
   pytest -o addopts="" -q` 全绿；`/Volumes/Date/天枢Agent/.venv/bin/ruff check .` 干净。
   **不要**自己合 `main`；推送分支后把分支名、提交列表、测试/负控证据回报 Lead。
7. 干不完就如实报：完成了什么、卡在哪、还差什么、哪些宣称仍然不成立。

## 1 要求 → 现状 → 本轮交付

| PRD 要求 | 现状（代码/测试事实） | 本轮 | 谁 |
|---|---|---|---|
| §10.2–§10.4 四维评测、判据边界与正负控 | `eval/checks.py` 已有 `Check.kind ∈ {safety, completion, metric}`，报告有逐 case 的 `columns`；**没有**四维聚合、**没有**「无效样本 vs 产品失败」的分离、**没有**判据负控元测试 | 四维报告结构 + 无效/失败分离 + 负控登记与元测试 + suite 版本化（m4.6） | W1 |
| §10.5 真实用户任务集 | 现有 20 条契约基线是 harness/model 脚本 case；**没有**三类真实用户任务夹具 | 三类任务集：本地资料整理、受控文件修改（做成可跑的 harness 任务并纳入报告）；联网研究按「未具备能力」诚实登记 | W1 |
| §10.6 本地运行指标、控制面不被慢订阅者阻塞 | 没有任何指标采样；`daemon._main_loop` 单线程里同步 `_push_events()`，一个不读的订阅者会把派发/审批/取消一起堵住 | 本地指标采样（启动/取消/恢复/资源）+ 慢订阅者隔离（有界队列 + 发送期限 + 断开 + 游标补读）+ 浸泡测试 | W2 |
| §8.1.1 扩大自主执行前的隔离门禁 | `exec.local_capabilities()` 如实声明 `isolation="none"`、`descendants_may_survive`；**没有**逐边界探针、支持矩阵与「不满足即关闭能力」的门 | 四边界探针（文件 / 网络 / 凭据 / 进程树）+ 后端×平台矩阵 + `require_isolation` 门 + 本机真实证据 | W3 |
| §8.5 一致备份、恢复演练、保留期；§5.3.5 迁移中断 | **没有**备份模块；`SnapshotStore` 是写前副本，不是备份；数据库只有 `CREATE TABLE IF NOT EXISTS`，没有版本化迁移与中断恢复 | 一致点备份 / 校验 / 干净根恢复；版本化迁移日志 + 故障注入 + 续做；保留期声明 | W4 |
| §5.3.5 payload 契约、精确版本、旧版映射 | `schemas/c3/event.json` 只冻结**信封**，`payload` 是 `type: object` 无字段校验；事件行没有 payload 版本 | 逐类型 payload schema + 写入前校验 + 事件版本旁表 + 旧版夹具读取兼容 | W5 |
| §1.4 / §3.4 / §7.1 / §11.1 / §13.6 | 本次为新要求，其中 §7.1 依赖模型摘要能力、§11.1 的 G2/G3 依赖网络/MCP/子代理与平台实机 | 只落文档（PRD 合入 + 本规格 + 各工作流文档）；不实现 | W0 |
| §5.2.1 通用工具准入 / §5.6 共享预算账本 | 与并行记忆工作流同改 `dispatcher.py` / `tools.py` / `loop.py` / `permissions.py` | **本轮不做**，登记为下一轮（见 §6） | — |

## 2 W1：G0 四维评测与真实用户任务集（`g0-eval`）

分支 `feat/eval-four-dimensions`；worktree `/Volumes/Date/ts-wt-g0`。
文件所有权：`src/tianshu/eval/**`、`tests/test_eval_*.py`、`docs/eval-four-dimensions.md`、
`docs/real-user-tasks.md`。不碰 `src/tianshu/` 下其他模块。

**2.1 四维报告结构**（§10.4）

- `eval/report.py` 的 `build_report()` 增加稳定的 `dimensions` 段：
  `safety` / `completion` / `reliability` / `efficiency`，各自给「样本数、通过数、失败清单、
  依据的 check_id」。四项**独立记录**：安全性仍是硬门槛（`verdict=unsafe`），完成度失败
  **不得**被记成不安全。
- 可靠性维度从 `Observation` 里的恢复/故障事实取数（recovery bucket、重复副作用、超时、
  取消）；效率维度取 attempt 数、token、已知/未知费用、端到端耗时。缺数据必须显示
  `unknown` 而不是 0。
- 报告里每 case 保留现有 `columns`；新增 `dimensions` 不得改变既有 `verdict` 口径（除非
  新要求明确要求，且要在 PR body 里写清）。

**2.2 判据有效性与无效样本**（§10.2 判据边界）

- `Check` 增加 `path_coverage: bool`（专门测错误路径的 case 标注；未触发即没有覆盖证据）。
- `Observation` 区分「产品失败」与「评测基础设施无效」（真实子进程起不来、夹具 / 后端
  不匹配、取证失败）：新增无效样本计数与原因；无效样本不参加分子/分母，但**产品按契约
  应持久化的 intent/result/Receipt/审批/恢复证据缺失仍是产品失败**，不得当无效剔除。
- 关键判据必须有负控：新增 `tests/test_eval_negative_controls.py`，对「重复写 / 审批绕过 /
  错误产物 / 丢失恢复证据」各造一个反例，断言对应 `Check` 会红；同时保留合法正控（额外
  交付物、独立授权的第二目标）不得误报。另加元测试：`safety` 类 check 必须登记负控
  （缺登记即红），防止新增判据无负控。
- suite 版本 m4.5 → **m4.6**；`compare_reports` 遇到版本不同的两份报告必须明确标为
  「不可比」而不是算提升。

**2.3 真实用户任务集**（§10.5）

新增 `src/tianshu/eval/tasks.py`（或 `eval/tasks/`），三类任务各带版本化输入、目标产物、
授权范围与验收判据，**不靠模型自称完成**：

| 任务类 | 夹具 | 必验 |
|---|---|---|
| 本地资料整理 | 多份授权资料 → 总结产物 | 关键事实覆盖、可定位来源、无禁止输出；读 A / 写 B 边界；缺资料时诚实报告 |
| 受控文件修改 | 批准 / 拒绝 / 撤销三条路径 | 批准前目标不变；获批动作只改绑定目标；额外交付物按各自授权单独验收；拒绝后不重复请求；撤销冲突不覆盖用户后续修改 |
| 联网研究交付 | **本轮不提供** | 明确报「未具备能力」，不得用脚本或静态网页冒充线上完成 |

- 任务夹具与判据进 `build_report` 的独立 scope（如 `real_user`），与契约基线的
  `harness` / `model` 分开计数；报告列明 case 数、重复数、有效/无效数、逐次结果。
- 每个任务至少 3 次重复；harness 用脚本模型可跑，真实模型复跑为 opt-in（环境变量开关，
  缺凭据时跳过并如实标注，不伪造分数）。
- 网络任务「未具备能力」必须是一条**通过的诚实性判据**（报告写着 unavailable 才算过），
  不是跳过。

**2.4 验收**

`tests/test_eval_four_dimensions.py`、`tests/test_eval_real_tasks.py`、
`tests/test_eval_negative_controls.py` 全绿；负控：回退四维分离 / 回退无效样本分离 /
去掉某安全判据的负控登记，对应测试必须红（PR body 写明红的样子）。文档：四维口径、
判据有效性、真实任务集台账（含版本号与未做项）。

## 3 W2：§10.6 本地运行指标与控制面隔离（`runtime-metrics`）

分支 `feat/runtime-metrics`；worktree `/Volumes/Date/ts-wt-metrics`。
文件所有权：`src/tianshu/metrics.py`（新）、`src/tianshu/daemon.py`、`src/tianshu/ipc.py`、
`tests/test_metrics.py`、`tests/test_slow_subscriber.py`、`tests/test_soak.py`、
`docs/runtime-metrics.md`。**不碰** `host.py`（并行会话占用）。

**3.1 本地指标**（§10.6）

- 新模块 `metrics.py`：进程内采样器，写本机数据根（如 `<data_root>/metrics.jsonl`，原子追加），
  **不联网、无自动上传**；字段至少：`startup_ready_ms`（冷/热分开）、`cancel_ack_ms`、
  `cancel_stop_ms`、`restart_recover_ms`、`rss_mib` / `cpu_pct` / `disk_growth` 采样、
  `event_seq_head`、`slow_subscriber_dropped`。提供 p50/p95 汇总（纯 Python，无新依赖）。
- 指标是「有数据」而不是「有日志」：每个采样点必须能被测试读取并断言；缺阈值时如实输出
  `unknown`，不写假 SLO 数字（数值冻结留给发布候选，PRD §10.6 已声明）。
- 不引入遥测；导出遵守 D7（本机）。

**3.2 控制面隔离（慢订阅者）**（§10.6）

- 现状事实：`daemon._main_loop` 单线程顺序执行 `_drain_requests()` 与 `_push_events()`，
  `conn.send()` 同步写 socket。一个订阅后不读的客户端会把审批 / 取消 / 状态查询一起堵死。
- 修法：订阅推送改为**每连接有界发送队列 + 独立发送线程**；队列满或发送超期 → 断开该
  订阅者并记 `slow_subscriber_dropped`，**不影响**其他连接与请求处理；订阅者重连后按
  `since_seq` 游标补读，不丢审计事实。
- 队列上界、发送期限、断开后补读全部要有测试；不得用「加大 socket 缓冲」冒充。

**3.3 验收**

- `tests/test_slow_subscriber.py`：真实 socket + 一个订阅后不读的客户端 + 并发写事件，
  断言 `status` / 审批查询在声明时限内仍返回；断言慢订阅者被断开且重连后从游标补齐。
  负控：把推送改回同步 `send`（或把队列设成无界且同步发送），测试必须红。
- `tests/test_metrics.py`：指标采样可读、冷/热启动、取消计时、重启恢复计时、p95 计算、
  无网络调用；负控：把采样写空 / 谎填 0，测试红。
- `tests/test_soak.py`：`TIANSHU_SOAK=1` 才跑的有界浸泡（默认 3 分钟，环境变量可调），
  覆盖断连 / 重连 / 取消 / 重启，报告资源趋势与残留进程；默认 CI 跳过并显式说明。
- `docs/runtime-metrics.md`：指标定义、测量方法、当前实测值（本机 macOS arm64，注明硬件/
  OS/数据规模）、尚未冻结的 SLO 与原因。

## 4 W3：§8.1.1 隔离门禁（`isolation-gate`）

分支 `feat/isolation-gate`；worktree `/Volumes/Date/ts-wt-isolation`。
文件所有权：`src/tianshu/isolation.py`（新）、`src/tianshu/exec.py`（只加只读探针/能力声明，
不改 spawn 语义）、`tests/test_isolation.py`、`docs/isolation-matrix.md`。

**4.1 四边界探针**（PRD §8.1.1 表）

| 边界 | 探针（真实子进程，不用 mock 冒充） | 本机 local 后端预期 |
|---|---|---|
| 文件与权威数据 | 子进程读 / 写工作区外路径；子进程打开事件库 `RW` | 全部**成功** → 判「不隔离」 |
| 网络 | 子进程连本机监听端口（loopback） | 成功 → 「出网不受控」 |
| 凭据 | 父进程设哨兵环境变量，子进程读 | 可见 → 「继承宿主环境」 |
| 终止与资源 | 子进程派生孙进程，超时后杀树，检查孙进程是否存活 | POSIX 组杀应可确认；Windows 见下 |

- 每个探针返回结构化结论 `IsolationFinding(boundary, status ∈ {enforced, not_enforced, unknown},
  evidence)`；`unknown` 必须阻断依赖它的能力启用，不按「没发现问题」当通过。
- `require_isolation(backend, boundary)`：不满足 → 拒绝启用依赖该边界的能力（模型生成代码、
  不可信本地 MCP 程序、直接执行命令的子代理）。现有 `run_command` 路径只有显式 exec 策略，
  本轮**不**立即改变其默认行为（避免与既有验收冲突），但门必须先能被测试调用；
  若接入会让既有测试变红，先回报 Lead 再决定。

**4.2 支持矩阵**

- `docs/isolation-matrix.md`：后端 × 目标 OS/架构 × 四边界 × 结论 × 证据（命令、退出码、
  时间、本机文件/进程观察）。本轮只有 **macOS arm64 本机** 实测；Windows / mac-x64 /
  win-arm64 标 `unknown`（无实机），不臆造。
- 负控：造一个「声称隔离但探针不通过」的假后端，断言门拒绝；把 local 后端的探针结果改回
  「enforced」，测试必须红。

**4.3 验收**

`tests/test_isolation.py`（真实子进程，含超时保护）全绿；`docs/isolation-matrix.md` 有实测
证据与 unknown 清单；PR body 写明哪些能力因此被关闭、哪些仍未验证。

## 5 W4：§8.5 一致备份恢复 + §5.3.5 迁移中断（`backup-restore`）

分支 `feat/backup-restore`；worktree `/Volumes/Date/ts-wt-backup`。
文件所有权：`src/tianshu/backup.py`（新）、`src/tianshu/migrate.py`（新）、
`tests/test_backup.py`、`tests/test_migration.py`、`docs/backup-restore.md`。
不碰 `recovery.py` / `snapshot.py`（并行会话占用或改了就冲突）；需要读它们时只读。

**5.1 一致点备份**（§8.5）

- `create_backup(data_root, dest)`：先停并发写者（复用现有 host/lock 语义，只读判断；
  不持有的情况下用 SQLite 在线备份 API），在同一一致点导出：事件库、必要投影、写前快照、
  内容 blob、版本清单；`backup.json` 记录库版本、应用版本、每个文件的 sha256、引用清单、
  恢复须知、保留期声明。**不是**复制正在写入的 `.sqlite3` 文件。
- 原子性：写临时目录 → fsync → 原子改名；中断留下的临时目录不得被 `verify` 当有效备份。
- `verify_backup(dest)`：哈希、引用完整性、事件 `seq` 连续、清单可解析；缺 blob / 坏清单 /
  版本不兼容 → 明确拒绝并给补救路径，**不**生成空历史。
- `restore_backup(dest, data_root)`：只允许恢复到干净根（非空目标拒绝）；恢复后核对未决
  intent/result、审批、租约并输出报告；恢复失败保留原数据。
- 保留期声明（§8.5）：事件 / 快照 / blob / 诊断 trace / 索引分别的保留期、磁盘额度、
  清理责任；被审计或未决操作引用的材料不得清理。文档化 + 可测试的引用保护（不实现全自动 GC）。

**5.2 版本化迁移与中断**（§5.3.5 第 3 条）

- `migrate.py`：数据库/投影结构版本号（`PRAGMA user_version` 或旁表）、步骤注册表、
  每步进度持久化到 `migration.json`；迁移前要求已有 `verify_backup` 通过；不 UPDATE/DELETE
  历史事件。
- 故障注入：每一步前/后调用 `crashpoint.failpoint` 硬退；测试用子进程在迁移中断后重启，
  断言：可续做、无混合版本半成品、或可从备份恢复；不支持的版本拒绝写入并给恢复路径。
- 负控：把迁移改成「先删后建」或去掉备份前置，测试必须红。

**5.3 验收**

`tests/test_backup.py`、`tests/test_migration.py` 全绿；覆盖备份中断、一致点恢复、损坏/缺失
内容、磁盘满（monkeypatch 写失败）、迁移中断、忘记后恢复旧备份（与现有 undo/snapshot 语义
对齐）。文档写清「备份 ≠ 快照 ≠ 撤销」与仍不成立的宣称。

## 6 W5：§5.3.5 事件 payload 契约与历史兼容（`event-history`）

分支 `feat/event-payload-contract`；worktree `/Volumes/Date/ts-wt-events`。
文件所有权：`src/tianshu/event_schema.py`（新）、`src/tianshu/events.py`（只加校验钩子 +
版本旁表）、`schemas/c3/event.json`（契约）、`tests/test_event_schema.py`（新）、
`tests/fixtures/events/`（新）、`docs/event-payload-contract.md`。

**6.1 契约（单独 `docs(contract):` 提交）**

- 每个 `EVENT_TYPES` 成员的 payload schema（JSON Schema 2020-12 子集），含 `required`、
  `additionalProperties: false`、嵌套对象/数组/枚举；不支持的关键字在**注册时**拒绝
  （fail-closed），不是运行时忽略。
- 事件行增加 `payload_version`（整数）。实现用**旁表** `event_versions(seq INTEGER PRIMARY
  KEY, type TEXT, version INTEGER)` 写入同一事务，不改既有 events 表、不 UPDATE 历史行；
  历史行无版本记录 → 读取时按 `version=0`（冻结前的旧版）映射，映射表可测试。
- 旧版映射规则：缺字段的按「旧版本来就没有」读取，不按最新 schema 默认补值后假装原始
  记录如此。

**6.2 写入校验与读取兼容**

- `EventLog.append` 在落盘前校验 payload；非法 → 类型化错误（带 `type` 与失败路径），
  不写入、不落 blob。
- `read()` / 回放 / 投影接受旧版（version=0）与新版本；同一类型跨版本读取必须有固定夹具
  测试；`replay` 不因校验升级而失效。
- 版本旁表缺失/损坏时 → 明确 unknown（阻断依赖精确版本的读取），不猜。

**6.3 验收**

- `tests/test_event_schema.py`：每种类型至少一正一反例；旧版夹具逐个可读；`replay` 绿；
  未知 type 仍拒；不支持 dialect 拒；负控：去掉写入校验 / 把旧版按新版默认补值，测试红。
- `docs/event-payload-contract.md`：类型清单、版本、旧版映射、仍冻结不了的字段（如果真有）
  与原因；明确「类型封闭 ≠ 字段契约完整」的当前边界。

## 7 W0：PRD 合入与文档（Lead）

- 把工作区里 PRD 的 2026-11-11 新增章节合入 `main` 的 `docs/PRD-v1.md`：保留 `main` 已有的
  记忆契约章节；§5.2.1 冲突按「记忆工具（已有）→ §5.2.2 记忆契约增量（已有）→ §5.2.3 通用
  工具准入（新增）」编号，改掉新增内容里 4 处 §5.2.1 交叉引用。合并后逐节核对：新增章节
  全在、记忆章节未丢、章节号连续、内部链接可解析。
- 全轮收尾：`docs/KNOWLEDGE.md` §2/§4/§5/§7/§8、`README.md` 测试数、`docs/DEBUG-*`（如适用）
  由 Lead 统一更新；最终 `main` 上全量测试 + ruff + CI 绿。
- **不**改 `docs/SPEC-RESIDUALS-MEMORY.md`、`docs/F10-memory-design.md`（并行会话领域）。

## 8 合并顺序与依赖

1. W0 规格 PR 先合（本文件 + PRD 合入），其他人从合入后的 `main` 开分支。
2. W1–W5 并行（文件所有权互不重叠）；W5 与并行会话的 `events.py` 冲突由 Lead 统一解。
3. 全部工作流合入后，Lead 跑全量测试 + ruff，更新 `KNOWLEDGE.md` / `README.md`，开收尾 PR。
4. 依赖：W4 的迁移前置需要 W4 自己的备份校验（同 PR）；W3 的矩阵依赖本机真实证据；
   无跨工作流代码依赖。

## 9 本轮明确不做（如实登记，不写进「已完成」）

- **§5.2.1 通用工具准入**、**§5.6 共享预算账本**：与并行记忆工作流同改
  `dispatcher.py` / `tools.py` / `loop.py` / `permissions.py`，本轮改会直接冲突。等记忆
  N1 合入后按本规格同一口径另开工作流（`budget.py` + 准入校验器 + 接入）。
- **§7.1 模型语义摘要**：依赖摘要调用能力与 `loop.py`（并行占用）；本轮保留现有确定性
  卸载/裁剪，不实现语义摘要，验收项记入缺口。
- **G2 网络 / MCP / 子代理**：本轮不实现任何新能力；§10.5 联网任务按「未具备能力」诚实登记。
- **G3 平台实机证据**：只有 macOS arm64 本机；Windows / mac-x64 / win-arm64 的安装、
  升级、回退、隔离证据均 **unknown**，不用 CI job 或制品下载冒充。
- 不改运行时依赖、Python 版本、CI 必过检查、既有判据（除新要求明确改变语义且写在 PR body）。

## 10 验收清单（每个工作流收工时逐条回报）

- [ ] 新代码有回归测试；关键修复有负控，且负控真的跑过（写明回退后红的样子）。
- [ ] `pytest -o addopts="" -q` 全绿、`ruff check .` 干净（自己 worktree 实测）。
- [ ] 文档写「当前边界 / 未验证项 / 仍不成立的宣称」，没有把「要求」写成「已实现」。
- [ ] 没有改别人的文件；没有改既有判据让自己变绿；没有新增运行时依赖。
- [ ] PR body 含：改了什么、为什么、判据、负控摘要、剩余 unknown。
