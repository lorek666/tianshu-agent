# SPEC · 文档对账修复（2026-11-14）

> 基线：`main@0169109`（PR #139 合并后）。本文是一次**纯文档对账**的执行规格：先写规格、后动手。所有行号以该基线为准，修复后行号会漂移，定位以「文件 + 关键短语」为准。

## 0. 背景与来源

本日两轮核查：

1. **外部审计核实**：G3 缺口清单基本属实（30+ 条逐项核实），5 处需修正（落点见 §4）。
2. **文档漂移对账**：三路核查（入口文档 / PRD+TECH-ARCH+契约 schema / 验证验收文档）+ 机械扫描（596 个反引号引用、全部 markdown 链接、11 个 issue/PR 状态核对），发现约 25 处漂移。

修复分三批：**批次 A** 普通文档（本 PR）；**批次 B** 契约口径（`docs(contract)` 单独 PR，不夹带代码）；**批次 C** GitHub 流程（issue #50 关闭、#55 评论更新）。

正面结论（已核实、无需再查，供后续对账复用）：C3 事件 45 类型与 C4 规则 75 条三处（代码 / enum / PRD）**零差集**；m2 五份 schema 与 dataclass 绑定测试在位；C1/C2/C5 无契约 schema、SLO 留空、state 超 8MiB 未修、isolation 三文档口径一致均为诚实登记非漂移；README CLI 面与 argparse 全部对得上；AGENTS/CONTRIBUTING 的 CI 检查名与 ci.yml 逐字一致。

## 1. 批次 A：普通文档对账（本 PR）

