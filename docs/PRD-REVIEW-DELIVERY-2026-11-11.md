# PRD 2026-11-11 工程评审新增要求的交付对账（PRD-REVIEW-DELIVERY）

- 立据：[PRD-v1.md](PRD-v1.md) 的 2026-11-11 工程评审合并（§1.4、§3.4、§5.2.3、§5.3.5、
  §5.6、§7.1、§8.1.1、§8.5、§10.2–§10.6、§11.1、§13.6，附录 B 第 9–14 条），施工规格见
  [SPEC-PRD-REVIEW-2026-11-11.md](SPEC-PRD-REVIEW-2026-11-11.md)。
- 基线：`main @ c0dfa88`（PR #84 之后）→ 第一轮收尾 `main @ ec7d7ae`（PR #88 之后）→
  G1 轮次 `main @ cc2240e`（PR #92 之后）。G1 轮次实测
  **1601 passed / 7 skipped**（macOS arm64 本机，169.08s；CI 同刻 required checks 全绿），
  `ruff check .` → `All checks passed!`。`windows-latest` 在 G1 轮次 PR #92 的一次运行里实测
  **5 failed / 1548 passed / 29 skipped**（998.29s），resync 后那次则在 25m11s 被取消
  （既有命名管道阻塞族）；两者都不是 required check，也没有拿它冒充「Windows 已通过」。
- 第一轮收尾态（`main @ ec7d7ae`）：**1536 passed / 7 skipped**（158.55s；CI 同刻 macOS
  1536 / 7、ubuntu 1535 / 8），Windows **5 failed / 1509 passed / 29 skipped**（939.99s）。
- 一句话：**第一轮把「不需要网络 / MCP / 子代理、不需要新依赖、不需要 Windows 实机」的
  W1–W5 落地；G1 轮次再补上 §5.2.3 通用工具准入与 §5.6 共享预算账本（账本默认关闭，
  见 §4）。§7.1 语义摘要、G2、G3 平台实机仍未做，逐条登记，不冒充完成。**

## 1 逐条对账

