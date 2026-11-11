# PRD 2026-11-11 工程评审新增要求的交付对账（PRD-REVIEW-DELIVERY）

- 立据：[PRD-v1.md](PRD-v1.md) 的 2026-11-11 工程评审合并（§1.4、§3.4、§5.2.3、§5.3.5、
  §5.6、§7.1、§8.1.1、§8.5、§10.2–§10.6、§11.1、§13.6，附录 B 第 9–14 条），施工规格见
  [SPEC-PRD-REVIEW-2026-11-11.md](SPEC-PRD-REVIEW-2026-11-11.md)。
- 基线：`main @ c0dfa88`（PR #84 之后）。实测 **1519 passed / 7 skipped**（macOS arm64 本机，
  170.12s），`ruff check .` → `All checks passed!`。CI 的 required checks
  （ubuntu-latest / macos-latest / `ui`）在每个合入 PR 上都是绿的；Windows job 仍红，
  属 issue #50 的命名管道握手族，**不是** required check。
- 一句话：**本轮把 PRD 新增要求里「不需要网络 / MCP / 子代理、不需要新依赖、不需要 Windows
  实机」的部分落地并合入 main；G1 的通用准入与共享预算、§7.1 语义摘要、G2、G3 平台实机
  仍未做，逐条登记，不冒充完成。**

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
| §5.2.3 通用工具准入 | **未做（本轮）** | 与并行记忆工作流同改 `dispatcher.py` / `tools.py` / `loop.py` / `permissions.py`，本轮不做 | [SPEC-PRD-REVIEW-2026-11-11.md](SPEC-PRD-REVIEW-2026-11-11.md) §9 |
| §5.6 共享预算账本 | **未做（本轮）** | 同上；现实现仍只有单 Run 的 usage/cursor 与按上次成功用量的重试估算 | 同上 |
| §7.1 模型语义摘要 | **未做（本轮）** | 保留现有确定性卸载/裁剪（`context.py` + `loop._persist_spills`），不实现模型摘要 | 同上 |
| G2 网络 / MCP / 子代理 | **未做** | 没有新能力；§10.5 的联网研究任务以「未具备能力」的诚实性判据纳入，不用脚本或静态网页冒充线上完成 | [real-user-tasks.md](real-user-tasks.md) §5 |
| G3 平台实机 | **未做** | 只有 macOS arm64 本机证据；Windows / mac-x64 / win-arm64 的安装、升级、回退、隔离证据均为 **unknown**；Windows CI 红属 issue #50 族 | [isolation-matrix.md](isolation-matrix.md) §1、[runtime-metrics.md](runtime-metrics.md) §7、[backup-restore.md](backup-restore.md) §9 |

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

## 3 测试与门禁

- `main @ c0dfa88`：**1519 passed / 7 skipped**（170.12s，macOS arm64 本机），
  `ruff check .` 干净。基线 `da84d92` 为 1316 / 6：本轮五个工作流新增 **185** 条
  （W1 +50、W2 +15、W3 +13、W4 +31、W5 +76），其余 +18 来自并行的记忆工作流（#78/#83）。
  新增的 1 条 skip 是默认跳过的浸泡用例（`TIANSHU_SOAK=1` 才跑）。
- 每个 PR 的 required checks（ubuntu / macOS / ui）都是绿的；Windows job 仍红，
  失败集合与同刻 `main` 的既有失败一致（issue #50 族 + runner winsock 故障），
  **不是 required check**，也没有拿它冒充「Windows 已通过」。
- 每个工作流的关键修复都有负控，原始红样写在各自文档与 PR body 里；没有为了让测试
  变绿的判据放宽（唯一的既有判据调整是：`tests/test_events.py` 两处、`test_credentials.py`
  一处按新契约换最小合法 payload，断言未动；`test_job_claims.py` 两处夹具跟上当前
  `run.model_turn` 契约，断言未动——旧口径→新口径清单在 PR #84 body 里）。

## 4 仍然不成立 / 未验证的宣称（不许写进「已完成」）

- **G1**：§5.2.3 通用准入、§5.6 共享预算账本未实现；`run_command` 仍只受显式
  `ExecPolicy` 约束，不经过隔离门。
- **§7.1**：没有模型语义摘要；只有确定性裁剪/卸载。
- **G2**：没有网络 / MCP / 子代理能力；联网研究任务只验证「如实报告未具备」。
- **G3**：无 Windows / mac-x64 / win-arm64 实机；安装、升级、回退、凭据存储（Keychain）、
  平台 SLO 均未验证。macOS 13+ 最低版本未实测。
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

1. **G1**（等并行记忆工作流合入后）：§5.2.3 通用工具准入（资源种类 / 完整嵌套 schema /
   固定版本与描述摘要 / 系统提供的执行上下文）+ §5.6 共享预算账本（原子预占、attempt
   幂等结算、未知费用、取消传播、父子预算）。
2. **§7.1**：摘要材料先持久化再编译；replay 读保存的摘要；断网回放测试。
3. **G2**：网络 → MCP → 记忆 → 子代理，按「同一任务集证明收益」推进。
4. **G3**：历史数据升级/备份恢复、凭据存储、运行 SLO、长时间测试、安装/更新/回退与
   四目标平台矩阵；Windows IPC（issue #50）根因修复后才谈把 Windows 纳入 required。