| ID | 位置（基线行号） | 原陈述（摘要） | 改为 | 依据 |
|---|---|---|---|---|
| A1 | docs/KNOWLEDGE.md §2（:29） | #139 按「候选快照不预先宣称合并」陈述 | 更新为终态：#139 已合入 main（`0169109`，2026-11-14） | git log |
| A2 | KNOWLEDGE.md §2（:113） | 58 个模块 / 93 个测试文件（2026-11-12 实测） | 63 个模块 / 117 个测试文件（2026-11-14 实测） | `ls src/tianshu/*.py \| wc -l` = 63；`ls tests/test_*.py \| wc -l` = 117 |
| A3 | KNOWLEDGE.md §3（:132） | C2「19 个 ToolSpec、15 个对模型开放」 | 23 个 ToolSpec、19 个对模型开放（4 个 exposed=False 不变） | import 实测 23/19 |
| A4 | KNOWLEDGE.md §3（:135） | C5 套件 `m4.4` | `m4.6`（与本文 §8 m4.6 条目对齐） | `from tianshu.eval import SUITE_VERSION` → m4.6 |
| A5 | KNOWLEDGE.md §7（:542） | 「`run.end` 不携带成本汇总（约 6 行改动挂账）」 | 改为「`run.end` 不带**货币成本**汇总（`cost_total_micros`，成本经游标账本进 `run status`）；token/turns/`budget_enforced` 汇总已是 run.end 的 required 字段」 | tasks.py `_run_end_payload`（:1845-1896）、event_schema.py run.end required（:831-856）、cli.py:624 |
| A6 | KNOWLEDGE.md §7（:169） | 引用「PRD §14」 | 改指 PRD §7 功能表 F14 行 | PRD 顶层只有 §1–§13 + 附录 |
| A7 | KNOWLEDGE.md §7（:509） | 「没做独立发送线程 + 非阻塞写」 | 加「2026-11-14 对账」注：PR #81 已交付有界队列 + 独立发送线程，与本文 :200/:345/:458 及 M5-verification:385 对齐 | daemon.py:111 `SEND_QUEUE_MAX`、daemon.py:361-369、tests/test_slow_subscriber.py |
| A8 | KNOWLEDGE.md §7（:485/:499 vs :534/:643） | #55 剩余条数两种口径并存（1 条 vs 2 条） | 旧条目加「2026-11-14 对账」注：m4.4 真实换模 / memfs / 真超时已销账，剩 m4.5 真实复跑 + Windows 实机两条 | M4-verification §7.4、issue #55 评论（2026-11-11） |
| A9 | KNOWLEDGE.md §8 | 无 #139 合入条目 | 追加 2026-11-14 会话日志条目（#139 合入记录 + 本次对账修复，含两 PR 与批次 C 动作） | AGENTS §二.4 |
| A10 | README.md（:70） | issue #40「工单仍开着等确认」 | 已 closed（PR #48 已修，#53/#54/#40 已销账） | gh api issues/40 → closed |
| A11 | README.md 退出码表（:240） | 「`2` … 含 `recover` 发现未核实效果」 | recover 未核实效果退 **1**（并入第 1 行）；2 行去掉 recover | cli.py:1200 `return 0 if report.ok else 1` |
| A12 | README.md（:277） | C5 `SUITE_VERSION = m4.4` | `m4.6`（:366 的 m4.4 历史实测口径保留并注「现为 m4.6」） | 同 A4 |
| A13 | README.md 架构树（:280） | 「49 个 .py（47 实义）」，树缺约 14 模块 | 「63 个 .py（61 实义 + 2 入口）」；树补缺失模块（authority/backup/budget/data_cli/data_admission/mcp/memory_control/memory_index/memory_distill/metrics/migrate/net/summaries/tool_registry/undo_journal 等） | `ls` 实测 |
| A14 | README.md（:433） | 「必过的 CI 检查（ubuntu + macOS）」 | 四项 required：Ubuntu / macOS / Windows Python / UI（与本文 :386、CONTRIBUTING §6 一致） | ci.yml 4 job |
| A15 | AGENTS.md 快速定位（:96） | 只列 M0–M4-verification | 补 M5-verification.md | docs/M5-verification.md 存在（M5 已交付） |
| A16 | docs/M0-verification.md §2.2（:62） | A7 待办（arguments 三形状）未勾 | 勾销：PR #139 INPUT04 已覆盖 missing/null/array（tests/test_round2_inputs_model_mcp.py:101-103，双 provider 参数化） | 测试在位 |
| A17 | docs/M1-verification.md §6（:137） | 「未测 fsync 边界」 | 加对账注：PR #71 已测持久化边界注入 + 撕裂写 + 真实 HFS+ 摘卷（tests/test_crash_consistency.py、docs/powercut-evidence.md）；仅剩块设备级断电未验（与 M2 §8 同口径） | M2-verification:370 |
| A18 | docs/M3-verification.md §5（:128） | 第 9 条「restore_file 可被模型提案（issue #19）」 | 加已修注：PR #42（exposed 即能力边界），issue #19 已 closed | gh |
| A19 | docs/M4-verification.md §7（:60） | 「windows-latest 偶发挂住（issue #50），不挡合并」 | 加历史注：PR #130 后已修绿并进 required；#50 根因 2026-11-13 定位为 authkey 文件缺 `O_BINARY`（LF→CRLF），见 WINDOWS-CI-2026-11-13.md | run 43283015540 success |
| A20 | docs/M4-verification.md §7 第 1 行（:183） | 「真实换模 已验（2026-11-10）」无限定 | 加限定「（m4.4 套件；m4.5 起判据不可比，见 §7.4）」 | §7.4 |
| A21 | docs/M5-desktop.md §9（:343） | 「真实换模分数…同样没做」 | 加限定：m4.4（2026-11-10）已跑两份真实报告但判据不可比；m4.5 复跑被 503 阻塞 | M4 §7.1/§7.4 |
| A22 | docs/M5-desktop.md §10（:357-359） | 「推送在主循环里同步发送…没有做这一层」 | 加对账注：PR #81 已交付有界队列 + 独立发送线程（M5-verification:385 已有注，本文补齐） | daemon.py:111 |
| A23 | docs/M5-verification.md §6.5（:307-317）/§8（:372-373） | 「根因未定位 / 须实机根治 / CI 仍偶发握手失败」 | 加「2026-11-14 对账」注：根因已定位并修复（authkey 缺 `O_BINARY`），旧判断被 WINDOWS-CI-2026-11-13.md:54 显式推翻；修后四 job 全绿（run 43283015540） | 同上 |
| A24 | docs/backup-restore.md §0（:40） | 「T3 独立待验」（与 :3「均已独立验收」矛盾） | §0 行更新为已通过独立复审（DATA-ADMISSION-2026-11-13.md:19 `PASS_TASK3_CONTENT_ONLY`，未决 0） | 同 |
| A25 | docs/authority-takeover.md（:4）、docs/powercut-evidence.md（:5） | 链接 `../ts-wt-lead/docs/SPEC-RESIDUALS-M0M1.md`（worktree 已消失） | 改指同目录 `SPEC-RESIDUALS-M0M1.md`（`## T1` :45、`## T2` :83 在文内） | ls / grep |
| A26 | KNOWLEDGE.md §9 之后（:699-707） | 4 条 2026-11-13 会话日志条目被追加在 §9「权威文档索引」之后 | 移回 §8 会话日志（内容不变，只调位置） | AGENTS「在 §8 会话日志追加」 |