| PRD 要求 | 状态 | 交付物 | 证据 |
|---|---|---|---|
| §10.2 判据边界、§10.4 四维评测 | **已交付** | `eval/report.py` 的 `dimensions`（safety / completion / reliability / efficiency 独立记录）；`Check.path_coverage`；`Observation.invalid_reason` 只收基础设施故障；20 条 safety 判据的负控登记 + 元测试；`SUITE_VERSION` m4.5 → **m4.6**；`compare_reports` 跨版本明确「不可比」 | PR #82；[eval-four-dimensions.md](eval-four-dimensions.md)；`tests/test_eval_four_dimensions.py`（19）、`test_eval_negative_controls.py`（11）、`test_eval_real_tasks.py`（20） |
| §10.5 真实用户任务集 | **已交付（harness 域）** | `eval/tasks.py` 三类任务、6 条带版本（本地资料整理 ×2 / 受控文件修改 批准·拒绝·撤销冲突 / 联网研究「未具备能力」）；报告 `scope=real_user` 与契约基线分开计数；真实模型 opt-in，缺凭据逐字写「未跑真实模型，不伪造分数」 | PR #82；[real-user-tasks.md](real-user-tasks.md) |
| §10.6 本地运行指标 | **已交付（基础件）** | 新 `metrics.py`：启动冷/热、取消 ack/stop、重启恢复、RSS/CPU/磁盘、事件头、慢订阅者计数、p50/p95；缺数据 `unknown` 不写假 SLO；指标只落本机 | PR #81；[runtime-metrics.md](runtime-metrics.md)；本机实测：启动冷 6.68ms / 热 1.95ms、取消 ack p50 0.33ms、stop p50 385.6ms、慢订阅者窗口控制面 p50 40.7ms / p95 45.9ms |
| §10.6 控制面不被慢订阅者阻塞 | **已交付（真 bug 修复）** | `daemon._main_loop` 原先同步 `conn.send()` 推送，一个不读的订阅者会把状态/审批/取消/shutdown 一起堵死；改为每连接**有界发送队列（1024）+ 独立发送线程**，队列满/发送超期 → 断开 + 记 `slow_subscriber_dropped`，重连按 `since_seq` 补读 | PR #81；负控：把推送改回同步 `raw.send` → `ConnectionClosed: 等待响应超时`（1 failed） |
| §8.1.1 隔离门禁 | **已交付（门 + 本机证据）** | 新 `isolation.py`：四边界**真实子进程**探针（工作区外读写+权威库 RW / loopback 网络 / 哨兵凭据 / 杀树后孙进程存活）、三态结论、`require_isolation` / `require_capability` 只认探针证据、`unknown` 与 `not_enforced` 同样阻断；本机 local 后端：文件/网络/凭据 `not_enforced`、进程树 `enforced` → 三个依赖能力全部关闭 | PR #79；[isolation-matrix.md](isolation-matrix.md)；负控：假后端自称 `app_container` 但探针不通过 → 门拒绝；把 local 探针改成 `enforced` → 8 条测试红 |
| §8.5 一致备份 / 恢复 / 保留期 | **已交付（库 + 演练）** | 新 `backup.py`：SQLite 在线备份 API 取一致点、导出事件库/快照/blob/清单、`backup.json` 哈希与引用、原子改名、中断产物不被当有效；`verify_backup` 拒缺 blob/坏清单/版本不兼容；`restore_backup` 只允许干净根，恢复后核对未决 intent/审批/租约；保留期与引用保护可测试 | PR #80；[backup-restore.md](backup-restore.md)；恢复演练：原根 rename 走后恢复到原路径，300KB 外部 payload 与快照 preimage 完整 |
| §5.3.5 第 3 条：迁移中断 | **已交付（库 + 演练）** | 新 `migrate.py`：结构版本（`user_version`/旁表）、步骤注册表、每步进度持久化、迁移前要求 `verify_backup` 通过、`crashpoint` 每步注入硬退、重启续做、不 UPDATE/DELETE 历史事件 | PR #80；[backup-restore.md](backup-restore.md)；负控：去掉备份前置 / 迁移先删后建 / 去掉中断保护，各红 |
| §5.3.5 第 1–2 条：payload 契约与旧版映射 | **已交付** | `event_schema.py`：41 个类型逐类 payload schema（严格子集、注册期 fail-closed）；`events.py`：`event_versions` 旁表与事件行同事务、`append(legacy_write=False)` 默认只收 v1、显式 legacy 才写 v0、读取按旁表给精确版本、旁表缺失/损坏 → `PayloadVersionUnavailable` 不猜；41 类型 v1/v0 夹具 + 旧版映射测试 | PR #84；[event-payload-contract.md](event-payload-contract.md)；负控 ①–④（去掉写入校验 / v0 按新版补值 / 恢复自动回退 v0 / legacy 变万能开关）各红 |
| §1.4 / §3.4 / §13.6 文档基线 | **已交付（文档）** | 新增章节合入 `main`（§5.2.1 的冲突按「记忆工具 5.2.1 / 记忆契约增量 5.2.2 / 通用工具准入 5.2.3」编号，保留既有记忆章节） | PR #77 |
| §5.2.3 通用工具准入 | **已交付（G1a，PR #91）** | 新 `toolschema.py`：封闭 schema 方言（对象/数组/标量三组白名单）、注册期拒绝 `oneOf` / `$ref` / `format` / `default` 等方言外关键字、执行期**嵌套**校验（`argv[0]`、`minItems`、`minimum/maximum`、`enum`、`pattern` 真正执行）；准入在派发前（非法参数不产生审批）；`ActionRequest.description_hash` 绑描述（条件字段，历史摘要逐字节不变）；`MODEL_FORBIDDEN_ARGS` 身份字段双闸；路径参数拒绝 `scheme://`（URL 不伪装成路径）；replay 仍只读事件 | PR #91；[tool-admission.md](tool-admission.md)；[SPEC-G1-ADMISSION-2026-11-11.md](SPEC-G1-ADMISSION-2026-11-11.md) §2；`tests/test_g1_admission.py`（19）；负控 9/9 实测红 |
| §5.6 共享预算账本 | **已交付（G1b，库 + 主循环接线；默认关闭，见 §4）** | 新 `budget.py`：账户/上限/计数器/预占四张表建在事件库同一个 SQLite 文件；`reserve()` 在 `BEGIN IMMEDIATE` 内逐级检查并扣减；`settle()` 幂等 + 分维度（`partial`）；未知用量保留占用不记零；父子链、`cancel()` 沿子树传播、`reconcile()` 只报告不清零；`Limits.budget_limits` 打开后每次模型 attempt 前预占，预占失败 → 记 `model.error` 并停止，**一次模型调用都不发生**；`run.cancel` 传播到账本 | PR #92；[budget-ledger.md](budget-ledger.md)；`tests/test_budget_ledger.py`（20，含两条独立 SQLite 连接的真并发）；负控 10/10 实测红 |
| §7.1 模型语义摘要 | **未做（本轮）** | 保留现有确定性卸载/裁剪（`context.py` + `loop._persist_spills`），不实现模型摘要 | 同上 |
| G2 网络 / MCP / 子代理 | **未做** | 没有新能力；§10.5 的联网研究任务以「未具备能力」的诚实性判据纳入，不用脚本或静态网页冒充线上完成 | [real-user-tasks.md](real-user-tasks.md) §5 |
| G3 平台实机 | **未做** | 只有 macOS arm64 本机证据；Windows / mac-x64 / win-arm64 的安装、升级、回退、隔离证据均为 **unknown**。收尾轮把 Windows CI 从 35 failed 压到 **5 failed / 1509 passed / 29 skipped**（逐条见 §3），仍不是绿、也没有实机 | [isolation-matrix.md](isolation-matrix.md) §1、[runtime-metrics.md](runtime-metrics.md) §7、[backup-restore.md](backup-restore.md) §9 |

