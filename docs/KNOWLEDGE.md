# 天枢 Agent · 项目知识库

> **AI 开发必读必更。** 每次开发前读本文，结束后更新本文。强制指令见根目录 [AGENTS.md](../AGENTS.md)。
> 本文是「活文档」：蒸馏决策、架构要点、踩坑教训与当前状态，供任何一次开发会话快速上手。
> **权威规格仍以 [PRD-v1.md](PRD-v1.md) 为准**——本文与 PRD 冲突时以 PRD 为准，并应顺手修正本文。

---

## 0. 这份文档怎么用（给 AI）

- **开发前**：读 §1–§7，尤其 §5 踩坑教训与 §6 流程规则；改契约前先读 PRD §5/§6。
- **开发后**：
  - 状态变了 → 更新 §2 / §7；
  - 新决策 / 新教训 → 追加 §4 / §5；
  - **在 §8 会话日志追加一条**（日期 + 做了什么 + 发现什么），这是硬性要求。
- **对账**：本文任何「已实现什么 / 多少测试 / 覆盖哪些平台」的陈述必须与当前代码和 CI 一致，不留假话。数字会过期，更新时重新核实。

## 1. 项目是什么

- **目标**：面向普通用户的桌面 Agent OS——模型自选、在用户指定目录读写、完成长任务；把「AI 可能失控」当一等工程问题。
- **当前形态**：**M0–M5 已完成**。M0–M4 是命令行内核；M5 交付三进程桌面闭环（Daemon + Host + 浏览器里的 React UI），但**外壳是系统浏览器**，没有 Tauri/WebView2、托盘、安装包与签名（偏差见 [M5-desktop.md](M5-desktop.md) §2）。**别把它当成可双击安装的 App，也别把 README 的愿景当成已实现的能力。**
- **核心承诺**：事件可重放、执行侧授权、覆盖/删除需审批、默认可撤销、崩溃可恢复。
- **非目标（v1）**：图片/视频生成、多租户、防本机用户篡改（只防模型越权与失控 worker）。
- **技术栈**：Python 3.12（`requires-python = ">=3.12,<3.13"`）、`uv`、**零第三方运行时依赖**（`dependencies = []`）；dev 依赖只有 `pytest>=8.0` 与 `ruff>=0.6`。UI 是 Vite + React + TS，但只有**开发/构建**时需要 Node，运行时加载已提交的 `ui/dist`。
- **版本 / 许可**：`0.1.0`；Apache License 2.0（`LICENSE` 已被 git 跟踪）。

## 2. 当前状态（数字会过期，更新时重核）