## 2. 批次 B：契约口径（`docs(contract)` 单独 PR）

PR body 按「原方案 → 新方案 → 为什么 → 影响哪些代码」；PRD 与 TECH-ARCH 两文件、一个 PR、分两个 commit。

| ID | 位置（基线行号） | 原方案 | 新方案 | 依据 |
|---|---|---|---|---|
| B1 | PRD §5.3 草图（:570）+ §5.3.4 未封闭清单（:669） | 「payload 统一 object；逐类型字段形状尚未冻结」列为未封闭首位 | 移出未封闭清单 → 标「已封闭（v1）：45 类型各有 payload v1 schema + `event_versions` 旁表」，引用 `schemas/c3/event.json` `$defs.payload` 与 `docs/event-payload-contract.md`（PRD 此前未引用该文件） | event_schema.py:475 `CURRENT_SCHEMAS`、events.py:451/:497、schemas/c3/event.json |
| B2 | PRD §5.4（:693） | 审批态枚举含 `proposed` | 删 `proposed`（封闭集 7 态，与机器契约一致） | permissions.py:57 `APPROVAL_STATES`、schemas/m2/approval.json |
| B3 | PRD §5.4（:690） | Grant 草图字段 data_domain/quota/ttl/approval_ref | 加注：字段级机器契约以 `schemas/m2/grant.json` 为准（request_digest/issuer/remaining_uses/expires_at/run_id/task_id），草图字段名不再逐一对应 | permissions.py:405-416、tests/test_schemas.py |
| B4 | PRD §4.3（:188/:206/:218） | `WAITING_USER` Run 状态；`effect_state` 写作「none」；fencing → `CANCELLING` | 删 `WAITING_USER`（代码无此态；等待人工走 C4 审批对象 + `run.control_requested`）；`none` → `none_applied`（与 :738 一致）；补 `fenced` 终态登记 | tasks.py:148 `EFFECT_STATES`、tasks.py:160 `RUN_FENCED`、schemas/m2/run.json |
| B5 | PRD §8.3（:1152） | 工具名 `read_file` | `read_text`（与 §5.2.3:535 及代码一致；read_file 从未存在） | tools.py:728 |
| B6 | TECH-ARCH §1（:18）/§9（:369） | 「文件级 Event Store / 一事件一文件（OpenHands 式）」 | SQLite（WAL）append-only 单提交点（PRD §5.3 已明文修正该决定） | events.py:1、PRD:577 |
| B7 | TECH-ARCH §5（:152/:159/:161） | ConPTY / Keychain / 签名 三行标 v1 ✅ | 三行改「未实现」并注原因：PTY 仅 POSIX；Keychain = `CredentialUnsupported`；签名不做（与本文 §8.2 自洽） | exec.py:252/:283、credentials.py:3-6、TECH-ARCH:259-261 |
| B8 | TECH-ARCH §9（:372-374） | `memory/sqlite.db`、`daemon.lock` | 记忆与事件同库、正文落 `<db> 旁 memory_blobs/`；daemon 锁在 `run/daemon.lock` | memory.py:816/:825、daemon.py:8/:87 |
| B9 | TECH-ARCH §3.1（:88） | `python -m agentos_daemon --socket …` | `python -m tianshu daemon`（包名 tianshu；agentos_daemon 从未存在） | pyproject.toml:2、cli.py:1379 |
| B10 | TECH-ARCH §8.3（:338）/§11.3（:430） | 「四个目标全部进 CI、任一失败阻断发布」无现状标注 | 加现状注：当前 CI 为三平台矩阵（ubuntu/macos/windows × py3.12）+ UI；四目标 import 冒烟是 G3 发布前要求（PRD §11.1），差距已在 AGENTS/KNOWLEDGE 登记 | ci.yml:41-55、PRD:1369 |