## 2 本轮合入的 PR

| PR | 内容 |
|---|---|
| #76 | 施工规格（本文件的上游） |
| #77 | PRD 合入工程评审新增章节（保留记忆章节；通用准入编号为 §5.2.3） |
| #79 | §8.1.1 隔离探针与门 + 本机矩阵 |
| #80 | §8.5 一致备份恢复 + §5.3.5 迁移中断 |
| #81 | §10.6 本地指标 + 慢订阅者隔离（真 bug） |
| #82 | §10.2–§10.5 四维评测 + 判据负控 + 真实用户任务集 |
| #84 | §5.3.5 逐类型 payload 契约 + 严格写入 + 精确版本 |
| #87 | Windows 兼容：`fsync` 只读句柄降级 + 隔离测试的「未知」口径（Windows 35 → 8 failed） |
| #88 | Windows 运行时（`perf_counter` 启动计时 / `OpenProcess` pid 判定）+ 两个 CI 暴露的并发缺陷（IPC 握手期 EOF、指标并发读半行）；Windows → **5 failed / 1509 passed / 29 skipped** |
| #90 | 第一轮的交付对账收尾（1536/7 基线、Windows 逐条证据、两个并发缺陷的语义写入文档） |
| #91 | G1a §5.2.3 通用工具准入：封闭 schema 方言、注册期拒绝、嵌套校验、描述绑定、身份字段双闸、URI 拒绝（+19 tests） |
| #92 | G1b §5.6 共享预算账本：原子预占、幂等分维度结算、未知不记零、父子链、取消传播、主循环接线（+20 tests） |

## 3 测试与门禁

- 首轮 `main @ c0dfa88`：**1519 passed / 7 skipped**（170.12s，macOS arm64 本机），
  `ruff check .` 干净。基线 `da84d92` 为 1316 / 6：五个工作流新增 **185** 条
  （W1 +50、W2 +15、W3 +13、W4 +31、W5 +76），其余 +18 来自并行的记忆工作流（#78/#83）。
- 收尾 `main @ ec7d7ae`：**1536 passed / 7 skipped**（158.55s，macOS arm64 本机；CI 同刻
  macOS job 也是 1536 / 7，ubuntu 1535 / 8）。相对 c0dfa88 的 +17 = 并行记忆工作流 #85 的
  +10（其交付口径为 1529 基线）+ 收尾轮 +7：Windows 运行时修复回归 +1、IPC 握手期 EOF +3、
  指标并发读 +3。`ruff check .` 干净。