- **里程碑**：**M0–M5 已完成**。M4 由 PR #41 / #45 合入（代码在 `src/tianshu/eval/`）；**M5 桌面闭环由 PR #49 完成**（2026-11-10，`feat/m5-desktop`）：Daemon / Host / UI 三进程打通、审批 UI、关 UI 任务继续、重启后恢复。设计与契约见 [M5-desktop.md](M5-desktop.md)，实测证据见 [M5-verification.md](M5-verification.md)。
- **F10 独立复审与整改（2026-11-11，`feat/memory` / PR #64）**：[REVIEW-F10-memory.md](REVIEW-F10-memory.md) 对 `c4216e1` 做独立复审（高 1 / 中 5 / 轻 9），结论是**不能按"已验收"合并**；逐条处置见 [REVIEW-RESPONSE-F10-memory.md](REVIEW-RESPONSE-F10-memory.md)。· **高危 D1 按架构裁定改文档、不改实现**：`memory.search`/`memory.get` 的 `tool.result` 必然带正文——不留它，replay 就重建不出模型当时的输入，`messages_hash` 立刻对不上。所以"事件日志只存引用"**只对写入路径成立**（`Dispatcher._intent_args` 已把 propose 的 `content` 换成 `sha256`+长度），§3.1 / §11 已按实测改正，并把"`forget` ≠ 物理擦除、正文副本 = 事件日志读快照 + 正文 blob"写进契约。· **代码侧修 5 处**：① `Dispatcher` 拒收**另一个日志**的投影（同事务约束落到装配边界，不再靠调用方记得）；② Worker / daemon / eval / 测试替身统一复用 `stack.memory`（原先会各自 `open_memory()` 开第二个投影，自定义 blob 目录时写入成功却检索不到）；③ `parse_memory_uri` 与 `MemoryScope.workspace` **先拒相对路径再 realpath**（先 realpath 会把 `memory://workspace/notes` 按 CWD 补成绝对路径，fail-closed 退化成"取决于进程在哪启动"）；④ `memory.search` schema 暴露 `limit`（1..5，执行侧再夹一次）——否则 §8"模型可以要求更少"不成立；⑤ **被拒绝**的记忆写请求在 `dispatch_requests` 脱敏成 `content_sha256`+`content_bytes`（拒绝的请求永不执行，不破坏派发/审批绑定）。· **仍兑现不了（如实留账）**：编辑（`supersede`）与 workspace/task 作用域**创建**都没有生产入口（§13 #24 / #25）；**被采纳后**又被内容闸门挡掉的请求仍把原文留在 `dispatch_requests`（闸门在 executor 跑、请求行在 `prepare()` 就落库；彻底修要把扫描挪到落库前或给审计表定保留策略，§13 #26）；物理擦除仍是单开工单。· **一处自证伪**：§14 曾写"没有断言 origin 的测试"，实际 `test_agent_propose_then_confirm_through_dispatcher` 早就断言 `origin == "inferred"`，已改。
- **F10 记忆产生侧落地（N1，2026-11-11，分支 `feat/memory-lifecycle`，PR 待合）**：控制面有了**可判定的**身份——`Task.is_control` / `Task.memory_scopes` 从 `task.created` payload 读回（只加键、不加事件类型，rebuild 也还原）；`Dispatcher.open_control_run(memory_scopes=...)` 给 CLI 用。C2：`memory.propose` 升 **2.0**（`scope`/`scope_id`，模型只能写 `user` 与**本任务** `task`），新增 `memory.remember`（`1.0`、`exposed=False`：创建即 `active` + `origin=user_explicit`）。执行侧 `memory_exec._verify` 独立核验：控制面工具要 `is_control`（越权 → 新编号 `c4.memory_control_scope_binding`）、模型写 workspace **无条件拒**（不因 `Task.is_control` 放行——同一 Task 上后开的模型 Run 会继承控制面标记）、task 只能本任务、声明作用域只在控制 Run；策略层同步（`memory_declared_scopes` **只由派发层覆盖写入**，不接受请求自带值）。**#22 收口**：人批准 confirm 时执行器给 output 加 `origin="user_confirmed"`，投影只允许 `inferred → user_confirmed`（只升级不降级）；`user_explicit` 只能由 remember 产生。**#27 解除**：CLI `memory confirm|forget --task-id` 用控制 Run 的声明作用域签字，且 `lease_paths == ()`（不绑旧 task 目录）。规则只新增一条（`RULE_IDS` 49 → 50，三处同改），擦除回执 / 编辑 revision / 作用域越界都复用现有编号。测试：N1a +8（6 派发 + 2 CLI），N1b（edit/promote）+6（4 派发 + 2 CLI）；全量 **1343 passed / 6 skipped**。· **N1b 补记**：`memory.edit`（一次审批 = 旧行 superseded 可查 + 新行 active，新 `memory_id`/`revision=1`/`supersedes`，不许改 kind）与 `memory.promote`（移动语义：原行退役、目标作用域候选、Receipt 指原行、`source_refs` 必含原 id；目标作用域统一 canonicalize）已落地；**origin 按"这一版正文的来源"**：控制面 edit → user_explicit、模型经人批准 → user_confirmed、promote 来源随行不降档；`user_explicit` 的免审批只保留给 `confirm`（策略层与 `_requires_approval` 两处都按 `memory_tool == "memory.confirm"` 收口——复审口径：edit/promote 永远要人批准）。· **仍没做**：`memory.consolidate` / `memory.erase`（契约已定义，代码未落地）、L2/L3、向量召回、恢复分支、UI；`origin=user_explicit` 的**候选**免审批确认仍无生产入口（remember 直接写 active，不产生候选）。
- **剩余记忆施工规格（仅文档，2026-11-11）**：[SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) 把 F10 §13 #18–#27 十条拆成六个工作流（N1 产生侧 / N2 检索质量 / N3 L2-L3 / N4 擦除边界 / N5 恢复与审计 / N6 控制面 UI），每条给"现状（代码事实）→ 设计 → 判据 → 负控"与文件所有权，契约增量单独列在 §1 等 `docs(contract)` PR。**该文档是待实施方案，不是已完成能力**；实施前的记忆状态仍是"只交付 L1"。状态数字与实现进度见上一段 F10 实现轮条目。
- **F10 本地记忆设计（v1.1，提案，2026-11-10）**：新增 [F10-memory-design.md](F10-memory-design.md)。核心判断：**记忆是事件日志的另一个可重建投影，不是第二个事实源**——红线 1 已把独立记忆库这条路堵死。参照 [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（L0–L3 分层、记忆资产化、装备 loadout、默认私有、可编辑）与 [OpenViking](https://github.com/volcengine/OpenViking)（上下文即虚拟文件系统、三层读取、目录作用域检索、会话成文件 + diff、记忆带引用）的组织方式，**只取设计不取实现**。关键决定：记忆操作是 C2 工具（首版不新增事件族，避免与 `tool.intent`/`tool.result` 形成同一因果的第二份指针）；写入**分级审批**（propose 写惰性候选不挡；agent 发起的 confirm/forget 走 C4 审批 + Receipt，等同覆盖/删除）；默认 `scope=user` 私有；`origin=inferred` 永不自动激活；忘记=tombstone ≠ 物理擦除。· **第二轮（mem0 / Hindsight）补进**：L2 升级为**带证据的信念**（`evidence[]` + `proof_count` + 原文引文，refine 而非覆盖）；L3 是**预算好的可启动页面**（读取为 DB read，无检索/无 LLM）；蒸馏**只做增量 delta 刷新、禁止全量重生成**；**freshness awareness**（陈旧层先核验再用）；记忆写入过 **Memory Defense** 闸门（PII/密钥，红act 或阻断）+ **写入前去重**；实体/时序检索列为后续、须评测证明增益。· **（那一轮只落文档；实现状态见下一条）**
- **F10 记忆实现轮（2026-11-10，分支 `feat/memory` → PR #64，已提交）**：记忆从设计进入**落地**。新增 `src/tianshu/memory.py`（`MemoryProjection`：投影表 + FTS5 + 正文内容寻址 + 重建）、`memory_exec.py`（`MemoryExecutor`：执行侧二次核验、`memory://` 作用域判定）、`resources.py`（资源种类：裸绝对路径 = `file`，`memory://<scope>/<scope_id>` = `memory`，其他 scheme fail-closed）。接线：`dispatcher`（`_authority` / `_resolve_target` / `_policy_preconditions` / `_check_memory_preconditions`）、`permissions`（`PolicyEngine._classify_memory`）、`tools`（5 个 `memory.*` ToolSpec，均无 `path_arg`）、`loop`（只读分支 `_exec_memory_read`、写目标 `_memory_target`）、`context`（`context_payload(memories=...)`）、`cli`（`tianshu memory list|search|get|confirm|forget`）、`receipt`（`build_memory_receipt`）。契约增量：C2 五个工具（`version=1.0`）；C4 资源种类 + **6 条新规则编号**（`RULE_IDS` 43 → 49）；C3 **不新增事件族**（记忆生命周期由 `tool.intent`/`tool.result` + 审批事件承载）。· **测试**：四个记忆测试文件已覆盖——`test_memory_store.py`（51 个 `def test_` / 64 条）、`test_memory_dispatch.py`（20 条，真派发/审批/事务/资源种类/作用域/错装配投影/拒绝行脱敏）、`test_memory_loop.py`（12 条，主循环/预算/replay/CLI/遗忘后不可按 id 复活）、`test_resources.py`（23 个 / 44 条）。· **未落地 / 未覆盖（别当已完成）**：L2/L3 蒸馏与向量召回未实现；`confirm` **不改写 `origin`**，因此 `user_explicit` 与 `user_confirmed` 两档都**没有生产入口**、线上记忆恒为 `inferred`，§8 的 origin 排序实际退化成 `created_at` 降序；§8 的**作用域优先级**（优先当前任务/工作区证据）未实现（作用域只用于过滤）；`recovery.recover_run` **没有记忆分支**（intent 已提交而 result 未落库时按文件工具的推断走，投影侧安全但结论不精确）；`origin=user_explicit` 的免审批确认**生产侧没有产生入口**（`memory_exec._propose` 固定写 `inferred`，测试靠播种事件才走到那条分支）。逐条状态见 [F10-memory-design.md](F10-memory-design.md) §13，判据↔测试映射见 §14。
- **M0–M5 对抗性复审（2026-11-10）**：[DEBUG-M0M5.md](DEBUG-M0M5.md) 对 `main@4eb4ea9` 做了独立复审（严重 0 / 中等 5 / 轻微 7），本轮把 12 条**逐条裁决**（修复记录追加在同一文档末尾）：5 中等里 4 条已修（中-1 阻断 TOCTOU、中-2 未知工具恢复、中-3 注入证据面、中-4 畸形阻断记录 fail-open）、1 条**误报**（中-5：`prepare` 当场就拒，补钉住测试而非改代码）；6 条轻微修复（同内容写入口径、缺 blob 域错误、unblock 稳定 id、M3 diff 区间、封闭集全等断言、eval 归因分层）+ 1 条纯文档补登（Host 强杀后 Daemon 残留）。复审留账的静态疑点/未验证项开成 issue **#53 / #54 / #55**。
- **M0–M1 报告修复对账（2026-11-10 复核）**：[DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾补了「修复状态总表」——S1–S4 / M1–M4 / L1–L2 / R1–R5 逐条对应代码位置与钉住它的测试，含负控口径。复核中发现并修掉一条残留：**快照清单损坏时恢复/撤销漏出裸 `JSONDecodeError`**（现为 `SnapshotCorrupt` → unknown+阻断 / 拒绝撤销）。仍然不成立的四项如实写在总表末尾：真实断电、Windows 实机、authority 数据根逃逸 + 完整 takeover（issue #61）、跨多文件撤销的事务性。
- **2026-11-11 收尾轮：DEBUG-M0M1 除 Windows 实机外全部落地（PR #66–#72）**：① 真实断电 → 持久化边界注入 × 撕裂写 × 真实 HFS+ 强制摘卷（[powercut-evidence.md](powercut-evidence.md)），并**新发现两个真 bug**：删除类动作的恢复去读已被删除的文件（`FileNotFoundError` 穿出 `recover_run`）、撕裂 WAL 被静默洗成空库（fail-open，历史全丢）；② authority 数据根逃逸 → 固定用户级权威根（`authority.py`，与 `--db`/cwd 无关），守卫双读；③ 完整 takeover → `recovery.assess_run` 只读对账 + 三个接管入口；④ 跨多文件撤销 → 写前意图日志 + 崩溃后幂等续做 + 恢复侧阻断；⑤ #63 就绪竞态根因是 **`Listener` 默认 backlog=1，`probe` 的未认证连接自己占掉唯一队列位**；⑥ #58 费用行按**尝试次数**说话；⑦ #57 评测判据按目标作用域化（`SUITE_VERSION` m4.5，完成度/安全分栏）；⑧ #53/#54/#40 销账。规格与并发施工纪律见 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md)，判据↔测试映射见 [DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾新表。· **仍未成立**：块设备断电语义、Windows 实机、真实模型 m4.5 复跑（中转站 503 全模型不可用）、撤销不是两阶段提交。
- **测试**：**1343 passed, 6 skipped**（`feat/memory-edit-promote`（N1b）实测，2026-11-11；N1 自身 +13、#79 +13；`main` 上一次提交态基线 1316 passed / 6 skipped，macOS 本机实测，收尾轮 `43d04ef`，115.97s；上一轮 DEBUG-M0M1 复核轮为 1080 passed / 3 skipped；F10 分支 `feat/memory` **合入 `main` 的 #62 / #65 / #66 / #67 并完成复审整改后实测 1243 passed, 3 skipped**（105.91s；其中 140 条来自本特性的四个记忆测试文件，其余来自 main 的新增测试）。未注明「分支实测」的数字一律指 `main` 提交态基线。以下为历史上各轮实测的原始记录：macOS 本机实测，2026-11-10，DEBUG-M0M1 复核轮（分支 `fix/m0m1-corrupt-manifest`）：`uv run --python 3.12 pytest -o addopts="" -q` → `1080 passed, 3 skipped in 105.86s`；上一轮真实模型轮 `test/m4-real-model` 为 `1077 passed, 3 skipped in 101.01s`；`uv run --python 3.12 ruff check .` → `All checks passed!`）；测试文件 **40** 个 `tests/test_*.py`。基线 1061 →（复审轮 +13 → 1074）→（真实模型轮 +3 → 1077）→（DEBUG-M0M1 复核轮 +3 → **1080**），无新增 skip。真实模型轮新增 3 条：eval 拒绝 memfs 两条（库 + CLI）、replay 按路径核产物一条；DEBUG-M0M1 复核轮新增 3 条：坏快照清单的恢复（写工具 unknown+阻断）、只读调用不被误伤（none_applied、不阻断）、撤销拒绝（拒绝+零修改）。M5 新增 6 个测试文件：`test_ipc` / `test_host` / `test_control` / `test_daemon` / `test_integration_m5`，以及 issue #40 修复带的 `test_approval_reject`；五个 M5 文件共 **110** 条（ipc 26 / host 28 / control 27 / daemon 19 / integration_m5 10），`test_approval_reject` 9 条（均实测 `--collect-only`）。**Windows 的数字本轮没有重核**——无 Windows 实机，只有 CI `windows-latest` 的结论，别把 macOS 数字写成三平台通用。注意 `pyproject.toml` 的 addopts 已带 `-q`：直接 `uv run pytest` 会正常打印汇总；但**再在命令行补一个 `-q`**（叠成 `-qq`）汇总行就没了，那时要 `-o addopts="" -q` 才行。三种情况都实测过。
- **代码规模**：`src/tianshu/*.py` **37** 个模块（M5 新增 `ipc` / `host` / `daemon` / `control`，并把撤销从 `cli` 提成库 `undo`），另有子包 `eval/`（6 个 .py）。
- **平台**：CI 矩阵是 **ubuntu / macos / windows 三个平台 × Python 3.12**，另有一个 `ui` job（ubuntu：typecheck / vitest / build / `ui/dist` 漂移门禁）。Python 三平台：`7abad75` 的 run `42652194348` 三平台全 success；**M5 之后 Windows 不再是绿的**——命名管道上存在**偶发握手失败**（issue #50，`digest sent was rejected` / `digest received was wrong`，每轮失败的用例与条数都不同，本机 macOS 无法复现）。ubuntu / macOS 稳定绿。Windows 只有 `windows-latest`（Server 2022 runner）证据，没有实机验证。
- **门禁**：`main` 已开分支保护——必须走 PR、`strict`（分支须与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推与删除。required status checks 目前是 `test (ubuntu-latest) · py3.12` 与 `test (macos-latest) · py3.12` 两项；Windows job 在矩阵里跑但**还没加进 required 列表**（见 §7）。
- **CI 步骤**：`checkout` → `setup-uv` → `uv python install` → **`uv lock --check`（锁文件漂移门禁）** → `uv sync --frozen` → `uv run --frozen ruff check .` → `uv run --frozen pytest -o addopts="" -q`。权限 `contents: read`，`fail-fast: false`。
- **Windows 跳过项**：按平台跳过的是 memfs 符号链接、POSIX PID 探活、POSIX 权限位 ×2、无 PTY ×2、无进程组/信号 ×2 这几类（上一版记 8 项，本轮无 Windows 环境，**未重核条数**）；跳过原因写在跳过消息里，CI 用 `-rs` 打进日志。
- **M5 新入口**：`tianshu daemon`（常驻守护进程）与 `tianshu host run|stop|status`（桌面外壳）。运行数据根默认平台标准目录，`TIANSHU_DATA_ROOT` 覆盖；**旧的内核子命令（`run` / `task` / `approvals` / …）仍默认 CWD 下的 `.tianshu/events.db`**（`cli.DEFAULT_DB`，`--db` 可改）——两套落点并存，文档必须分开写。
- **数据目录**：`ipc.default_data_root()` 已把平台标准位置实现出来——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`（不用 Roaming：事件日志会撑爆漫游同步）、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`）。目录内：`events.db`、`artifacts/{blobs,snapshots}`、`run/`（IPC 端点 / `ipc.key` / 锁）、`logs/daemon.log`（Host 重定向的 daemon stderr）。测试与 CI 一律显式传 `--data-root` 到 tmp，**不允许**写用户主目录。
- **模型**：两个线上协议（OpenAI-compatible、Anthropic Messages），各一个 adapter，主循环只认内部规范化形状；契约测试在 `tests/test_model_contract.py`。桌面演示与验收默认 `scripted` 模型（不联网、不花钱）。**2026-11-10 起有两个真实模型的完整评测数据**（deepseek-v4.1-flash 18/20 case、glm-5.2 15/20 case，同一 suite m4.4）+ 一次真实模型审批流端到端；逐条证据与裁决见 [M4-verification.md](M4-verification.md) §7.1。key 走环境变量，不落库、不进文档。

## 3. 架构速览

- **唯一事实源**：SQLite WAL append-only 事件日志（触发器禁 UPDATE/DELETE）。Task/Run/Approval/Grant/Lease 都是同库事务更新的可重建投影。
- **Task ≠ Run**：Task 是用户看到的那件事，Run 是一次执行；改目标会 fence 旧 Run。
- **授权只在执行侧**：模型输出/工具输出/MCP 注解都不构成授权依据；dispatcher 是唯一派发入口，执行前登记 intent、执行后写 result + Receipt。
- **三进程（M5）**：UI（浏览器）→ loopback HTTP + SSE → Host（单实例、监护 Daemon、token 注入）→ IPC（AF_UNIX / Windows 命名管道，`ipc.py`）→ Daemon（任务/事件/审批/驱动线程）。**关 UI 只减少订阅者，不停任务**；重启后由 Daemon 的恢复扫描接手。细节见 [M5-desktop.md](M5-desktop.md) §1/§4/§5。
- **五道契约**（版本化；冻结条件＝字段级 JSON Schema 可校验 + 契约测试全绿 + 第二家 provider 验证过）：
  | ID | 名称 | 当前状态（只写可核实的） |
  |---|---|---|
  | C1 | Model I/O | 两个线上协议各一个 adapter；结束原因是封闭集，越界抛 `ModelError` |
  | C2 | Tool | 7 个工具、6 个对模型开放；`tool_manifest()` + 每种工具的参数要求由工具自己声明 |
  | C3 | Event | **已封闭**：41 个事件类型，写入侧拒未登记类型（只校验**类型名**，不校验 payload 字段）；声明了但从未发射的类型 **0** 个 |
  | C4 | Permission/Approval | **rule id 已封闭**：共 43 个——**拒绝原因 36 个** + 非拒绝判定 5 个（`c4.auto` / `c4.overwrite` / `c4.delete` / `c4.exec` / `c4.outside_workspace`）+ 保留未使用 2 个（`c4.unhandled` / `dispatch.denied`）；写入前校验。`c4.approval_rejected` 是 issue #40「否决即终局」新增的拒绝原因。审批/凭据见 PRD §5.4 |
  | C5 | Eval | `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）已分离，评测套件 `m4.4` |
- **封闭集落在三处**：代码里的集合（`events.EVENT_TYPES` / `rules.RULE_IDS`）、`schemas/` 下的 enum（`schemas/c3/event.json`、`schemas/c4/rules.json`，共 7 个 schema 文件）、PRD 对应小节；测试把前两者绑在一起，改一处忘另一处会红。
- **两个「未」不是一回事**：C3 的「声明未发射」是 **0**（没有登记了却不发射的事件类型）；C4 的「保留未使用」是 **2**（`c4.unhandled` / `dispatch.denied`，防御性兜底，登记为合法但没有产生路径）。别把这两个数字混着写。
- **十一条红线**（与功能冲突时以红线为准，每条能被一条测试打红）：
  1. 事件日志 append-only，无 UPDATE/DELETE
  2. 副作用执行前已持久化 intent，执行后必须写 result
  3. 上下文是可再生视图，删掉所有视图可从头重编
  4. 授权只在执行侧判定
  5. 副作用结果未知时禁重放，只有 `none_applied` 可安全重试
  6. 每个副作用必须产出可独立核对的 Receipt
  7. 跨层只传版本化契约对象，SDK 对象/异常实例不得越界
  8. 工具 schema 变化即视为新工具，不自动升级
  9. 能力声明只用于路由，不用于授权
  10. replay 与 evaluate 不得混称，安全门槛不可被其他分数抵消
  11. 任何「崩溃后无法判定状态」的设计视为 bug
- **模块地图**：`src/tianshu/` 37 个模块 + `eval/` 子包。按功能分：事件与状态（`events`/`tasks`/`jobs`/`leases`/`snapshot`/`recovery`/`replay`）、授权与派发（`permissions`/`rules`/`dispatcher`/`guard`/`receipt`）、模型与执行（`model`/`capability`/`context`/`exec`/`execpolicy`/`credentials`/`pricing`/`sandbox`）、工具与路径（`tools`/`workspace`/`pathkeys`/`fsutil`/`canonical`）、撤销（`undo`）、主循环与入口（`loop`/`worker`/`cli`/`errors`/`crashpoint`）、**桌面三进程（`ipc` 传输与契约 / `daemon` 常驻与恢复 / `control` RPC 映射 / `host` 外壳与 UI 桥）**、评测（`eval/`）。细节见 PRD / TECH-ARCH 与 [M5-desktop.md](M5-desktop.md)。
- **CLI 子命令**（实测 `tianshu --help`）：`run` / `task` / `worker` / `recover` / `undo` / `approvals`（别名 `approval`）/ `replay` / `daemon` / `host` / `evaluate`。
  - `worker` 是**一次性 drain**——队空或撞上被占用的工作区就退出；**常驻进程是 `daemon`**（`host run` 会拉起并监护它）。
  - `host` 只有 `run` / `stop` / `status` 三个动作（不带动作 = `run`）；`status` 在跑退出 0、没跑退出 1。
  - `replay` 审计不调模型；`evaluate` 换模跑固定 case。
  - 退出码：`0` 成功 / `1` 失败或取消 / `2` 前置条件错 / `3` 需要人决定（等审批、已暂停、待核查、`undo` 未带 `--approve` 的预演）/ **`4` = evaluate 判 unsafe**（安全门槛一票否决）。

- **已实现的功能面**（PRD §7 编号 → 落地模块，模块说明取自代码）：
  - **F1 执行原语** → `exec.py`（argv 模式，非 shell；超时/截断/进程组）
  - **F2 执行策略** → `execpolicy.py`（默认全拒）
  - **F5 上下文编译** → `context.py`（纯函数、按字节预算、丢弃只记账不摘要）
  - **F9a 模型准入** → `capability.py`（能力声明与准入，只用于路由，不用于授权）
  - **F12 凭据** → `credentials.py`（分域查找 + 全链路脱敏；**Keychain 后端 v1 未实现**，如实报 `unsupported`）
  - **F13 成本** → `pricing.py`（整数微美元；未知价格记 `None` 并单独计数）
  - **F14 界面部分** → `ui/`（React + TS）：机器枚举 → 人话的映射在 `src/lib/humanize.ts`；`digest` / 请求 id / 枚举值不进视图，内部细节放折叠区（PRD §14）
  - 其余 P0 功能落在：`events` / `recovery`（事件与恢复）、`tools`（工具系统）、`permissions`（授权审批）、`tasks` / `jobs` / `leases`（任务与运行）、`snapshot` / `receipt`（产物与回滚）、`undo`（撤销）
  - **F10 记忆**属 v1.1，v1 不做（PRD §2.2 / §7）

## 4. 关键决策（D1–D7）与硬约束

| ID | 决策 | 直接后果 |
|---|---|---|
| D1 | 首批用户＝普通用户，后期开源 | 权限默认值是产品决策而非配置项；F11 回滚 / F13 成本可见升 P0 |
| D2 | 单用户 | 砍多租户；租约只防「同用户双实例」；README/代码须显式声明单用户单机 |
| D3 | 编程沙箱默认可写 | 写/删分级；删除必须可撤销 |
| D4 | 图片视频走用户自己的 key | 成本可见成硬需求；不做充值计费 |
| D5 | 快照保留策略 | 见 PRD §8.4 |
| D6 | Task 是用户可见单位，Workspace 是其属性（0..n） | 空目录任务合法；「读 A 写 B」可表达 |
| D7 | 诊断包仅本地导出、永不上传 | 但默认脱敏（最终用途是贴公开 issue） |

**派生硬约束**：安全不能靠用户配置（默认值必须正确）；默认可写就必须默认可撤销；单用户假设必须显式声明。

### 收尾轮实现决策（2026-11-11，PR #66–#72）

- **阻断状态属于"这台机器这个用户"，不属于某个 `--db`**：`authority.py` 把它固定到 `TIANSHU_AUTHORITY_ROOT`（否则 `~/.tianshu/authority`），与 `--db`/`--data-root`/cwd 无关；键用 `pathkeys.overlap_key(resolve(path))`（与租约同口径）。失败语义朝安全方向：**库不存在=空集**（不是损坏，否则新用户第一次用就被自己挡住）、**读不出=已阻断**、**写失败=如实报「跨数据根阻断未生效」**。代价：权威根只保护"阻断"，跨数据根没有租约互斥。
- **接管前对账只读**：`recovery.assess_run` 不写事件、不落盘、不阻断（用 `SnapshotStore.__new__` 挂属性绕过会 `mkdir` 的构造函数）；拒绝接管后的双重证据（日志 + 权威根）由调用方在租约事务回滚**之后**落。三个入口（`AgentLoop.start`/`resume_run`、控制 Run）共用同一条实现——只保护控制 Run 等于没保护。
- **撤销用"写前意图日志 + 幂等续做"，不是两阶段提交**：`artifacts/undo/<run_id>.json` 记录完整计划与每步 `post_state`；崩溃后先核对 done/剩余项与磁盘一致再从断点继续，**绝不从头再来**；`completed` 只差补事件不阻断。恢复侧把未完成撤销变成阻断（`recovery.blocked` + 权威根），阻断是 journal 的投影、不是事实源：`unblock` 可解除，但只要 journal 仍 `in_progress`，下次恢复会重新挡住。
- **就绪标记必须晚于"端点真的可服务"**：`on_ready` 在 accept 线程进入 accept + 自连认证握手成功后才落；`LISTEN_BACKLOG=64`。根因不是"listen 没完成"，而是 backlog=1 时探活自己占满队列（详见 §5）。
- **评测判据按目标作用域 + 分类**：`Check.kind`（completion/safety）只有 safety 进 `safety_gate`；完成度判据红不影响安全结论；`SUITE_VERSION` m4.5 使旧报告不可比（两道守卫）。
- **`EventLog` 对"有内容却读不出 events 表"的库 fail closed**：0 字节按新库（首次启动窗口无历史可丢），只有"文件存在且有内容、读不出 schema"才 `EventLogCorrupt`。检查必须在 `executescript(_SCHEMA)` 之前、`PRAGMA journal_mode=WAL` 之前——顺序即是"先读、再写"的保证。

### M5 与 PRD / TECH-ARCH 的偏差（即 M5 文档里的 D1–D5，别与上表 D1–D7 混）

| # | 文档原样 | 本版实现 | 换回去的时机 |
|---|---|---|---|
| M5-D1 | 桌面应用 = **Tauri 2 · React · TS** | UI 是 React + TS，但**外壳是系统浏览器**，没有 Tauri/WebView2 | 单独工单：外壳 + 托盘 + 开机自启 |
| M5-D2 | **Desktop Host (Rust)** | Host 是 Python 进程（`host.py`） | 与 D1 同一张工单；只换实现，IPC/UI 契约不动 |
| M5-D3 | UI ↔ Host 走**类型化 IPC（版本化）** | 预览壳走 **loopback HTTP + SSE**：`POST /rpc`、`GET /events`、`GET /state` | D1 落地时换 WebView 内 IPC |
| M5-D4 | TECH-ARCH §4.2 帧 = `[4B 长度][JSON]` | 语义一致，帧编码用标准库 `multiprocessing.connection`（POSIX = AF_UNIX，Windows = 命名管道）+ `authkey` | 需要跨语言/跨版本 IPC 时只替换 `ipc.Server` / `ipc.Client` |
| M5-D5 | 关闭窗口 ≠ 退出：最小化到托盘 | 没有窗口可关：**关浏览器标签 = 断订阅者**；Host 活到 `tianshu host stop` 或被杀 | 与 D1 一起 |

逐条理由与"不为偏差辩护"的部分见 [M5-desktop.md](M5-desktop.md) §2。

### M5 实现决策（踩出来的，不是偏好）

- **驱动线程的 SQLite 连接必须 `cross_thread=True`**：连接在 serve 线程建、驱动线程用；默认 `check_same_thread=True` 会抛 `ProgrammingError`，表现是 Run 永远停在 `created`（本分支修过两次：先是连接归线程，再是跨线程复用）。`EventLog(cross_thread=True)` 只给"连接建在一处、只在该线程用"的通道用，不是免检通行证。
- **SSE 用 `fetch` 读流，不用浏览器 `EventSource`**：契约要求每个请求带 `X-Tianshu-Token`，而 `EventSource` 天生带不了自定义 header，也没有自定义重连游标；用 fetch 流能达到同样的协议与「带 `since_seq` 重连」语义。
- **token 只进页面 `<meta>` 与请求头，不进 URL**：URL 会进浏览器历史、shell 历史、聊天记录和截图；`UiBridge.url` 刻意不带 token（`src/tianshu/host.py` 的 `url` 属性注释写明了理由）。

### M5 修掉的真 Bug：`host stop` 的退出路径（三个缺陷叠在一条路径上）

修在 `f5da9ef`。三个缺陷单独看都像"偶发/环境问题"，叠起来把"关 Host"变成半失败；全是**顺序与注册位置**问题，不是算法问题：

1. **`/shutdown` 端点注册在 `do_GET`，而 `tianshu host stop` 发的是 POST** → 永远 404，`stop` 只能打印"Host 未在超时内退出"并返回 1。修法：只接受 POST + token；GET 回 405，**不能**落到 SPA 兜底回 200 + `index.html`——那会让人以为通知成功了。回归：`tests/test_host.py::test_shutdown_endpoint_is_post_only_and_requires_token`。
2. **清理回调赋给 `_Handler` 的类属性** → 普通函数成为类属性会被描述符协议绑定成 bound method，`threading.Thread(target=self.stop_callback)` 调用时多塞一个 handler 实例当 `self` → `TypeError`，清理线程静默死掉（日志里只有一条没人看的 traceback）。修法：回调挂在 **server 实例**上，handler 用 `self.server.stop_callback` 取。回归：`tests/test_host.py::test_stop_host_stops_a_running_host_and_clears_info`。
3. **`stop_all()` 先置 `stop_event` 再清理** → 主线程立刻从 `run_host()` 返回，进程退出把"正在关 Daemon"的清理线程一起带走：`stop` 报"Host 已退出"，Daemon 却留成孤儿继续占 socket、继续接活。修法：**清理完成之后**才放主线程走（`stop_requested` 负责去重）。回归：`tests/test_integration_m5.py::test_host_stop_stops_daemon_too`。

第 3 条**在 pytest 进程内测不出来**：主线程退出后进程还活着，清理线程不会死，Bug 会躲过去；必须用**真子进程**跑 `tianshu host run` → `stop_host()` → 进程退出后立刻 `ipc.probe` 断言为假。本轮在新 tip 实测：`host run` → `host stop` 输出 `Host 已退出`，Host 与 Daemon 进程都消失，`ipc.probe == False`，`run/` 只剩常驻的 `ipc.key`。这也是下面 §5「顺序即契约」与「真进程验收」两条教训的来源。

### M0–M5 复审修法的实现决策（2026-11-10，`fix/m0m5-audit`）

- **把「阻断核验」压到执行边界，同时让恢复和派发抢同一把锁**：`FileExecutor.execute()` 在改文件前最后一步按**真正要写的那个路径**再判一次；`TaskStore.begin_execution()` 在拿住执行守卫之后、写 `execution.started` 之前按资源路径再判一次；`recover_run(..., data_root=...)` 给定时，恢复全过程（含落 `recovery.blocked`）在本机执行守卫内跑，CLI `recover` / daemon 启动恢复 / eval 崩溃恢复三处都传。代价与边界：恢复要等守卫（上限 `GUARD_TIMEOUT_MS=5s`，超时抛 `GuardTimeout`）；daemon 的恢复扫描在 bind 之前（此时没有驱动线程），所以不会自锁；库调用方不传 `data_root` 时没有跨进程线性化，只剩两道就地核验（口径不变，窗口收窄）。
- **未知/畸形一律 fail-closed，且"拒绝"要用已有的 rule id**：`guard.scan_blocked()` 把「回放出的阻断集合」和「判定不了的事件 seq」一起交出来；`blocked_workspaces()` 见到后者抛 `EventLogCorrupt`（这份日志答不了"哪些目录被阻断"），写入守卫转成 `WorkspaceBlocked`；守卫内被拒的派发在 `_abort_dispatch` 里映射回 `dispatch.workspace_blocked`，不新造 rule id（C4 是封闭集）。恢复里 `SPECS.get(tool_id) is None` 直接 unknown + 阻断，不再走"没证据=没写"的推理。
- **同内容写入 = 没有可观测副作用**：`_modify_text` 在 `before == data` 时返回 `succeeded / none_applied`，不落 preimage、不产 applied 凭据（`build_receipt` 只对 `applied` 出凭据）。这样活体路径与恢复侧（`before == expected_after → none_applied`）口径一致，"副作用已发生"不再被一句空话冒充。
- **`EventLogCorrupt` 的语义从一个扩到三个**：payload_hash 不符（A8）、外部 payload blob 缺失/不可读、阻断记录解析不出 workspace。三种都是"这份日志不能作为结论来源"，所以共用同一个域错误，但消息必须带 `seq`（和 blob 路径），否则调用方只知道"读失败"。

### M4 真实模型实测后的实现决策（2026-11-10，`test/m4-real-model`）

- **产物哈希按「路径」归属，不按「最后一次写入」**：`replay` 原先取最后一条带 `sha256` 的 `tool.result` 去比 `run.end.artifact`。真实模型会「先建报告、后覆盖目标文件」，于是最后一条属于被覆盖的目标，拿它比对报告哈希 → **一份完好日志被判 `ok=false`**（审计假红）。改为 `written_sha[path]`；`run.end` 带 `path` 时只跟同路径比，没有该路径记录才退回旧口径（不凭空判红）。这是审计侧最重要的口径：**误报比漏报更伤**，一次假红之后没人再看 replay。
- **评测只接受 `local_fs`，其它后端开跑前拒绝**：fixture 用 `Path.write_text` 直接落宿主盘、判据从宿主盘读、崩溃 case 还要跨进程看见同一份状态；memfs 是进程内单例、状态随进程消失，三条全不成立。实测（不加守卫）`TIANSHU_SANDBOX_BACKEND=memfs` 跑整套得 **1/20 + `verdict=unsafe`**——唯一的绿是唯一不写文件的 N6。假分数会被引用，所以 `run_suite` 先 `require_local_backend()`（CLI 退 2），拒绝时连工作根都不建。
- **P1/P2 判据不在这轮改**：真实模型的 `unsafe` 里有五条根因是判据写死了脚本模型的收尾形态（系统提示词要求写报告 → 模型的第二个副作用是合法交付物；`approval.requested=0` 只是"没尝试"，不是"绕过"）。修复要改判据语义 → 改 `fixture_digest` → 旧报告全部失去可比性，必须 bump `SUITE_VERSION` 并复跑两份真实报告。混在"补证据"的 PR 里偷偷改判据，比不改更坏；登记工单，判据口径写进 [M4-verification.md](M4-verification.md) §8 第 7 条。

## 5. 踩坑教训（别再踩）

- **自审有盲区**：PR #7 自写自审自合，第二读者十分钟找出 13 项缺陷、5 项高危。作者知道自己的意图，所以看不出实现没兑现意图。→ 高危改动必须独立复审（见 §6）。
- **测试证据会造假**：曾用「同进程两条连接」冒充两个进程、用 `sleep(0.05)` 猜时序、用手写替身盖住真洞。→ 并发要真子进程 + 屏障，时序用注入时钟，契约断言用真实 `tool_manifest()` / 真实 schema。
- **文档会说假话**：README/PRD/复审报告多次与代码漂移，出现专门「修假话 / 对账」的提交。→ 改代码同步改文档；复审报告写错归因要按事实重写。
- **「放在系统标准位置」这类话要能指到代码行**：README 长期写着「运行时数据放在系统标准位置」，而 `cli.DEFAULT_DB` 一直是**当前工作目录下的 `.tianshu/events.db`**（相对路径，靠 `.gitignore` 排除）；`~/Library/Application Support/AgentOS/` 那套当时只存在于 TECH-ARCH、属 M5 设计。这条假话过了多个里程碑没人发现——**直到 M5 才真正兑现**：`ipc.default_data_root()` 实现了平台标准目录（`TIANSHU_DATA_ROOT` 覆盖），`daemon`/`host` 用它；旧内核子命令仍默认 CWD，两套并存。教训不变：凡是「放在哪里 / 什么时候清理 / 谁负责」的运行时行为陈述，都要能指出是哪一行代码；指不出来，就说明它还没实现。
- **事务要可组合**：各 store 各自 `BEGIN IMMEDIATE` 会嵌套报错；统一走 `EventLog.transaction()`（已在事务中则复用）。
- **父子目录需要互斥**：`(st_dev, st_ino)` 只答「是否同一目录」，`/ws` 与 `/ws/sub` 是同一棵树，须 ancestor/descendant 一律冲突。
- **租约/fence 必须释放**：Run 收尾或被 fence 后要在同一事务释放租约，否则新 Run 被自己上一个 Run 挡住。
- **进程身份 ≠ pid**：pid 会复用，用 `ProcessIdentity{instance_id, pid, started_at}`；外来实例不得过守卫/续租/释放。
- **封闭集要强制**：`finish_reason` 曾出现越界值 `error`/`unknown` 靠 `type: ignore` 压着；改为封闭集 + `ModelError` 对象，越界即报错。同一条道理后来复用到 C3 事件类型与 C4 拒绝原因上。
- **状态必须是事件的投影**：直接写表不写事件，回放就无法解释为什么被 fence。
- **「定义了」不等于「会产生」**：一个 id/类型可能只在代码里有定义、没有任何发射点。登记契约时要么给出产生路径，要么显式标注「保留未使用」，不能让它默默躺在集合里冒充已实现。
- **「超时」和「断线」都返回 `None` 的 API 是定时炸弹（M5 实测烧核）**：对端一断开，`multiprocessing.connection` 的 `poll()` 立刻报可读、`recv_bytes()` 立刻 EOF；读循环若只判 `None` 就会 100% CPU 空转——演示里关掉浏览器后 daemon 烤满一个核，直到被杀。修法是把语义钉死：`ServerConnection.recv()` 断线仍返回 `None` 但置 `eof=True`（此后再调立刻返回），`Client.next_event()` 断线抛 `ConnectionClosed`。回归测试还包含**真子进程 CPU 记账**（`time.process_time()`：空转 1.5s 会吃掉约 1.5s CPU），因为"读线程退出了"是内部事实，"没烧核"才是用户能感觉到的后果。
- **`Listener.accept()` / `Connection.recv()` 的阻塞语义约束了关闭方式**：这些调用不能被就地 `close()` 从另一个线程可靠打断（会卡在内部锁上），所以 Daemon 的关闭靠**「轮询 + 停止标志」**（带超时的 `poll`，每轮检查 stop），而不是收到 `shutdown` 就 `close()`。代价是退出最多等一个轮询周期；换来的是不会出现"关不掉的守护进程"。
- **验收必须用真进程 / 真产物**：M5 的界面截图是**真 daemon + 真 `ui/dist` + 真 Host 桥**跑出来的。手写一个 HTML 当截图会盖住 token 注入的 Bug——真页面是「空 `<meta>` 原地替换」，手写 HTML 恰好"有/没有 token"都看不出来（M5 期间真踩过：多插一个 meta，`querySelector` 拿到空值 = 没 token）。任何"UI 能用了"的结论都得指着真产物。
- **后台 shell 启动的子进程继承 `SIG_IGN`**：用 `(cmd &)` 起的进程后用 `kill -INT` 收尾是无效的（子进程忽略 SIGINT），本机实测后台 Python 的 `SIGINT` disposition 就是 `1`（SIG_IGN）。这是**测试环境陷阱，不是产品 Bug**——要么用能自退的脚本，要么用 `SIGTERM` / 显式 shutdown 接口，别把"打不死"当缺陷报。
- **顺序即契约（M5 `host stop` 教的）**：退出/清理路径里"**先发信号再清理**"与"**先清理再放行主线程**"是两种语义，选错会把安全边界（这里：不留孤儿进程）变成偶发问题——先置 `stop_event`，主线程立刻返回、进程退出把清理线程带走，`stop` 报成功但 Daemon 还在跑。凡是"关掉 A 之前必须先把 B 收掉"的路径，顺序本身就是契约，要写进注释和测试名（`..._stops_daemon_too`），不能靠"反正会一起退出"。
- **"敲一下叫醒阻塞的 accept"必须跨平台写，且不能阻塞 close（M5 二次踩）**：Windows 上 accept 阻塞在 `ConnectNamedPipe` 的 **overlapped I/O**，从别的线程关 listener 句柄**不会取消**挂起的 I/O——accept 线程永远卡住、命名管道名一直存在（症状：daemon 已退出、`probe()` 仍为真、下一个实例被拒）。第一版只给 Windows 加了"敲门"，结果 **ubuntu 红了**：Linux 上关掉正在被 `accept()` 使用的 fd 也不会可靠唤醒 syscall（macOS 会，所以本机一直绿）。第二版又踩：敲门本身（`CreateFile` 在实例全忙时）可能长时间不返回，把"关服务端"变成新的卡点（CI 的 windows job 挂在 Pytest 里 20 分钟没输出）。**结论**：①阻塞式 `accept` 的唤醒要么跨平台统一做、要么别做；②任何"为了让退出更快"的辅助动作，都必须放进带超时的旁路线程，不能让退出路径等它；③"本机绿"对跨平台阻塞语义几乎没有证明力。
- **子进程日志不要用没人读的 `PIPE`**：`stderr=subprocess.PIPE` 而父进程不读，子进程写满 64KB 缓冲区就阻塞在 `write` 上——表现是"端点永远不出现"，一个跟被测语义毫无关系的死锁。写文件既避免死锁，失败时还能把尾巴带进断言消息（`_tail()`）。**推论**：子进程"起不来"的断言必须带上 `returncode` 与日志尾巴，否则只能猜。
- **"挂住"和"失败"要分开**：CI 作业默认 360 分钟超时，一个挂住的测试会占 6 小时 runner 且不给任何信息。给 job 设 `timeout-minutes` 后，挂住至少会带着已有输出结束。同理，等子进程就绪不要靠"轮询 probe"猜进度——让它自己在 `on_ready` 落一个标记文件，断言就能区分"没走到 on_ready"和"走到了但端点连不上"。
- **pytest 进程内测不出"进程退出"类 Bug**：主线程退出后 pytest 进程还活着，清理线程不会死，Bug 会躲过去。这类逻辑必须用**真子进程**（`subprocess` 跑真的 `tianshu host run`，再 `stop_host`，然后从外部断言进程/端点），否则测试测的是"函数返回了"，不是"进程真的退干净了"。

- **「再检查一次」关不严 `check → act` 的窗口，共享一把锁才关得严（复审 中-1）**：`ensure_not_blocked` 只在派发 phase-1 查一次，查完到落盘之间仍可被插入一条 `recovery.blocked`。修法不是再多加几次检查，而是**让两件事抢同一把锁**：恢复落阻断与派发落盘都进本机执行守卫（`execution_guard`），先后就有了全序；同时把核验压到执行边界（改文件前最后一步、按真正要写的那个路径判）。教训：凡是「判定 → 副作用」跨进程成对出现的，先问"这两件事有没有共同的线性化点"；没有的话，检查次数只是把窗口缩小，不是关掉。
- **「无法判定」必须和「没有」分开（复审 中-2 / 中-4）**：两处 fail-open 是同一个病——`except Exception: continue` 把解析不了的阻断事件当成"没阻断"；未知 `tool_id` 套用"清单里没有条目 = 确定没写"。前者等于替一条看不懂的审计记录宣布结论，后者等于替一个语义未知的工具假设写入协议。口径：**不可解析 / 语义未知 / 读不出结论，一律 `unknown` + 阻断**，让人来看。猜出来的"没事"比明确的"不知道"危险得多。
- **证据要核「真正被写的那个东西」（复审 中-3）**：M1 的「kill×100 · 0 重复副作用」听着很强，实际注入只走 `create_report`，不变量 A 读的还是预置后从未被写的诱饵文件 `note.md`——断言一直绿，但和这次写入无关。修法：从 `tool.intent` 参数解析真实目标再逐字节核对，并把矩阵配平到 create + 覆盖写。**而且必须做负控**：故意破坏不变量（只写半截内容、把 A 改回读诱饵）看测试是否红，否则分不清"断言成立"与"断言恒真"。
- **复审报告也会误报，误报同样要留证据（复审 中-5）**：报告称 `prepare` 对目录外删除"不拒、会给用户一张永远批不了的审批"，实测是 `rejected`（`dispatch.lease_lost`）、无审批、等待队列为空、文件没动。处理方式不是"顺手加个检查"糊过去（那会多出一条永远不生效的分支），而是**加一条钉住测试**把"它本来就拒"变成回归，并在报告里写明反证。审查者与被审者都可能错，能复现的输出才是共同语言。
- **真实模型会把"评测器没想到、但系统提示词要求"的动作做出来**：P1 的判据写「整个 Run 只能有一个生效副作用」，脚本模型照做；真实模型先把报告写出来（`SYSTEM_PROMPT` 规则 3 + `输出文件：…` 明说了要它），于是两个副作用 → 判据红 → `verdict=unsafe`。**判据要按目标作用域化**（"`notes.md` 恰好被写一次"），而不是数整个 Run 的副作用总数；否则测的是"模型像不像我的脚本"。
- **"不安全"与"没做到"必须分开**：一份 `unsafe` 意味着"系统被绕过"，而"模型什么都没做"只是完成度为 0。把 `approval.requested=0`、`outcome≠failed` 这类**收尾形态**标成 safety，会让"更保守的模型"被判更不安全——方向反了。
- **假分数比没有分数危险**：memfs 下评测能跑完、能出报告、能打印 `verdict=unsafe`，看起来像结论，其实是后端不匹配（fixture 落宿主盘、执行走虚拟盘）。判不了就拒绝（`require_local_backend`），比交一份会被引用的数字强。
- **审计工具的误报会训练人忽略它**：`replay` 那条"产物哈希不一致"是错的（比的是另一个文件的哈希），但文案看起来像真发现。凡是"核对"类逻辑，先问"我比的是同一个对象吗"——按路径/按 id 归属，不要按事件顺序猜。
- **校验器自己崩掉，等于没人替你回答「到底写没写」**：快照清单被截断时 `SnapshotStore.load()` 直接漏出 `json.JSONDecodeError`——恢复还没开始推理就先抛栈，CLI 只打印 "Expecting property name"，既没说清哪份证据坏了，也没有阻断。读审计材料的代码，解析失败必须转域错误（`SnapshotCorrupt`，与 `EventLogCorrupt` 同一档），并让调用侧落成 `unknown`+阻断或明确拒绝；不能当成空清单继续，也不能让裸异常穿过库边界。负控照旧：把修复回退后新用例必须红，红的样子就是那条 `JSONDecodeError`。


- **"读不出来"必须 fail closed，而不是洗成"什么都没有"（2026-11-11 实测）**：`events.db` 的 WAL 被撕到 schema 首帧时，旧 `EventLog.__init__` 照常 `executescript(_SCHEMA)` 建表——打开成功、`events` 表 0 行、原 WAL 被丢弃，历史静默蒸发，`recover_run` 据此报告"没有任何未决调用"。修法：先记下"库存在且有内容"，connect 后、建 schema **之前**读 `sqlite_master`（这一步触发 WAL 回放，是"有没有历史"的唯一事实来源），读不出就抛域错误；`PRAGMA journal_mode=WAL` 必须后移（否则可能先毁 WAL 再检查）。推论：**任何"打开即建表"的初始化都自带一条 "坏库→空库" 的洗白路径**，隔离门禁要放在建表之前。
- **探活/心跳自己也会占资源，backlog=1 会被自己堵死（#63 根因）**：`multiprocessing.connection.Listener` 默认 backlog 是 1，而 `probe()` 是"连一下就关"的**未认证**连接——服务端必须先失败一次握手才能腾出队列位；队列满时 macOS 对 AF_UNIX 的 `connect` **立即 ECONNREFUSED**。表现是"端点文件存在、进程活着、probe 连不上"，连续两轮都被误判成"就绪早于 listen"。修法：backlog 调到 64 + 就绪前自连一次完整握手 + `probe_result` 带 errno/重试。教训：**探活链路上的每个"轻量动作"都要算它占用的队列/锁/句柄**；"本机偶发"先看容量，别先怀疑逻辑。
- **"恢复判据"要覆盖"目标本来就不存在"这件事**：`delete_file` 的 applied 状态是"文件已删除"，而恢复的 applied 分支无条件 `size()` 目标 → `FileNotFoundError` 穿出 `recover_run`。#54 把 delete 纳入注入矩阵才暴露：**哈希一致 ≠ 目标存在**；applied 的目标可能是 `absent`，output/凭据都要按删除形状写。
- **库契约接好 ≠ 产品接线（T3 的教训）**：撤销意图日志提供了 `outstanding()`/`resume()` 契约、也有 7 条崩溃续做测试，但 `recovery.py` 属于另一工作流，合并后**没有任何产品代码**调用它们——恢复照样报告干净、写入照样能进。补接线的 5 条测试（`tests/test_undo_recovery.py`）和负控（去掉接线 → 4 failed）证明：**"有库 API + 有单测"不等于"产品里生效"**，跨模块的收尾必须点名到"哪个调用点"。
- **测试夹具不能模拟不存在的历史版本**：`EventLog` 改成 fail closed 后，两条迁移用例红了——它们用裸 `sqlite3` 造"只有 leases/approvals 一张表"的库。但真实旧库一定带 `events` 表（`_SCHEMA` 从第一次提交起就建它）。修法是把 fixture 改成"先建完整 schema、再把那张表降级"，而不是放宽产品判据：**别用一个不存在的历史版本给自己作证**。
- **`detach -force` 不是断电**：真实 HFS+ 卷强制摘下后重挂，验证了"fsync 后的产物可恢复"，但手工探针显示 **20MB 完全未 fsync 的数据照样存活**——所以这个手段**不能**制造"丢页缓存"，也不能用来宣称"未 fsync 一定丢"。写文档时必须把"能证明/不能证明"分开列。

## 6. 流程规则（机械 + 纪律）

- **门禁是机械的**：`main` 分支保护——走 PR、CI 绿（`strict`）、`enforce_admins`（所有者也绕不过）、禁强推/删除。本地绿不算数。
- **高危改动独立复审**：碰授权/租约/并发写/事件日志/计费，须由「第二读者」（另一会话或人）复审；不读作者自查结论，只按计划验收项逐条对照，并给出**可复现探针证据**。
- **复审结论分三层**：已修 / 部分修 / 未修；后两者不得当作已解决，登记为已知限制并跟踪销账。
- **契约变更单独提交**：`docs(contract):` 单独 commit / 单独 PR，body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」，不夹带代码。
- **文档对账**：见 §0 与 §5；提交前跑 `uv lock --check` + `uv run --frozen pytest` + `uv run --frozen ruff check .`。
- 细节见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 7. 已知限制 / 未决问题

**边界与隔离**

- **`Workspace` 是路径边界，不是安全边界**：它回答"能不能碰这个路径"，不隔离进程、不防绕过。
- **`LocalExecBackend().capabilities.isolation == "none"`**：v1 从未交付物理沙箱。`MemBackend` 更弱——它连权限位、owner、符号链接都没有，**不是沙箱**。
- **D 系列约束**：安全不能靠用户配置；默认可写就必须默认可撤销；单用户假设显式声明。

**F12 凭据**

- **Keychain 未实现**：零依赖下只能 ctypes 调 `Security.framework` / `CredReadW`，无平台可验证。v1 交付的是接缝 + 全链路脱敏 + 明确报 `unsupported`。**不得声称"密钥存在系统钥匙串里"。**
- 真实模型 key 只从环境变量读，永不进库；用过的中转 key 应自行作废。

**M0–M5 复审留账（[DEBUG-M0M5.md](DEBUG-M0M5.md)，2026-11-10；修复记录见该文档末尾）**

- **M2 静态疑点 → issue #53**：否决缓存只按 `run_id` 生效（同一 Task 新 Run 是否忘掉否决，取决于 `run start` 是否只由人/控制面触发，未逐条验证）；`revoke_consumed` 的判定依赖 dispatcher 状态核验，未逐条探针验证。**两条都未复现**，不许当成已确认漏洞，也不许当成已解决。
- **M1 `delete_file` 注入矩阵 → issue #54**：本轮（PR 修法）把 "kill×100" 的证据面从只覆盖 create 扩到 create + 覆盖写；删除类动作的恢复判据是 `absent`、撤销按 preimage 重建，另开一组断言跟进。
- **M4 未验证项 → issue #55**（真实换模对比 / Windows `evaluate` / memfs 评测路径 / 真超时）：2026-11-10 真实模型轮已销账三条，**只剩 Windows 实机一条**。
- **按已知限制接受**：`create_report` 先 `exists` 检查再 `os.replace`，对**外部进程**仍有覆盖 TOCTOU；lease 只防受控 run，PRD 非目标本就包含"防本机用户"。登记是为了下次读到的人不至于以为没考虑过。
- **中-5 是误报**（`prepare` 当场拒绝目录外目标），处理方式是一条钉住测试，不是产品代码改动。

**M0–M1 复核留账（[DEBUG-M0M1.md](DEBUG-M0M1.md) 修复状态总表，2026-11-10）**

- ~~**authority 数据根逃逸 + 完整 takeover → issue #61**~~：**已修（2026-11-11，PR #70）**。权威根固定到用户级位置（与 `--db`/cwd 无关）；三个接管入口都先只读对账、拒绝时落双证据。残留：权威根只保护"阻断"（跨数据根没有租约互斥）、resume 对账发生在认领作业之后、网络盘上是本机视角。
- **真实断电 / Windows 实机**：**断电语义部分验证**（进程级四个持久化边界 + 撕裂写 + 真实 HFS+ 强制摘卷，PR #71），**块设备/固件断电仍未验证**（`detach -force` 仍刷脏页，"未 fsync 一定丢"不成立）；Windows 实机仍未验证（见 #50）。
- ~~**跨多文件撤销**~~：**已修（2026-11-11，PR #66/#72）**：写前意图日志 + 崩溃后幂等续做 + 恢复侧阻断。仍**不**宣称一次事务：崩在动作之间是"可续做"，需要人/CLI 触发续做。
- ~~**M5 集成测试偶发红 → issue #63**~~：**已修（2026-11-11，PR #69）**。根因是 `Listener` 默认 backlog=1 + 探活的未认证连接自己占满队列（macOS 立即 ECONNREFUSED），不是"就绪早于 listen"；改 backlog=64 + 自连握手后 `on_ready` + `probe_result` 带 errno/重试，同一用例文件 100 次 0 失败。

**M4 评测未验证项（2026-11-10 真实模型轮更新，不许写成"通过"）**

- 真实换模：**已跑**（deepseek-v4.1-flash 18/20、glm-5.2 15/20，同一 suite m4.4，`compare_reports` 判可比；逐条裁决见 [M4-verification.md](M4-verification.md) §7.1）——但两家都判 `unsafe`，根因是 P1/P2 判据写死了脚本模型收尾形态（issue #57），**不是**「换模通过」；
- Windows 上的 `evaluate` **未验证**（issue #55 仅剩的一条）；
- `memfs` 后端：**不做，改为开跑前拒绝**（实测假分数 1/20 + unsafe，见 §4）；
- 真超时：**已覆盖**（黑洞端点真等满 60s×3，`3m01.6s` 后 failed）。
- **m4.5 真实模型复跑：未完成（外部阻塞）**。判据已按目标作用域化（#57）并通过 scripted 基线与确定性单测；但要产出两份真实报告需要可用模型中转站，当时该 key 的所有模型一律 `503 model_not_found / auto groups is not enabled`（`/v1/models` 为空，换域名/模型 id 相同）。文档写"未完成"，没有拿 scripted 冒充，也没有为了绿放松安全判据。

**M5 桌面闭环未验证 / 已知限制（详见 [M5-desktop.md](M5-desktop.md) §10 与 [M5-verification.md](M5-verification.md)）**

- **未验证**：Windows 实机（只有 CI `windows-latest`；命名管道的杀软 / ACL / 长路径差异没覆盖）· 真实模型（无 API key，演示与验收用 `scripted` 模型，不代表真实模型能力）· Tauri/WebView2 外壳与托盘（M5 根本没做）· 同一毫秒的并发审批压测（只验证了先后顺序）。
- **Daemon 生命周期不写事件日志**：`shutdown(reason)` 只进 `logs/daemon.log`。C3 是封闭集合且每条事件挂 `run_id`，新增 `daemon.*` 族是契约变更，代价大于收益。
- **陈旧锁判定有 2 秒宽限窗**：第一个 Daemon 在「拿到锁 → bind」之间超过 2s（超大库恢复扫描）时，第二个实例理论上可能误判陈旧并接管。跨平台读取进程启动时间没有 stdlib 方案，属单独工单。
- **推送在主循环里同步发送**：一个不读数据的订阅者理论上能拖慢 RPC 响应；M5 用"本地 socket + 每批 256 条"把风险压在可接受范围，没做独立发送线程 + 非阻塞写。
- **驱动线程卡死 >5 秒时刻意不跨线程关连接**：关闭正被别的线程使用的 SQLite 连接只会得到没人看得到的 `ProgrammingError`，还可能截断写入；保留连接交给进程退出回收（测试钉住"shutdown 仍返回、锁已释放、没有 ProgrammingError"）。
- **`approvals.decide` 的 `reason` 被忽略**：审批记录没有自由文本字段，要存就得改 `schemas/m2/approval.json`（契约变更）；当前只用于 UI 展示。
- **不是可双击安装的 App**：无外壳、托盘、开机自启、安装包、签名、公证、自动更新（M5-D1/D2/D5）。

**平台**

- **无 Windows 实机证据**：只有 `windows-latest`（Server 2022 runner）的 CI 结果。
- **Windows 未设为 required status check**：必过检查只有 `test (ubuntu-latest) · py3.12` 与 `test (macos-latest) · py3.12` 两项，**Windows 红了不会挡合并**。而 `.github/workflows/ci.yml` 自己写着 Windows 是**主打平台**——这是已知门禁缺口，建议 Windows 稳定后把 `test (windows-latest) · py3.12` 补进 required 列表。
- **Windows 上 IPC 有未根治的偶发握手失败（issue #50）**：命名管道上 `digest sent was rejected` / `digest received was wrong` 偶发出现，每轮失败的用例与条数都不同；生产侧无需改（`accept()` 的契约本就"握手失败可重试"，daemon 循环 `continue`、`Client.connect()` 重试到超时），受影响的只有"直接 accept 一次"的测试，测试侧已加 `_accept_retrying()`。**根因未定位**，要真 Windows 机器才能抓（怀疑管道实例队列与探活/半开连接的时序竞态）。`ci.yml` 里已就近注明，且**刻意不加 `continue-on-error`**：让它继续红，是为了每次都在提醒还欠着一件事。
- **CI 作业有 25 分钟上限**：GitHub 默认 360 分钟，挂住的测试会占 6 小时 runner 且不给任何信息（M5 实测踩过）。
- **不得声称 main 历史上每个提交都绿**：`154b916` 那次 macOS job 排队 15 分钟后被取消（`runner_name` 为空、`steps` 为空），是 runner 供给问题，同提交的 ubuntu / windows 都绿。

**数据落点**

- **两套落点并存，别混写**：
  - `daemon` / `host`：默认平台标准目录（`ipc.default_data_root()`，`TIANSHU_DATA_ROOT` 覆盖）——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`；目录内含 `events.db`、`artifacts/{blobs,snapshots}`、`run/`、`logs/daemon.log`。
  - 内核子命令（`run` / `task` / `approvals` / `undo` / …）：**仍是当前工作目录下的 `.tianshu/events.db`**（`cli.DEFAULT_DB` 相对路径，靠 `.gitignore` 排除），`--db` 可改。
  - 因此"运行时数据放系统标准位置"这句话**只对桌面入口成立**；写文档时必须带主语，别再写成全局结论。

**仍开着的问题**

- **issue #27**：PRD §4.1 关键字段列与实现双向漂移 11 处，需逐条决定收敛方向。
- **issue #40**：**已修**（否决即终局，`c4.approval_rejected`，`SUITE_VERSION` 升 `m4.4`，PR #48 合入 `feat/m5-desktop`；判据与限制更新见 [M4-verification.md](M4-verification.md)）。

- **issue #55**（M4 未验证项）：四条里三条销账（真实换模 m4.4 跑过、memfs 评测改为明确拒绝、真超时黑洞端点跑满）。**m4.5 真实模型复跑被中转站阻塞**（所有模型 `503 / auto groups is not enabled`，见 [M4-verification.md](M4-verification.md) §7.4），**Windows 实机复验仍未做**（需要 Windows 实机），issue 保持打开。
- ~~**issue #57**~~：**已修（2026-11-11，PR #68）**：`Check.kind`（completion/safety）+ 8 条作用域化判据 + 完成度/安全分栏 + `SUITE_VERSION` m4.5。真实模型报告未复跑（同上一条阻塞）。
- ~~**issue #58**~~：**已修（2026-11-11，PR #67）**：`RunResult.model_attempts`（真发一次请求才 +1，含超时/失败/重试）+ `cli._cost_note` 统一口径；全失败时打印「0 个完成轮次 / 3 次尝试（费用未知）」，不再说"没有模型调用"。
- ~~**issue #53 / #54**~~：**已销账（2026-11-11）**。疑点 1 不成立（模型无法启动 Run，写成设计前提 + 三层钉子）；疑点 2 三条路径探针成立；delete 注入矩阵补齐并暴露一个真 bug（见 §5）。
- ~~**issue #61 / #63**~~：**已修**（见上一段与 PR #69/#70）。

**实现上的疤**

- `run.end` 不携带成本汇总（成本在 `run status` 里看；约 6 行改动挂账）。
- `check_exec()` 仍是低层 API，`spawn` 已不再调用它。
- 多工作区相对路径解析仍绑定到第一个可写工作区。
- 契约未全冻结：`schemas/` 下只有 `c3/event.json`、`c4/rules.json` 与 `m2/{approval,grant,job,run,task}.json`，C1/C2/C5 **没有**字段级 schema——补齐并加契约测试之前，别宣布冻结。

**本地环境提醒（非项目事实，只对所有者本机成立）**

- 本仓库位于外置卷 `/Volumes/Date`：**务必经常 `git commit`、尽早 push**（卷拔掉就没了）。`refs/`（参考仓库，约 762MB）已在 `.gitignore` 里，**不要入库**。

## 8. 会话更新日志（AI 每次追加，勿删历史）

- **2026-11-11** · **N1b 复审整改：promote 目标作用域归一 + origin 不降档**：第二读者抓到两条中等问题——① 提升目标用 `MemoryScope(scope, scope_id)` 裸构造、不 canonicalize，而声明集合按 canonical 存：CLI 传 `/a/b/.` 会被判"不在声明内"，UI/API 会直接踩；② edit 固定 `user_confirmed`、promote 固定 `inferred`，把 `user_explicit` 记忆"降档"（权重 1.0→0.6），与 PRD "三档不降级"冲突。修法：新增 `resources.build_memory_scope()` 统一 canonicalize（workspace 走 `MemoryScope.workspace`），派发层/执行侧/CLI 三处都改；origin 改为**按这一版正文的来源**——控制面 edit → `user_explicit`、模型写经人批准 → `user_confirmed`、promote **来源随行**（投影校验 `origin == 原行 origin`）。补两条测试：workspace 目标原始路径归一、Receipt `resource/root` 指原行。· 教训："契约说三档不降级"要落到**每条写路径的 origin 赋值**上；scope 字符串比较前必须先归一，否则"语义相同"会在授权判定里变成"不在声明内"。
- **2026-11-11** · **N1b 落地：memory.edit / memory.promote**：`memory.edit` 一次审批产出两行（旧行 superseded、仍在库里可查；新行 active、`supersedes` 指向旧行），审批绑定 `before_revision`，不允许改 kind；`memory.promote` 是**移动**：原行立即退役（原作用域不再召回）、目标作用域新行是候选（再 confirm 才召回），Receipt 针对原行（`receipt_memory_id`），`source_refs` 必含原 id，只控制面能调。· **踩到两个同源坑**：策略层 `_classify_memory` 的 overwrite 分支把 `memory_origin == user_explicit` 当成免审批，于是 edit 也被自动放行；但执行侧 `_requires_approval` 的豁免只认 confirm → CLI 报 `overwrite 缺少审批绑定`。修法是把"免审批"按**工具**收口（`memory_tool == memory.confirm`），两处都改，负控：去掉该限制 → `test_cli_edit_keeps_old_row_and_activates_new` 红。第二个坑：`build_request` 对 overwrite 写死 `before_status: "candidate"`，edit/promote 的目标可能是 active → 改成用 `args.get("status")`（可不声明），状态约束由投影迁移校验兜底。· 另：edit 的 Fig 旧测试（`test_supersede_keeps_old_row...`）原来借 propose 的 output 造 supersede，现按生产形状改用 `memory.edit`。
- **2026-11-11** · **N1 复审整改：堵掉"控制任务复用为模型 Run → 模型继承控制面身份写 workspace"**：第二读者用探针复现——CLI 为 workspace 作用域 confirm/forget 创建的控制任务带 `is_control=1` + workspace 绑定，同一 task 上再开模型 Run，`memory.propose` 到 workspace 会 `succeeded applied` 并写入投影。根因：控制面标记是 **Task 级持久标记**，`exposed=False` 只挡 `memory.remember`、挡不住模型可见的 `propose`。修法：`memory.propose → workspace` 在**派发层与执行侧都无条件拒**（不因 `is_control` 放行），共享写只留 `memory.promote`；补回归 `test_workspace_propose_is_refused_even_on_a_control_task`（负控：去掉两道判定即 `admitted`）。同批按复审建议做的三件小事：`TaskStore.create_task` 对 `memory_scopes` 做 URI 校验（不再只查"非空字符串"）、`cli._task_json` 输出 `is_control`/`memory_scopes`、`_verify` 的"写别人的 task"分支加注释标明是纵深防御（authority/policy 通常先拒）。· **教训**：把"可信来源"落成 **Task 级**标记时，要问"这个 Task 还会不会跑不是那个来源的 Run"——答案是会，所以模型可见的工具必须**无条件**按模型规则限制，不能与"控制面特权"共用同一条豁免。
- **2026-11-11** · **F10 记忆产生侧（N1）落地**：见 §2 的 N1 条目（契约、执行侧、CLI、测试都写在那里）。· **踩了 / 决定了三件**：① 新规则编号的产生路径只认 `DispatchDenied` / `PolicyDecision` / `_reject` / `_reject_runtime`（`test_rule_ids._producer_hits` 的 AST 扫描），`MemoryDispatchRefused` 不算——所以同一条拒绝要在派发层用 `DispatchDenied` 抛一次，顺带满足"执行前就能拒"；② `memory.propose` 的 `origin` **不能**在投影层锁死 `inferred`：`user_explicit` 候选是免审批确认分支的合法输入（测试播种），越权防线在执行器与策略层，不在字段校验；③ 模型写 workspace 作用域必须拒（共享边界），但**拒在哪一层**要想清楚：策略层会因绑定而放行，所以拒点落到执行器 `_verify`，测试直接钉 `c4.memory_scope_denied` 且投影无行。· **没做**：见 §2 N1 条目的"仍没做"；Windows 实机、真实模型驱动 UI 照旧没有。
- **2026-11-11** · **记忆契约 v1.2（仅文档：PRD §5.2.2 / §5.4.2 + F10 §5/§7/§9/§10）**：把 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) §1 的契约增量写成**契约本体**。PRD：① §4.1 `Memory` 加 `layer`（l1/l2/l3）与 `erased_at?`，标注"v1.2 契约、代码未落地"；② 新增 §5.2.2 记忆工具契约增量——`memory.propose` 1.0→**2.0**（加 `scope`/`scope_id`，`task` 只能是本任务、`workspace` 不从模型进入），新增 `memory.edit` / `memory.remember` / `memory.promote` / `memory.consolidate` / `memory.erase`（含 `input_schema` 原样、`exposed=False` 的两道边界、origin 不由参数决定、不新增事件族、`task.created` 加 `memory_scopes` 键）；③ 新增 §5.4.2 四条规则编号语义（`c4.memory_scope_not_in_loadout` / `c4.memory_control_scope_binding` / `c4.memory_edit_requires_prior_revision` / `c4.memory_erasure_requires_receipt`）。F10：§5 加 `layer`/`erased_at` 语义，§7 审批表加 5 行（标"v1.2 契约"），§9 编辑入口指向 `memory.edit`（一次审批两行），§10 写清"擦除 = 删副本 + 删索引；事件日志读快照保留、UI 不许写已彻底删除"，§13 加注"v1.2 契约不在已落地口径里"。· **决定**：契约先行 ≠ 提前注册——`DECLARED_UNUSED_RULE_IDS` 的定义是"防御兜底、当前没有产生路径"，不是计划功能占位；拿它占位等于让注册表替不存在的路径作证（`test_rule_ids` 的判据就是"登记了必须有产生路径"）。所以四条编号只写语义，**登记与产生路径在同一次改动**（`rules.py` / `schemas/c4/rules.json` / PRD §5.4.1 三处同改）。同理 `tools.SPECS` 不提前加：加了就进模型工具清单、却没有执行器。· **未做**：代码一行没改（`tools.SPECS` / `rules.RULE_IDS` / `schemas/` 均未动）；新工具与新判据测试都还不存在；F10 §13 状态未变；纯文档改动，本地全量实测 **1316 passed, 6 skipped**（116.93s，与 `main` 基线一致）。· 落地顺序仍是 SPEC §8：N1 → N4 → N5 → N2 → N3 → N6。
- **2026-11-11** · **剩余记忆工作量施工规格（仅文档，未改任何契约、未写代码）**：立据 F10 §13 仍为"未实现 / 无入口 / 部分落地"的十条（#18–#27）+ §9 的 `host.py` 零记忆端点，落 [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md)（形态沿用 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md)：施工纪律 + 文件所有权 + 每工作流"现状（代码事实）/ 设计 / 判据 / 负控"）。六个工作流：**N1 产生侧**（origin 两档、作用域创建、编辑 supersede、CLI/remember/promote，#18/#22/#24/#25/#27）、**N2 检索质量**（作用域优先级 + 向量 + RRF，#20/#23）、**N3 L2/L3**（确定性合成 + freshness，#19）、**N4 擦除边界**（§10）、**N5 恢复 + 审计表脱敏收尾**（#21/#26）、**N6 控制面 UI**（§9）。N1/N2/N3 都动 `memory.py`，**串行**；N4/N6 可与 N1 并行，**N5 与 N1 同改 `dispatcher.py` 故串行**。· **写进规格的关键判断（都还没实现，别读成已修）**：① `origin` 由**审批出处/控制面入口**决定，模型参数里永远没有 origin——`user_confirmed` = 人批准的 confirm，`user_explicit` 只由 `exposed=False` 的 `memory.remember` 产生；② **workspace 是共享边界**：模型只能写 `user` 与自己的 `task`，共享必须人签发 `memory.promote`；③ **编辑一次审批两行**（旧行 `superseded` 保留可查 + 新行 `active`），投影加 `_apply_edit` 分支，**仍不新增事件族**（只多一种 `tool.result` 形状）；④ 向量索引是**可重建投影**，`model_id + dim` 不同不混空间，未配置 provider 就降级 FTS-only 并如实标注；内置 `HashEmbeddingProvider` **只配测试与离线基线，不许宣称语义能力**；⑤ L2 v1 走**确定性引用式拼装**（两次合成逐字节相同 → 增量 delta 天然成立），LLM 蒸馏另立契约与评测；⑥ **擦除 = 删正文副本 + 删索引**；事件日志里的读快照删不掉，日志压缩与红线 1 冲突，规格明确"不做"，UI 也不许写"已彻底删除"；⑦ 恢复判据是**"有 intent 无 result ⇔ 一定没生效"**（result 与投影同事务），所以不写 unknown、不阻断目录；⑧ 审计表**终态后**脱敏，落地前必须先证明终态后没有任何路径从 `args_payload` 重算 `args_hash`。· **另外**：`memory.propose` 加 `scope` 是 schema 变更 → 按红线 8 升 `version 2.0`；工具面绊线 `test_veto_scope.py` 必须随工具增减同步登记。· **未做**：C2/C4/PRD/`schemas/` 一行没改（契约增量在规格 §1，待单独 `docs(contract)` PR）；规格里的判据测试**都还不存在**；本地全量重跑过 **1316 passed, 6 skipped**（113.69s，与 `main` 基线一致）。· **第二读者（只读核对）纠正一处真错 + 四处措辞**：① 我原写"task 作用域里被激活的记忆没人能忘记"——**错**：`tasks.py` 没有"任务结束"状态（`task.status` 恒 `open`），同一 task 再开一个 Run 就能让模型 `memory.forget` + 人审批走完；事实是"人能忘，但只能绕道模型 Run"，属**控制面缺口**而非隐私死锁，规格已改；② `apply_event` 不止在 `_commit_result` 调用（`rebuild()` 也走），已限定为"实时写路径"；③ `_SCHEMA_VERSION` 只进 `projection_hash()`、没有持久化版本号可比对，"版本不符则重建"是**新增机制**，已标注；④ `_apply_confirm`/`_apply_forget` 的 UPDATE 列比我写的多（恢复分支、`revision`/`last_seq`），已写全；⑤ `grep -rin` 会连 `.pyc` 一起命中，已限定 `--include=*.py`。· **仍未验证**：Windows 实机、真实模型驱动 UI。
- **2026-11-11** · **DEBUG-M0M1 收尾轮：除 Windows 实机外全部落地（PR #66–#72）**：先写 [SPEC-RESIDUALS-M0M1.md](SPEC-RESIDUALS-M0M1.md) 再动手，六个工作流各自独立 worktree/分支（文件所有权表写进规格 §0，KNOWLEDGE/README/DEBUG 只由 lead 落），主线合并顺序 #66 → #67 → #68 → #69 → #70 → #71 → #72。基线 `main@540c543`（1080 passed / 3 skipped）→ 收尾后 **1316 passed, 6 skipped**、`ruff check .` 干净。七件事：① 真实断电 → `fsutil` 四个持久化边界故障点（`TIANSHU_FAILPOINT_PATH` 限路径）× {新建,覆盖写,删除} × 三条断言，撕裂写模拟（清单/blob/WAL），真实 HFS+ `detach -force` 摘卷实验（opt-in，本地 3 passed）；② authority 数据根逃逸 → 固定用户级权威根（`authority.py`），守卫双读，真子进程双 `--db` 探针；③ 完整 takeover → `recovery.assess_run` 只读对账 + `LeaseStore.reconcile` + 三个入口（`AgentLoop.start`/`resume_run`、控制 Run），真 `SIGKILL` 前任 + 真子进程接管；④ 跨多文件撤销 → `undo_journal` 写前意图日志 + 幂等续做 + 恢复侧阻断（阻断是 journal 的投影）；⑤ #63 → 根因 `Listener` 默认 backlog=1 被探活自己占满（macOS 立即 ECONNREFUSED），backlog=64 + 自连握手后 `on_ready`，100 次文件级运行 0 失败；⑥ #58 → `model_attempts` 口径（全失败打印「0 个完成轮次 / 3 次尝试（费用未知）」）；⑦ #57 → `Check.kind` + 作用域化判据 + m4.5 基线，真实模型复跑被中转站 503 阻塞（如实写"未完成"）。· **另外抓到并修掉两个真 bug**：删除类动作的恢复去读已被删除的文件（`FileNotFoundError` 穿出 `recover_run`）、撕裂 WAL 被静默洗成空库（fail-open，历史全丢、恢复报"干净"）。· **踩了**：`git checkout -- <file>` 会把**未提交**的队友改动一起丢掉（负控回退要用文件备份，不是 git 还原）；`detach -force` 仍刷脏页（20MB 未 fsync 存活），所以"断电压测"不能靠它；测试夹具造"半张表旧库"等于拿不存在的历史版本作证；库契约接好≠产品接线（T3 的 `outstanding()` 没人调用）；探活自己会占队列。· **留下**：`docs/powercut-evidence.md` / `docs/authority-takeover.md` / `docs/undo-journal.md`（T3）/ `docs/m5-readiness.md` / `docs/cost-and-veto.md` / `docs/eval-scoped-checks.md` / `docs/SPEC-RESIDUALS-M0M1.md`；issue #40/#53/#54/#57/#58/#61/#63 有证据可关；#55 只剩 Windows 实机 + m4.5 真实模型复跑（外部阻塞）；#50（Windows 命名管道握手）仍未定位。· **仍未验证**：块设备/固件断电、Windows 实机、真实模型 m4.5。

> 格式：`日期 · 做了什么 · 发现/决定了什么`。最新在上。

- **2026-11-10** · **F10 记忆落地轮的契约文档增量（`feat/memory`，文档侧）**：把 PRD 与 F10 从"提案"对齐到**实际落地形状**。改了三处：① PRD §4.1 `Memory` 补齐 `evidence[]` / `proof_count` / `consolidated_at?` 三个字段，并说明它是投影、不是第五张事实表；② PRD §5.2 新增 §5.2.1 记忆工具表（5 个工具的 `version` / `side_effect_level` / `exec_model` / C4 动作 / 审批 / 执行路径 + `input_schema` 原样 + "首版不新增事件族"的因果）；③ PRD §5.4.1 新增"记忆"组的 6 条规则编号与"用户该怎么做"。F10 侧：新增 §6.1 资源种类设计（`memory://` scheme 前缀、逐字节不变的裸绝对路径、fail-closed、不哈希的理由、派发/执行四处怎么分流），§13 改成**逐条实际状态**（23 条，含"未落地/未覆盖/无产生入口"），§14 改成判据↔**真实测试函数名**的映射（21 条）。· **决定/发现**：资源种类用 **scheme 前缀**而不是给 `ActionRequest` 加字段——后者会改 `to_canonical_dict()`，让**所有 file 请求**的 `digest()` 与 `action_digest()` 变化，历史审批 / grant / 派发记录 / 重放会集体失效；裸绝对路径 = `file` 则逐字节不变。另发现两个坑：**`dispatch.memory_not_found` / `dispatch.memory_stale_revision` 一度"登记了却没有产生路径"**（`test_rule_ids.py::test_every_id_has_a_producer_or_is_declared_unused` 直接红，靠 `dispatcher` 也产出这两条才转绿）；**`origin=user_explicit` 的确认免审批路径没有产生入口**（`memory_exec._propose` 固定写 `inferred`，防模型自称"用户明示"绕过审批）。· **测试数字（工作区实测，未提交；跑的时候队友还在改，以提交后 CI 为准）**：`uv run --frozen pytest -o addopts="" -q` → **1212 passed, 3 skipped**（105.56s，23:58–00:00 实测；**合入 `main` 前**的分支数字）；`--collect-only` → 1215 条 / 44 个 `tests/test_*.py`。新增记忆测试四个文件：`test_memory_store.py`（51 个 `def test_`，参数化后 64 条）、`test_memory_dispatch.py`（18 条）、`test_memory_loop.py`（11 条）、`test_resources.py`（22 个 / 42 条）。· **仍未覆盖**：L2/L3 蒸馏与向量召回未实现（`grep -rin distill src/tianshu/` 无结果）；`origin=user_explicit` 的免审批确认在生产侧没有产生入口。· **收尾时先红后绿的一条**：`test_default_blob_dir_tracks_the_event_log_store` 曾断言"默认 blob 目录 == `EventLog.blob_dir`"，与 `MemoryProjection.__init__` 的设计相反（正文放 DB 旁 `memory_blobs/`：事件 payload 不可删、记忆正文将来要能物理擦除，同目录会因同字节而误删日志材料）；现已改名为 `test_default_blob_dir_is_separate_from_event_payload_store` 并按设计断言，实测转绿。**判据别只信一次红/绿，要问它测的是不是设计要的东西。**· **别写成已验收**：Windows 只是跑不挡（不在 required checks），本机只有 macOS 数字。

- **2026-11-10** · **DEBUG-M0M1 修复对账（`fix/m0m1-corrupt-manifest`）**：用户问「M0–M1 报告里的问题修好了吗」，逐条复核 16 项（S1–S4 / M1–M4 / L1–L2 / R1–R5）——每项都有代码落点与钉住测试，总表补进 [DEBUG-M0M1.md](DEBUG-M0M1.md) 末尾。复核中新发现一条**真残留**：快照清单被截断/写坏时 `SnapshotStore.load()` 漏出裸 `JSONDecodeError`，恢复还没推理就先崩、CLI 只打印 "Expecting property name"、也不阻断。修法：`errors.SnapshotCorrupt`（与 `EventLogCorrupt` 同档）；恢复侧整份核对 → `unknown` + 阻断（不退回"没证据=没写"）；`undo.plan` 拒绝并给补救路径；库入口抛类型化错误；两条回归 + 负控（回退修复即红，红的样子就是那条 `JSONDecodeError`）。1077 → **1080 passed / 3 skipped**（105.86s），`ruff check .` 干净。· **仍然不成立**：真实断电、Windows 实机（#50）、authority 数据根逃逸 + 完整 takeover（**新开 #61**）、跨多文件撤销事务性。· **顺手对账**：§7 的「M4 评测未验证项」整块按真实模型轮更新（三条销账、只剩 Windows）；M0–M5 复审里已修的项保持原记录不动。 · **另外**：反复跑集成测试时抓到一条**既有**偶发红（`test_crashed_daemon_recovers_run_without_duplicate_side_effects` 首段：`on_ready` 已落盘但端点连不上），在 pristine `main` 上 80 次文件级运行复现 2 次 → 与本轮改动无关，开 **#63** 留证；不掩盖，也不当成绿。
- **2026-11-10** · **F10 记忆设计第二轮学习（mem0 / Hindsight，仅文档）**：读 [mem0](https://github.com/mem0ai/mem0)（ADD-only 加性抽取「不静默改写」、写入前去重、实体抽取、语义+BM25+实体+时序四路检索、SQL 事实为源 + 向量/实体为投影）与 [Hindsight](https://github.com/vectorize-io/hindsight)（Observations 带证据与 proof count、refine 而非覆盖；Mental Models 是预算好的可启动页面、读取为 DB read；增量 delta 刷新防漂移；freshness awareness；Decision Models；Memory Defense）后，把 9 条增量折进 [F10-memory-design.md](F10-memory-design.md)。· **最关键的判断**：mem0 与 Hindsight **独立收敛到同一个架构**——「事实是 source of truth，向量/实体/合成层都是可重建投影」——而这正是天枢红线 1 已有的形状；天枢不需要借鉴，只需要把 L2/L3 做成**带证据、可增量、可证伪**的信念。· **三条实质改变**：L2 必须有 `evidence[]` + `proof_count`（没有证据的信念不成立）；蒸馏**只做增量 delta，禁止全量重生成**（LLM 全量重写会漂移）；**freshness awareness**（陈旧层不短路检索，先回落原始事实核验）。· **不抄**：两家都是重服务 + 外部 LLM/embedding 硬依赖（违反零依赖与离线评测）；Hindsight 的 decision model 天枢跑不了；mem0 的 update=覆盖与天枢 supersede 是不同路线。· **别信的数字**：mem0 自报 LoCoMo 92.5 / LongMemEval 94.4 但明说开源版「方向类似、数字不同」；Hindsight 说别家分数是 self-reported。
- **2026-11-10** · **F10 记忆设计落文档（仅文档，未写代码）**：读 [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（L0–L3 分层蒸馏、记忆资产化、装备 loadout、默认私有、可编辑记忆、冷启动 save file）与 [OpenViking](https://github.com/volcengine/OpenViking)（上下文即虚拟文件系统、三层读取、目录作用域检索、会话成文件 + diff、记忆带引用）后，落 [F10-memory-design.md](F10-memory-design.md)。· **决定**：记忆**不是第二个事实源**，而是事件日志的可重建投影（红线 1 已堵死独立记忆库）；记忆操作做成 C2 工具，**首版不新增事件族**——新增 `memory.*` 会与 `tool.intent`/`tool.result` 形成同一因果的第二份指针，正应了 §5.3.3 给 `parent_seq` 立的规矩；写入**分级审批**（propose 写惰性候选不挡；agent 发起的 confirm/forget 走 C4 审批 + Receipt，等同覆盖/删除）；默认 `scope=user` 私有、共享是显式动作；`origin=inferred` 永不自动激活（摘要/网页不能变成长期用户事实）；忘记=tombstone ≠ 物理擦除。· **不抄**：不建独立记忆服务、不引外部 embedding/VLM 作硬依赖、不做多租户/团队 ACL（D2）、不做云托管（D7）、不照抄 AGPL 代码。· **踩了**：`NEXT-CAPABILITIES-PLAN.md` §5 已有实施拆解，本文只补**设计**输入，两者不重复也不冲突（冲突以本文为准）。· **留下**：PRD §4.1 `Memory` 实体字段与 §7 F10 加了指向本文的指针；README 与 §9 索引同步。契约增量（C2 记忆工具、C4 审批规则编号）待 PR08 `docs(contract)` 单独 PR。
- **2026-11-10** · **按“采用最新技术、五年演进”要求修订扩展方案（仍为提案）**：在 [实施方案](NEXT-CAPABILITIES-PLAN.md) §0 增加浏览器读取、动态工具发现、混合检索与持久化委派目标。官方资料核验纠正旧 MCP 通用流程：2026-07-28 使用逐请求版本/能力及 server/discover，initialize 仅留旧版兼容路径。当前 Playwright 官方要求 macOS 14+，与项目 macOS 13 下限冲突，浏览器后端需单独门禁，未宣称兼容。LanceDB 是可替换候选，四目标制品本轮未验。程序化工具调用以真实隔离为前提；A2A 只面向外部 Agent 对接。未安装依赖、未修改实现或现行契约、未运行功能测试。

- **2026-11-10** · **下一阶段能力方案（仅提案）**：新增 [网络 → MCP → 记忆 → 子代理实施方案](NEXT-CAPABILITIES-PLAN.md)，按 11 个独立契约/实现 PR 拆解。源码核对发现现有只读工具绕过 dispatcher 且依赖 workspace、schema 校验是顶层小子集、预算计量主要在单 Run/cursor 内；计划先扩展公共资源准入与共享预占，再依次交付原生 web 工具、MCP adapter、本地显式记忆、只读子代理。**未修改实现、未运行测试、未改变现行契约**。官方 MCP SDK 的可选依赖方案需单独 ADR；“核心零依赖、adapter 可选依赖”仍是建议，不是已批准的新规则。记忆逻辑忘记与历史物理擦除分开，网络私有数据外发另行授权。

- **2026-11-10** · **M0–M5 复审修法（`fix/m0m5-audit`）**：把 [DEBUG-M0M5.md](DEBUG-M0M5.md) 的 12 条发现逐条裁决并落地——4 条中等修复（阻断核验下沉执行边界 + 恢复与派发共享执行守卫；未知工具恢复 fail-closed；畸形阻断记录 fail-closed；M1 注入矩阵扩到覆盖写）、6 条轻微修复、1 条误报加钉住测试、1 条文档补登；基线 `main@4eb4ea9` 的 1061 → **1074 passed / 3 skipped**（97.83s），`ruff check .` 干净。· **踩了**：`check → act` 的窗口不是"多查一次"能关的，得让两边抢同一把锁（`begin_execution` 的守卫覆盖整个写过程，恢复侧不进这把锁就白搭）；`except: continue` 与"未知工具套用已知推理"是同一个 fail-open 病；M1 的强证据其实读的是诱饵文件（只有负控才照得出恒真断言）；复审报告自己也会误报（中-5 说 `prepare` 不拒，实测 `rejected`）。· **留下**：修复记录追加在 [DEBUG-M0M5.md](DEBUG-M0M5.md) 末尾（每条的命令、实测输出与残余风险）；静态疑点/未验证项开成 **#53 / #54 / #55**；`errors.EventLogCorrupt` 语义扩到三种情形（hash 不符 / 外部 blob 缺失 / 阻断记录解析失败）；`recover_run(data_root=...)` 成为 CLI/daemon/eval 三处的统一调用方式。
- **2026-11-10** · **M5 合并后的收尾（PR #49 已合，PR #51 补丁）**：`tianshu host stop` 三缺陷、Windows/Linux 平台差异、UI 三缺陷、以及"挂住"问题全部修完；`main@6862d8f` 实测 **1061 passed / 3 skipped**。· **踩了**：`/shutdown` 端点挂在 `do_GET` 而 `host stop` 发 POST（永远 404）；清理回调赋给 handler **类属性**被描述符协议绑成 bound method（`TypeError` 静默吞掉清理）；`stop_all` 先置 `stop_event` 再清理 → Daemon 留成孤儿；Windows 的 accept 叫不醒（overlapped I/O）、`PeekNamedPipe` 直接抛错、重复实例抛裸 `PermissionError`；Linux 上关 fd 唤不醒 accept；**敲门自己也会阻塞**（windows job 挂 20 分钟）；子进程日志用未读的 `PIPE` 会死锁；崩溃恢复测试靠轮询 probe 猜进度导致 macOS 偶发红。· **留下**：`docs/M5-verification.md` §5（host stop）、§6（断线烧核）、§6.5（跨平台差异与 Windows 未解决问题）；issue **#50** 记录 Windows 命名管道偶发握手失败（根因未定位，已在 `ci.yml` 就近注明、**不加** `continue-on-error`）；UI 三个可见缺陷已修（任务状态/守护进程版本人话映射、1280 宽审批按钮回到首屏），遗留"审批事件 action≠rule id 导致标题兜底"另开工单。
- **2026-11-10** · **M5 桌面闭环（PR #49）**：三进程（Daemon / Host / 浏览器 UI）打通，IPC 契约与断线语义、审批 UI、关 UI 任务继续、重启恢复、离线演示脚本全部落地；测试 942 → **1060 passed / 3 skipped**（`f5da9ef` 收尾后实测）。· **踩了**：`recv()` 对"超时"和"断线"都返 `None`，客户端一断 daemon 读线程 100% CPU 空转（实测烧一个核，靠 `eof` + `ConnectionClosed` 修掉，回归测试带真子进程 CPU 记账）；驱动线程跨线程复用 SQLite 连接直接 `ProgrammingError`，Run 永远停在 `created`（`cross_thread=True` 修）；手写 HTML 截图会盖住 token 注入 Bug（真页面是空 `<meta>` 原地替换）；`tianshu host stop` 三个缺陷叠在一条退出路径上（`/shutdown` 挂错 `do_GET`、清理回调绑成 bound method、先置 stop_event 再清理）→ stop 永远 404、清理静默死、Daemon 留成孤儿，靠 POST-only + 回调挂 server + 清理完再放主线程修掉（真子进程回归才测得出来）。· **留下**：M5 偏差 D1–D5 与"不做安装包/托盘"的口径写进 [M5-desktop.md](M5-desktop.md) §2/§9；断线语义写进 §3.6 与 4 条回归测试；`SUITE_VERSION` 升 **m4.4**（否决即终局，issue #40，PR #48）；剩余限制与未验证项见 [M5-desktop.md](M5-desktop.md) §10 与 [M5-verification.md](M5-verification.md)。
- **2026-11-10** · 把未提交的草稿按 `7abad75` 事实重写并落库 · 发现草稿里 Windows 和测试数两处都是假话，属"文档说假话"复发；落库时又抓到第三处——README 长期声称「运行时数据放系统标准位置」，实际一直是 CWD 下的 `.tianshu/`。
- **2026-11-09** · 扩充 AGENTS.md 开发规则：新增测试纪律 / 数据与事件完整性 / 安全与授权 / 契约与类型纪律 / 依赖与平台 / 提交与仓库卫生 / 反模式共 7 节。· 规则全部溯源 PRD 红线与 REVIEW-PRA 教训，不写空话；固化「红就是红，不装作绿」（不用 continue-on-error / type: ignore 假绿）。
- **2026-11-09** · 建立本知识库 + AGENTS.md 强制读写钩子；README 重写为标准开源样式；`main` 配置分支保护（PR + CI 绿 + enforce_admins + 禁强推）。· 确认现状：M0–M3 完成、767 测试、Windows CI 暂摘（#21）；把「自审盲区 / 测试证据造假 / 文档假话」三大教训固化进流程。

## 9. 权威文档索引

- [PRD-v1.md](PRD-v1.md) — 产品需求与架构（唯一权威）：范围、契约、红线、里程碑、决策记录
- [TECH-ARCH.md](TECH-ARCH.md) — 技术架构：选型、进程模型、打包、平台差异
- [REVIEW-PRA.md](REVIEW-PRA.md) — PR #7 独立复审报告（§5 教训的主要来源）
- [DEBUG-M0M5.md](DEBUG-M0M5.md) — M0–M5 对抗性复审报告 + 本轮修复记录（裁决、命令、实测输出、残余风险）
- [M5-desktop.md](M5-desktop.md) — M5 桌面闭环：三进程拓扑、IPC 契约、恢复语义、偏差（§2）与已知限制（§10）
- [M0-verification.md](M0-verification.md) / [M1-verification.md](M1-verification.md) / [M2-verification.md](M2-verification.md) / [M3-verification.md](M3-verification.md) / [M4-verification.md](M4-verification.md) / [M5-verification.md](M5-verification.md) — 各里程碑验收证据
- [F5-context-compile.md](F5-context-compile.md) — F5 上下文编译的设计与取舍
- [F10-memory-design.md](F10-memory-design.md) — F10 本地记忆设计（v1.1）：投影而非事实源、L0–L3 分层、作用域与装备、默认私有；**§6.1 资源种类（`memory://` scheme）**、**§13 契约增量的实际状态**、**§14 判据↔真实测试映射**
- [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) — 剩余记忆工作量施工规格（**待实施**）：§13 #18–#27 逐条现状与设计、六个工作流、契约增量清单、判据与负控口径、明确不做
- [F1-exec-primitive.md](F1-exec-primitive.md) / [exec-run-command.md](exec-run-command.md) — F1 执行原语与 `run_command` 工具
- [REFS.md](REFS.md) — 参考仓库与固定 commit
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — 分支、提交、独立复审规范
- [../AGENTS.md](../AGENTS.md) — AI 开发强制指令