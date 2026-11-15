# W15 保留期/GC 契约评估 · 2026-11-15

> 路线图 [ROADMAP-COMPLETE-AGENT-2026-11-14.md](ROADMAP-COMPLETE-AGENT-2026-11-14.md) W15：「保留期/GC（summaries、dispatch_requests、审计表）」，出处 §7 多处挂账（G1 ⑤⑥、G2 边界、§7.1 边界均写「保留期/GC 未做」）。
>
> 本片是 **docs-only 契约评估**：盘点全部持久化面的删除不变量、增长驱动与恢复/审计角色，给出保守 v1 边界与拍板项。**不改代码、不改 schema、不动 C1–C5**；若维护者拍板实现，另立实现 PR（落点 `maintenance.py` / `data_cli.py` 被 OPEN PR #161 占用，需等其落地）。

## 1. 基线约束（评估的前提，不可推翻）

1. **红线 1**：事件日志 append-only，绝不 UPDATE / DELETE——`events` 表本体不在任何 GC 候选内。
2. **红线 2/6**：副作用执行前持久化 intent、执行后写 result + Receipt，Receipt 可独立核对——`dispatches.intent_seq` / `boundary_protocol` 是崩溃恢复正确性的组成部分（`none_applied` 判定依赖 `boundary_protocol` 把升级前的旧行排除在外，[dispatcher.py](../src/tianshu/dispatcher.py) SCHEMA 注释），非终态 Run 的行绝对不可删。
3. **红线 5/11**：`applied` / `none_applied` / `unknown` 三态不合并——`budget_reservations.known` 未决行同理不可删。
4. **数据生命周期既定边界**（[DATA-LIFECYCLE-2026-11-13.md](DATA-LIFECYCLE-2026-11-13.md)）：「不新增删除操作，不扩展 GC 范围或承诺在线 GC 安全」；`backup.cleanup_plan()`（[backup.py:1800](../src/tianshu/backup.py)）是现有唯一清理候选面，带内容对象与未擦除记忆引用保护，且模块注释明说「**人工清理；本模块只给候选清单（cleanup_plan），不执行删除**」（[backup.py:93](../src/tianshu/backup.py)）。
5. **schema 层的反删除不变量**：`summary_versions` 有 `BEFORE DELETE` 触发器（[summaries.py:128](../src/tianshu/summaries.py) `summary_versions_no_delete`）；`dispatch_requests` 终态后 `args_redacted` 脱敏（dispatcher.py SCHEMA 注释：「审计表不该永久保存记忆正文原文……只能在终态之后做」）——隐私面已经用「脱敏」而不是「删除」实现。
6. **摘要表与事件同库且回放依赖**：三张摘要表建在事件库同一 SQLite（KNOWLEDGE §8 2026-11-11 §7.1 轮），回放按 `summary_id` 读回正文并三道校验（行 / 版本与正文摘要 / 材料台账），**缺记录 = problem，不重新生成**——删摘要行直接破坏回放校验。
7. **既有增长兜底**：`DiskAdmission` 空间门槛（维护准入，[maintenance.py](../src/tianshu/maintenance.py)）在磁盘不足时拒绝启动新 Run；指标文件已按大小轮转（2026-11-14，#150，默认 16 MiB × 4 份）。**系统今天不会因无 GC 而写满磁盘后继续写**——它会在门槛处如实拒绝。这是「没有 GC」的现状风险上限。

## 2. 逐面盘点（增长驱动 × 不变量 × 角色）

| 存储面 | 增长驱动 | 删除不变量 / 保护 | 角色 | 初判 |
|---|---|---|---|---|
| `events` | 每事件一行，payload 全量 | 红线 1 | 唯一事实源、回放、审计 | **禁删**。归档后原库收缩 = 红线修订，拍板项 |
| `dispatches` | 每工具调用一行 | `boundary_protocol`/`intent_seq` 恢复语义；`request_digest` 绑定审批 digest | 崩溃恢复 + 审计证据 | 非终态禁删；终态行删除破坏 `unknown` 复核与审批对账，拍板项 |
| `dispatch_requests` | 每请求一行 | 终态后脱敏（隐私已解决） | 审计（digest/reason/rule_id） | 同上；脱敏已把隐私问题与保留问题分离 |
| `summary_versions` | 每（材料 × 版本）一行 | **DELETE 触发器**（summaries.py:128）；回放三道校验依赖 | 上下文编译的持久化材料 | 放宽触发器 = 契约变更，拍板项 |
| `summary_materials` / `summary_sources` | 每材料 / 每来源 | 内容寻址共享；被 versions 引用 | 编译来源追溯 | 禁删（依赖 versions） |
| `jobs` | 每 Run ≥ 1 行 | 「一 Run 一开作业」部分索引只管非终态 | 终态行 = outcome 审计 | 终态行是低风险候选，但 `outcome`/`output_ref` 与 Receipt 关联，先测量再拍板 |
| `budget_reservations` | 每调用一行 | `known` 三态不合并 | 计费对账 | `settled` + `known` 终态行是低风险候选，同上 |
| `budget_accounts` / `budget_limits` / `budget_counters` | 聚合行 | — | 计费事实 | 禁删 |
| `leases` | — | release 即 DELETE（leases.py:461/666） | 自清理 | **无增长问题**（现状已解决） |
| `memories` / `memory_meta` / `memory_vectors` | 用户数据 | erase 只置投影行 `forgotten`，正文副本物理删除走 `memory.erase` 显式流程 + Receipt | 记忆事实 | 禁删（擦除是显式授权流程，不是 GC） |
| `content_objects` / `content_grants` | 内容寻址 | `cleanup_plan` 引用计数保护 | 内容共享 | 禁删（引用保护已就位） |
| `migration_log` | 每迁移一行 | append-only audit | 迁移审计 | 行数 = schema 版本数，无增长问题 |
| `metrics.jsonl*` | 每样本一行 | #150 已按大小轮转 | 已解决 | ✅ |