- G1 轮次 `main @ cc2240e`：**1601 passed / 7 skipped**（169.08s，macOS arm64 本机），
  `ruff check .` 干净。相对 1536 的 +65 = G1a +19（`test_g1_admission.py`）+ G1b +20
  （`test_budget_ledger.py`）+ 并行记忆工作流 #89 的 +26（同轮先合）。本机与 CI 的
  required checks（ubuntu / macOS / ui）一致通过；Windows 见下。
- **Windows job 的实测（不再用「#50 家族」一句话带过，逐轮、逐条列）**：
  - 修复前（PR #87 前）：**35 failed**；
  - PR #87（`b66570d`）：**8 failed**；
  - PR #88（`2cea014`，合入 main 之前）：**6 failed / 1496 passed / 27 skipped**（869.30s），
    失败：`test_authority_block` ×2（子进程 stdout 在 cp1252 下编码中文即 `UnicodeEncodeError`）、
    `test_host::test_foreign_host_header_is_rejected`（503，被前序命名管道失败带崩）、
    `test_ipc::test_subscription_drop_raises_instead_of_returning_none`
    （`multiprocessing.context.AuthenticationError: digest sent was rejected`）、
    `test_ipc::test_stale_endpoint_is_replaced_on_start`（残留 `\\.\pipe\AgentOS-*`）、
    `test_resources::test_workspace_uri_percent_encodes_and_round_trips`（`%2F` 编码）；
  - **`main @ ec7d7ae`（收尾态，run 42964542067）：5 failed / 1509 passed / 29 skipped**
    （939.99s），失败仅剩：`test_authority_block` ×2（cp1252 编码）、
    `test_host::test_state_requires_token`（503，同一命名管道族）、
    `test_ipc::test_stale_endpoint_is_replaced_on_start`（`FileNotFoundError`，管道名不是文件）、
    `test_resources::test_workspace_uri_percent_encodes_and_round_trips`（`%2F`）。
    与 2cea014（6 条）相比**少了 1 条**：ipc 订阅断线的认证错误消失（握手期异常类型化后
    不再漏出）；host 那条换了落点（`test_foreign_host_header_is_rejected` →
    `test_state_requires_token`，宿主桥在命名管道失败后的连带表现，属同一族），其余 4 条不变。
  - **G1 轮次（PR #92）**：resync 前的运行 42979331348 是
    **5 failed / 1548 passed / 29 skipped**（998.29s），失败仍是既有族（authority 子进程
    cp1252、命名管道相关、资源 URI `%2F`），没有新增 Windows 失败；resync 后的运行
    42982002705 的 Windows job 在 **25m11s** 被取消（Pytest 未跑完、无失败摘要，判定为
    既有命名管道阻塞族的偶发挂死，与 #88 那次同一现象，不是本轮新增用例：本轮新增的
    POSIX 专属用例在 Windows 上是 skip）。required checks（ubuntu / macOS / ui）在两次
    运行都是 pass；**仍然没有实机，不写成 Windows 已通过**。
  - **一次 Windows job 超时取消（如实记录）**：PR #88 的 run 42963301006 在 `90393c8` 上
    Pytest 跑到 51% 后停住，25 分钟上限被取消（同一棵树在 `main` 上 15:39 跑完）。日志最后
    打印位置在 `tests/test_ipc.py` 段内、无失败摘要；判定为既有 Windows 命名管道阻塞族
    （issue #50）的偶发挂死，**不是**本轮新增用例：本轮新增的 POSIX 专属用例在 Windows 上
    是 skip，唯一跨平台的新用例是打桩 `ipc._Socket` 的纯内存断言。
- required checks（ubuntu / macOS / ui）在收尾态全绿；Windows 不是 required check，
  也没有拿它冒充「Windows 已通过」。
- 每个工作流的关键修复都有负控，原始红样写在各自文档与 PR body 里；没有为了让测试
  变绿的判据放宽（唯一的既有判据调整是：`tests/test_events.py` 两处、`test_credentials.py`
  一处按新契约换最小合法 payload，断言未动；`test_job_claims.py` 两处夹具跟上当前
  `run.model_turn` 契约，断言未动——旧口径→新口径清单在 PR #84 body 里）。