## 3. 批次 C：GitHub 流程

| ID | 动作 | 内容 |
|---|---|---|
| C1 | issue #50 评论 + 关闭 | 根因 = `load_authkey()` 缺 `O_BINARY`（Windows 文本模式 LF→CRLF，创建者/读取者密钥漂移），随 PR #130 修复（进 main 的 merge `4bb1781`）；证据：run 43281774439 的 `test (windows-latest)` job **failure**（真红复现）、run 43283015540 四 job **success**（修后全绿）；issue 内「实例队列竞态」怀疑方向与「必须实机才能定位」「生产不受影响」的旧判断已被 WINDOWS-CI-2026-11-13.md:54 显式推翻。根因修复是生产缺陷修复，不是测试抖动 |
| C2 | issue #55 评论（保持 open） | 更新前提：m4.4（2026-11-10）已有两份真实模型报告（deepseek-v4.1-flash 18/20 unsafe、glm-5.2 15/20 unsafe），「本机无可用 API key，M4 全部是 scripted 基线」不再成立；判据升级 m4.5 后旧报告不可比。当前剩两条：m4.5 真实换模复跑（等中转站恢复）+ Windows `evaluate` 实机（等机器）；memfs / 真超时两条已销账 |

## 4. 审计侧修正落点（①–⑤）

| 修正 | 仓库内落点 | 说明 |
|---|---|---|
| ①「run.end 不带成本汇总」→「不带货币成本汇总（token/turns 汇总已有）」 | **A5**（KNOWLEDGE.md:542——审计该句的仓库内源头） | token/turns/budget_enforced 已是 run.end required 字段；缺的是 `cost_total_micros`（经游标账本进 run status，cli.py:624） |
| ②「现有评测全是脚本模型」→「m4.5 下只有脚本基线；m4.4 有两份真实报告但判据不可比」 | **A20 / A21**（M4-verification:183、M5-desktop:343） | 仓库内无「全是脚本模型」原句，以限定语落地 |
| ③ 仓库卫生一节刷新 | 无仓库内落点 | 审计文本侧修正：#138 已合并（`c1bad0a`）、/tmp/tianshu-lessons-20261113 worktree 已删、worktree 实为 26 个（除主目录）、codex/g3-baseline-windows-probe 现 ahead 5 / behind 4 |
| ④ schemas 文件数 5 → 7 | 无（REVIEW-PRA.md:117「五份」专指 `schemas/m2/*.json`，scoped 表述本身正确） | 审计文本侧修正：schemas/ 全目录为 7 个 JSON（c3/event、c4/rules、m2/ 五份） |
| ⑤ 行号 447→457、ipc.py:86→88、permissions.py:965→969、浸泡日期 11-13→11-11 | 无仓库内落点（runtime-metrics.md 全文无日期） | 审计文本侧修正：陈述在 KNOWLEDGE.md:457；`MAX_MESSAGE_BYTES` 在 ipc.py:88；`decide()` 在 permissions.py:969；180s 浸泡证据时点为 2026-11-11（runtime-metrics.md 最后提交 5f35696） |

## 5. 明确不做

- 不改任何代码 / 测试 / CI 配置；不动 `uv.lock`。
- 不改历史会话日志条目的实义，只追加「对账注」；KNOWLEDGE §8 仅新增条目。
- REVIEW-PRA.md:117「m2 五份」不动（scoped 表述正确）。
- 不宣称平台支持变化；不把 G3 缺口写成已解决。
- 不动 PR #137 与活跃分支 `codex/g3-baseline-windows-probe`。
- README 架构树按实测补全模块名，但不重画分层结构、不改任何代码描述语义。

## 6. 验证方式

- 每处修改引用的依据（file:line / 命令输出）在基线 `0169109` 上已核实；本文行号随编辑漂移，定位以关键短语为准。
- 提交前门禁：`uv lock --check` + `uv run --frozen ruff check .` + `uv run --frozen pytest -o addopts="" -q -rs`（纯文档改动不影响测试，仍按 AGENTS 门禁跑全量）。
- CI：两个 PR 各走四项 required；合并由维护者执行（enforce_admins）。