## 3. 结论：缺的是「测量」与「政策」，不是「删除代码」

1. **没有任何一张表现在「今天就安全可删」**。§7 挂账的「保留期/GC」若按「加 DELETE 语句」实现，会同时踩红线 1/2/5 与 schema 层触发器。三张被点名的表（summaries、dispatch_requests、审计表）恰恰是反删除不变量最重的三张。
2. 增长最猛的面（`events`、`dispatches`/`dispatch_requests`、`budget_reservations`）全部绑红线；而真正无界增长的实际风险上限由 `DiskAdmission` 门槛兜住（如实拒绝而非静默膨胀）。
3. **v1 建议（保守边界）**：交付**只读保留期审计命令**（`data_cli` 子命令：逐表行数、最老行年龄、终态占比、按当前速率的增长投影、`cleanup_plan` 现状输出），**零删除**。它回答「哪里在涨、涨多快、可清理候选有多少」，把拍板所需的数字给出来。可清理候选的判定标准在本片写死：终态 + Receipt 对账通过 + 无恢复依赖 + 不在触发器/红线保护集 + （若做归档）归档校验通过之后。实现待 #161 落地后另立 PR。
4. **拍板项（维护者，不拍板就不做）**：
   - **事件日志归档后原库收缩** = 红线 1 的修订（archive-then-truncate 语义、归档独立校验、回放读归档的接缝），牵动 backup/verify/replay 三条链，代价最大收益最大。
   - **`summary_versions` FAILED 行豁免触发器** = 契约变更，`docs(contract)` 先行。
   - **jobs / budget_reservations 终态行保留期数值**（如 90 天）= 政策拍板，拍了才写代码。
5. **非目标**：在线 GC 安全承诺（DATA-LIFECYCLE 边界不变）；多租户/配额（D2 非目标）；归档压缩加密（未要求）；`memories` 的任何自动删除（擦除只走显式 `memory.erase`）。

## 4. 与在途面的关系

- **#161（restore T1）**：占用 `maintenance.py` / `events.py` / `data_cli.py`——本片 v1 测量命令的实现落点，等其落地。
- **W5（调度器，本轮并行）**：若落地，「定时清扫」是其潜在调用方，但清扫本体在本片拍板前不存在——无依赖关系。
- **#140/#141（文档对账）**：与 KNOWLEDGE/PRD 相交，合并时按 modify-modify 并集法处理（W 系列合并轮同法）。
- **v1 审计命令（本片 §5）**：已在 `feat/retention-audit` 交付并单独 docs(contract) 记录；W5 调度器是未来「定时清扫」的潜在调用方，清扫本体在三项拍板前不存在。

## 5. v1 落地记录（2026-11-15，docs(contract) 单独提交）

- **原方案（§3.3）→ 落地**：`data_cli` 子命令 `retention-audit`（`tianshu data retention-audit --data-root`）按本片划界交付——逐表面测量、增长投影、候选测量、cleanup_plan 现状，**零删除不变量成立**（mode=ro 连接 + 测试钉住 events.db 字节哈希不变）。实现与测试在 `feat(data)` commit，不夹带本文件以外的契约陈述。
- **拍板流程变更**：三项拍板（§3.4）由维护者 2026-11-15 委托定为「**先测量后拍板**」——先以审计命令取得逐表面实测数字，再逐项拍板；本片不因此改写 §3.4 的「不拍板就不做」边界，三项数值/红线修订仍留维护者。
- **实现中的边界澄清**（不改变上文契约）：增长投影仅在观测窗口 ≥1 天时外推（不足则如实返回 null）；候选测量的终态判定只用代码权威常量（jobs `TERMINAL_JOB_STATES`、runs `NON_TERMINAL_STATES`、budget `SETTLED`+known）在 Python 侧解释，dispatch 面只报直方图不猜终态语义——与 §1.2/§1.3 的「三态不合并」一致。
- **影响哪些代码**：`src/tianshu/data_cli.py`（只读审计路径）、`tests/test_data_cli.py`（+4 条）；schema、RPC、事件、UI 零变更。