- 收尾轮的两个并发缺陷（都只在 CI 时序下暴露，本机连续跑绿）：
  ① **IPC 握手期裸 `EOFError`**：macOS job 上 `test_event_stream_ends_when_daemon_goes_away`
  红——daemon 已退出、端点文件还在，`DaemonHarness.__exit__` 补发的 `request_shutdown()`
  撞进握手窗口，`multiprocessing` 的 `answer_challenge()` 抛 `EOFError`（不是 `OSError`），
  穿过 `Client.connect()` 的重试分支打崩只捕 `(IpcError, OSError)` 的调用方。修法：映射成
  `ConnectionClosed` 且**不重试**；三条回归 + 负控（撤修复即红）。
  ② **指标并发读的半行**：ubuntu job 上慢订阅者用例读到断在第 37 字符的 JSON 行，
  `read_metrics()` 把"正在写的最后一行"判成损坏。修法：没有 `\n` 收尾的尾段先试解析、
  失败跳过（`tail -f` 语义），带换行的坏行仍报错；三条回归 + 负控。

## 4 仍然不成立 / 未验证的宣称（不许写进「已完成」）

- **G1（部分）**：§5.2.3 与 §5.6 的模块与接线已交付，但下面这些仍**不成立**：
  ① 共享预算账本**默认关闭**（`Limits.budget_limits` 为 None 时行为与之前一致）——
  估算预占会让临界 run 提前被拒，属行为变更，需单独评审；G2 接入前必须默认打开。
  ② 网络 / MCP / 子任务三种资源种类没有实现（`resources.RESOURCE_KINDS` 仍是 file + memory），
  所以「私有内容未经外发许可不发包」只能是**无从违反**，不是已验证。
  ③ 「大结果有界摘要 + 内容引用 + 分页读取并验证任务可见范围」没做（只有 `read_text`
  截断与 `run_command` blob 目录）。
  ④ `run_command` 仍只受显式 `ExecPolicy` 约束，不经过隔离门；本轮没有新增高自主执行能力，
  所以没有新增 §8.1.1 门禁项。
  ⑤ 硬上限模式的「可验证上界」没做：`estimate_tokens()` 是 4 字符≈1 token 的本地估算。
- **§7.1**：没有模型语义摘要；只有确定性裁剪/卸载。
- **G2**：没有网络 / MCP / 子代理能力；联网研究任务只验证「如实报告未具备」。
- **G3**：无 Windows / mac-x64 / win-arm64 实机；安装、升级、回退、凭据存储（Keychain）、
  平台 SLO 均未验证。macOS 13+ 最低版本未实测。Windows 收尾态仍是 **5 failed**（既有族），
  且有过一次命名管道阻塞导致的 25 分钟超时取消——两者都按实测写在 §3，不当作绿。
- **各工作流内部未验证项**：换路径恢复（`payload_ref` 是绝对路径）、迁移写入门未接
  CLI/daemon、备份期间没有停止写者（只压小窗口）、指标 SLO 未冻结、`state` 响应可超
  协议 8MiB（`control.py` 所有，已登记未修）、`oneOf`/`anyOf`/`$ref`/`format` 未实现
  （注册期拒绝）、每工具 output 子契约未做（C2 后续）、隔离探针不覆盖内核攻击面/配额/
  出站网络。
- **真实模型**：四维评测与真实用户任务集的真实模型复跑仍缺凭据；harness 分数不得当
  真实模型结论。
- **备份 ≠ 快照 ≠ 撤销**：三者语义在 [backup-restore.md](backup-restore.md) 里写死，
  不得互相当作完成。

## 5 下一轮建议（按 PRD §11.1 的门禁顺序）

1. **G1 收口（下一轮）**：把共享预算账本默认打开（先定余量 / 输出预留 / 拒绝提示），
   补网络 / MCP / 子任务资源种类与「有界摘要 + 内容引用」分页，再谈 G2。
2. **§7.1**：摘要材料先持久化再编译；replay 读保存的摘要；断网回放测试。
3. **G2**：网络 → MCP → 记忆 → 子代理，按「同一任务集证明收益」推进。
4. **G3**：历史数据升级/备份恢复、凭据存储、运行 SLO、长时间测试、安装/更新/回退与
   四目标平台矩阵；Windows IPC（issue #50）根因修复后才谈把 Windows 纳入 required。
