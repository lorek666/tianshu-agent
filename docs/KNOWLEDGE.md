# 天枢 Agent · 项目知识库

> **AI 开发必读。** 每次开发前先读本文，掌握架构红线、关键决策、踩坑教训与已知边界；强制指令见根目录 [AGENTS.md](../AGENTS.md)。
> **权威规格以 [PRD-v1.md](PRD-v1.md) 为准**——本文与 PRD 冲突时以 PRD 为准，并顺手修正本文。

---

## 0. 这份文档怎么用（给 AI）

- **开发前**：通读 §1–§7，尤其 §5 踩坑教训、§6 分级门禁与 §7 已知边界；改契约前必读 PRD §5/§6。
- **开发后**：
  - 新增架构决策 / 新踩坑教训 / 边界变化 → 更新本文 §4 / §5 / §7；
  - Tier 1 高危改动或重要演进 → 在 [`docs/changelog/`](changelog/README.md) 新增独立文件 `YYYY-MM-DD-<scope>.md`（**不再往本文集中追加会话流水，根除并行分支冲突**）；Tier 2/3 常规与文档改动直接以规范 Commit / PR 正文留证。
- **不手写静态计数**：模块数、测试总条数、平台跳过数一律以实时命令（如 `uv run --frozen pytest`）与对应提交的 CI 日志为准，不在文档中硬编码易过期的总数字。

## 1. 项目是什么

- **目标**：面向普通用户的桌面 Agent OS——模型自选、在用户指定目录读写、完成长任务；把「AI 可能失控」当一等工程问题。
- **当前形态**：**M0–M5 已完成，R1–R3 组合候选已由 PR [#175](https://github.com/lorek666/tianshu-agent/pull/175) 按保护规则合并到 main。最终候选 `343ebf0e7c1f40dcc276ed3dd247f3a8eec002d0/tree 94f8e34fa306030a5f172f7838bec62f012e3805` 的 run [43889749967](https://github.com/lorek666/tianshu-agent/actions/runs/43889749967) 六项 job 与 Windows required 汇总全部 success；merge commit `219d17ab5ed1744f841039879c1f6b9e12d91f07` 自身的 push run [43893763334](https://github.com/lorek666/tianshu-agent/actions/runs/43893763334) 七项 job 也全部 success，远端候选分支已删除。** M0–M4 是命令行内核；M5 历史基线交付 Daemon + Python Host + 系统浏览器里的 React UI。组合候选包含未签名 Tauri/WebView2 薄壳、托盘、单实例与 Host 生命周期、锁定 Python runtime、四目标 build/verify、macOS 安装/卸载、assembler 与 draft release workflow；签名和自动更新尚未实现。**备份定稿高危缺陷已由 strict-database-fsync 修复并经第二读者 PASS；旧候选 `bede8e3`、`a55e752`、`1a619af` 的三轮原生 CI 红作为历史证据保留，没有用重试或旧绿覆盖。当前仍没有可双击安装并宣称四目标可用的正式成品；用户设备验证、签名/公证/自动更新和 TOCTOU/provenance 边界不能因 CI 绿而扩大。**
- **核心承诺**：事件可重放、执行侧授权、覆盖/删除需审批、默认可撤销、崩溃可恢复。
- **非目标（v1）**：图片/视频生成、多租户、防本机用户篡改（只防模型越权与失控 worker）。
- **技术栈**：Python 3.12（`requires-python = ">=3.12,<3.13"`）、`uv`、**零第三方运行时依赖**（`dependencies = []`）；dev 依赖只有 `pytest>=8.0` 与 `ruff>=0.6`。UI 是 Vite + React + TS，但只有**开发/构建**时需要 Node，运行时加载已提交的 `ui/dist`。
- **版本 / 许可**：`0.1.1` 组合修复候选（尚未发布）；Apache License 2.0（`LICENSE` 已被 git 跟踪）。

## 2. 当前状态速览

- **Goal 模式 G1 候选（2026-11-22，本地未合入）**：支持 `/goal`、目标配置、
  每轮手动推进和逐项人工验收；创建只保存配置，不启动模型。状态由事件及 Run
  派生，失败、空正文及 `partial/unknown` 结果均阻断继续与验收。已整合固定主干
  `7665e8684f5a124fe789f21909ee54325edc556b`；规格见
  [Goal 设计](SPEC-GOAL-MODE-2026-11-22.md)，验证和待验边界见
  [交付记录](changelog/2026-11-22-goal-mode.md)。自动连续推进仍属于未交付的 G2。
- **本地 Skill 命令候选（2026-11-22，尚未合入）**：新建与追问输入框支持
  `/`、`/skills`、`/skill <名称或版本标识> [要求]` 和 `/<技能名> [要求]`。
  只读发现用户与所选工作区的 `.agents/skills`，将选中技能全文保存为本轮输入；
  不增加工具或授权，不安装或自动执行技能脚本。实现范围、验证和未验证边界见
  [本轮记录](changelog/2026-11-22-skill-slash-integration.md)与
  [规格](SPEC-SKILL-COMMANDS-2026-11-22.md)，不代表主干或发行版已交付。
- **macOS outer 资源封套修复候选（2026-11-20，本地未合入）**：打包末端新增免费 no-force ad-hoc 封套及原生 deep/strict 验证，归档受限往返核对后再原生验证；direct artifact 输入只验证不重签。静态 verifier 精确允许可选封套，历史 unsigned fixture 仍可读。定向红绿与本机 tiny-C 结构实验已执行；局部 code/test 及文档增量已完成独立预审；原组合候选已完成限定独立整合预审，精确 `1c63e8297020dad51f72494a9db3230ffc61ec4f`（tree `6bd04d16c66f3e27b78c82be3be7c6fc35f9c891`）的授权 source 全量、lock/Ruff/diff 与即刻复制留证均 exit0，仅归属该冻结。本地已正常整合含 Responses 实现的主干 `f27a1bf` 为新冻结 `f88e2bd`，自身限定独立复核、受影响回归、UI 门禁、授权 source 全量与即刻复制留证均已完成，不沿用原冻结的绿。新 hosted required CI 和产品原生验收仍待完成，不能将源码/结构夹具证据当作新产品 App 验收。旧 `0.1.0` 草稿不可原地修补，详见[局部修复记录](changelog/2026-11-20-outer-bundle-seal.md)。

- **里程碑与主干进展**：
  - **M0–M5 内核与三进程桌面闭环**、**G0 可信评测**、**G1 通用执行基础**、**G2 网络 / MCP / 记忆（L1–L3 + 面板 + 外部 embedding gate + L2 冲突裁决）/ 子代理（含 W10 写声明与子任务面板）**、**W1 macOS Keychain**、**W2 网络连接重试与速率限制**、**W3 `state` 8MiB 响应预算**、**W5 最小定时调度器**、**低磁盘准入**、**恢复 B 的 T0 契约与 T1 平台身份锁底座**、**R0–R3 桌面发行构建与校验链（`desktop/` + `scripts/*release*` + `.github/workflows/release.yml`）** 均已合入 `main`。
  - **动态规模核验口径**（不写死静态数字）：Python 模块见 `src/tianshu/`，后端测试集见 `uv run --frozen pytest --collect-only -q`，前端测试见 `pnpm --dir ui run test`，Rust 桌面壳测试见 `cargo test --locked`（`desktop/src-tauri`），跨平台权威结果以对应 `main` 提交的 GitHub Actions CI 为准。
- **整合基线与新候选状态（2026-11-21）**：原 PR [#198](https://github.com/lorek666/tianshu-agent/pull/198) 的 ready CI [44670311820](https://github.com/lorek666/tianshu-agent/actions/runs/44670311820) 真实失败原件保留；修复后的精确 head `314e987ea5c8d1179caff6a4bcdf488c055f577f` 已通过新 ready CI [44678467012](https://github.com/lorek666/tianshu-agent/actions/runs/44678467012)，按保护规则合入 `a0732d9881247998df24db7af9f1c7989c1ae7a4`，该 main 的 push CI [44681321379](https://github.com/lorek666/tianshu-agent/actions/runs/44681321379) 也全部成功。旧红不被后续绿覆盖。本地 `0.1.1` 组合候选整合资源封套、模型用量 v2 和统一身份修复；原冻结 `1c63e8297020dad51f72494a9db3230ffc61ec4f`（tree `6bd04d16c66f3e27b78c82be3be7c6fc35f9c891`）已完成限定独立整合预审、授权 source 全量与静态/复制证据门禁。本地已正常合并含 PR209 Responses 实现的 `f27a1bfe03140e918cdcaa249d3c5c0863638d28`，形成精确新冻结 `f88e2bd55f796da78fc5ca7346ee8b77b4e241a7`（tree `cce786edc46b842c30ba687c10196ac135e36679`）。该组合自身的限定独立整合复核及提交绑定附录、受影响回归、UI install/typecheck/test/build、授权 source 全量与即时 collector、lock/Ruff/diff 均已完成并核验；全量原始日志 SHA256 为 `c76631831b0191d71fe7c4d9eb6e41fceb6c09c385d23f28d43224ab8a60aa8f`，动态总计及跳过原因以原件为准，不能沿用旧冻结的绿。原 `2b91bfc` 全量 RED、PR210 首轮 Windows collection RED 与各自后续最小修复证据均保留。新 required CI、产品 App 原生验收与真实复评仍待验，不能套用旧绿；详见[本轮记录](changelog/2026-11-21-preview-release-repair.md)及[首轮 CI 红与修复证据](changelog/2026-11-20-integration-ci-repair.md)。
- **当前主要未完成项（[PRD §11.1](PRD-v1.md) 首发：macOS Apple Silicon 限定预览）**：
  - **发行实跑与真机闭环**：`release.yml` 已接入显式 `macos_arm64_preview` 手动开关；默认仍构建四目标，同 SHA 的四项 required CI 与全部 job 成功仍是预览前置。单目标 draft 仅汇总 `macos-arm64`，以索引限制上传文件。精确 a073 已由 [44731411389](https://github.com/lorek666/tianshu-agent/actions/runs/44731411389) 实跑并生成未发布的 `0.1.0` draft；但下载归档 App 的原生 codesign 校验失败，原对象保留、不修补。新候选仍须重新绑定发行与原生验收，并用同一台 Apple Silicon Mac 验收安装、Keychain、核心任务、手动升级/回退和备份恢复，注明实测 macOS 版本。四目标发布实跑、Intel Mac 和 Windows 实机、物理断电不阻塞限定预览；数据安全与进程中断恢复门禁保持不变。Windows / Linux 系统钥匙串后端仍为 `unsupported`。
  - **普通用户首日开箱体验**：极简双区桌面界面（可折叠 `Sidebar` + `WelcomeComposer` 动态 2.5D 伴侣与自由留空/项目带入工作区 + 内联审批 `TaskWorkspaceView` + 按需「运行洞察」抽屉 + 发送钮左侧 `ModelStatusControl` 快速热切模 + 浅/深/跟随系统内存态外观切换）、8 栏设置中心（`SettingsDialog`：模型热切换与 macOS Keychain 密钥写入、安全授权默认策略、定时任务管理、账户预算账本与对账视图、MCP 只读状态、记忆库入口、守护进程水位与默认脱敏诊断包本地导出、外观与伴侣 7 态动效）、CLI `--desktop` 数据目录桥接、以及 **W4 模型流式输出**（`ModelStreamDelta` + Daemon 瞬态推送 + Host SSE + React `RunTimeline` 实时生成预览）均已落地；真实付费模型驱动 UI 的端到端验收仍待可用 API Key 实测，权限下拉中的「自动审批」「完全访问」仍诚实置灰待后端授权契约接入。
  - **G3 数据与合规门禁**包括 F15 CI 依赖许可证校验与 CycloneDX 1.5 SBOM、D7/F14 本地默认脱敏诊断包，以及 W12 C1/C2/C5 字段级 JSON Schema 与 `run.end` v2 微美元成本汇总。受管恢复 B/C 与 W15 离线 GC 在同一个候选中集成。受管写入需要支持目录 `fsync` 的 POSIX 文件系统，Windows 保留只读计划及符合绑定的复核。旧候选验收与原生 CI 历史见 [#198](https://github.com/lorek666/tianshu-agent/pull/198)，本次组合须绑定新对象验证。当前 G3 仍需候选冻结 SLO 实测。四目标正式发行的 ≥10 小时常驻浸泡仍待验；**首次 `macos-arm64` 限定预览已取消强制 soak 门禁，无替代时长，长时稳定性未验证**（见 §4 与 [PRD §10.6 / §11.1](PRD-v1.md)）。
  - **工单与评测**：Windows 锁与审批回归 [#145](https://github.com/lorek666/tianshu-agent/issues/145)、[#147](https://github.com/lorek666/tianshu-agent/issues/147) 已随 [#195](https://github.com/lorek666/tianshu-agent/pull/195) 合入并关闭，见[修复记录](changelog/2026-11-18-fix-issues-145-147.md)。真实付费模型复跑 [#55](https://github.com/lorek666/tianshu-agent/issues/55) 待验。
- **历史逐 PR 状态与 CI 哈希流水**：完整归档于 [docs/changelog/archive-2026-11-status.md](changelog/archive-2026-11-status.md)。

## 3. 架构速览

- **唯一事实源**：SQLite WAL append-only 事件日志（触发器禁 UPDATE/DELETE）。Task/Run/Approval/Grant/Lease 都是同库事务更新的可重建投影。
- **Task ≠ Run**：Task 是用户看到的那件事，Run 是一次执行；改目标会 fence 旧 Run。
- **授权只在执行侧**：模型输出/工具输出/MCP 注解都不构成授权依据；dispatcher 是唯一派发入口，执行前登记 intent、执行后写 result + Receipt。
- **三进程（M5）**：UI（浏览器）→ loopback HTTP + SSE → Host（单实例、监护 Daemon、token 注入）→ IPC（AF_UNIX / Windows 命名管道，`ipc.py`）→ Daemon（任务/事件/审批/驱动线程）。**关 UI 只减少订阅者，不停任务**；重启后由 Daemon 的恢复扫描接手。细节见 [M5-desktop.md](M5-desktop.md) §1/§4/§5。
- **五道契约**（版本化；冻结条件＝字段级 JSON Schema 可校验 + 契约测试全绿 + 第二家 provider 验证过）：
  | ID | 名称 | 当前状态（只写可核实的） |
  |---|---|---|
  | C1 | Model I/O | Chat Completions / Responses / Anthropic Messages 各有独立 adapter；结束原因是封闭集，越界抛 `ModelError`。Responses 实现分支与验证范围见[实施记录](changelog/2026-11-21-openai-responses.md) |
  | C2 | Tool | **23 个 `ToolSpec`、19 个对模型开放**（4 个控制面专属 `exposed=False`：`memory.remember` / `memory.promote` / `memory.erase` / `restore_file`）；`tool_manifest()` + 每种工具的参数要求由工具自己声明 |
  | C3 | Event | **已封闭**：45 个事件类型，写入侧拒未登记类型，且逐类型 payload 有 v1 契约（`event_schema.py` + `schemas/c3/event.json`）；声明了但从未发射的类型 **0** 个 |
  | C4 | Permission/Approval | **rule id 已封闭**：低磁盘 T1–T3 两项空间拒绝与纯读资格拒绝已合入主干，并补齐 W10 可写子任务创建门与控制面/UI F14 人话映射（见 [2026-11-18-t3-audit-closure.md](changelog/2026-11-18-t3-audit-closure.md)）。当前代码/schema/PRD 清单共 78 项，写入前校验。非拒绝判定 10 个（`c4.auto` / `c4.overwrite` / `c4.delete` / `c4.exec` / `c4.mcp_call` / `c4.memory_scope` / `c4.outbound` / `c4.outside_workspace` / `c4.subtask_spawn` / `c4.subtask_cancel`）+ 保留未使用 3 个（`c4.unhandled` / `dispatch.denied` / `c4.subtask_unavailable`）；`c4.approval_rejected` 是 issue #40「否决即终局」新增的拒绝原因；记忆组 6 条（N1 起）见 PRD §5.4.1。审批/凭据见 PRD §5.4 |
  | C5 | Eval | `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）已分离，评测套件 `m4.6`（判据按目标作用域化后逐版本不可比，见 [M4-verification.md](M4-verification.md) §7.4） |
- **封闭集落在三处**：代码里的集合（`events.EVENT_TYPES` / `rules.RULE_IDS`）、`schemas/` 下的 enum（`schemas/c3/event.json`、`schemas/c4/rules.json`）、PRD 对应小节；测试把前两者绑在一起，改一处忘另一处会红。
- **两个「未」不是一回事**：C3 的「声明未发射」是 **0**（没有登记了却不发射的事件类型）；C4 的「保留未使用」是 **3**（`c4.unhandled` / `dispatch.denied` / `c4.subtask_unavailable`，防御性兜底或历史归档，登记为合法但没有产生路径）。别把这两个数字混着写。
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
- **模块地图**：按功能分（五大核心模块采用门面 + 同目录 `_<mod>_*.py` 单层扁平 Mixin/类型子模块拆分，保持零契约变更与非递归 AST 扫描守卫覆盖）：事件与状态（`events` / `tasks`+`_tasks_{types,subtasks,cursor_rebuild}` / `jobs` / `leases` / `snapshot` / `recovery` / `replay`）、授权与派发（`permissions` / `rules` / `dispatcher`+`_dispatcher_{types,authority,exec,ledger}` / `guard` / `receipt`）、模型与执行（`model`/`capability`/`context`/`exec`/`exec_backends`/`exec_seatbelt`/`exec_landlock`/`execpolicy`/`credentials`/`pricing`/`sandbox`）、工具与路径（`tools`/`tool_registry`/`toolschema`/`workspace`/`pathkeys`/`fsutil`/`canonical`）、撤销（`undo`/`undo_journal`）、主循环与入口（`loop`+`_loop_{summary,memory,bookkeeping}` / `worker` / `cli`+`_cli_{ops,memory,eval}` / `errors` / `crashpoint`）、**桌面三进程（`ipc` 传输与契约 / `daemon` 常驻与恢复 / `control`+`settings_control` RPC 映射 / `host` 外壳与 UI 桥）**、记忆（`memory`+`_memory_{types,apply}` / `memory_exec` / `memory_index` / `memory_distill` / `memory_control` / `resources`）、运行与维护（`metrics` 运行指标 / `isolation` 隔离探针 / `backup` 备份恢复 / `restore_contract`+`restore_registry`+`restore_access`+`restore_manager` 受管恢复与解除 / `data_cli` 数据维护入口 / `data_gc` 保守离线安全 GC / `maintenance` 协作准入 / `migrate` 迁移 / `event_schema` 事件 payload 契约 / `diagnostics_export` 脱敏诊断包 / `scheduler`+`schedule_cli` 定时调度）、评测（`eval/`）。细节见 PRD / TECH-ARCH 与 [M5-desktop.md](M5-desktop.md)。
- **CLI 子命令**（`tianshu --help`）：`run` / `mcp` / `task` / `worker` / `recover` / `undo` / `approvals`（别名 `approval`）/ `replay` / `daemon` / `host` / `evaluate` / `memory`（别名 `memories`）/ `schedule` / `data`。
  - `worker` 默认按 `Worker.drain()` 的 64 作业预算分批调用，直到 `exhausted`（队空）或工作区争用停止；显式 `--max-jobs N` 仍只执行一轮。CLI 回归测试必须覆盖超过 64 个排队作业，避免把单轮预算误读为队列清空。
  - `host` 只有 `run` / `stop` / `status` 三个动作（不带动作 = `run`）；`status` 在跑退出 0、没跑退出 1。
  - `replay` 审计不调模型；`evaluate` 换模跑固定 case。
  - 退出码：`0` 成功 / `1` 失败或取消 / `2` 前置条件错 / `3` 需要人决定（等审批、已暂停、待核查、`undo` 未带 `--approve` 的预演）/ **`4` = evaluate 判 unsafe**（安全门槛一票否决）。

- **已实现的功能面**（PRD §7 编号 → 落地模块，模块说明取自代码）：
  - **F1 执行原语** → `exec.py`（argv 模式，非 shell；超时/截断/进程组）
  - **F2 执行策略与沙箱接缝** → `execpolicy.py`（声明式、默认全拒、deny 优先）、
    `exec_backends.py`（默认 `local`，支持 `TIANSHU_EXEC_BACKEND=seatbelt|landlock|auto` 与 `preferred_exec_backend()`；目标后端或平台不可用一律 `PolicyUnsupported`，绝不静默回落 `local`）、
    `exec_seatbelt.py`（W14 macOS Darwin SBPL 进程级沙箱，`macos-arm64` 四边界实测全 `enforced`）、
    `exec_landlock.py`（W14 Linux `x86_64`/`aarch64` 纯标准库 `ctypes` Landlock 沙箱：ABI >= 4 含 TCP 阻断声明 `app_container`，ABI 1..3 如实声明 `process_tree`，preexec 失败 `exit 126` fail-closed）、
    `exec.py`（本地后端、进程树与后端级敏感环境变量清洗 `scrub_sandbox_env`）、`isolation.py`（`PROBE_VERSION = "v2"` 四边界实测探针 + 门 +
    `isolation_view`）。「受信任执行」与非 `none` 沙箱后端的实测隔离等级在**审批前**可见（`ApprovalView.isolation`），
    执行结果/事件带 `output.isolation`；口径见
    [F2-sandbox-policy.md](F2-sandbox-policy.md)
  - **F5 上下文编译** → `context.py`（纯函数、按字节预算、丢弃只记账不摘要）
  - **F9a 模型准入** → `capability.py`（能力声明与准入，只用于路由，不用于授权）
  - **F12 凭据** → `credentials.py`（分域查找 + 全链路脱敏；Keychain 后端 **macOS 已落地**——`keychain_macos.py` SecItem 直调、仅 darwin 加载，W1 交付并经 #151 独立复审；其余平台仍如实报 `unsupported`）
  - **F13 成本** → `pricing.py`（整数微美元；未知价格记 `None` 并单独计数）
  - **F14 界面部分** → `ui/`（React + TS）：机器枚举 → 人话的映射在 `src/lib/humanize.ts`；`digest` / 请求 id / 枚举值不进视图，内部细节放折叠区（PRD §7 功能表 F14 行）
  - 其余 P0 功能落在：`events` / `recovery`（事件与恢复）、`tools`（工具系统）、`permissions`（授权审批）、`tasks` / `jobs` / `leases`（任务与运行）、`snapshot` / `receipt`（产物与回滚）、`undo`（撤销）
  - **F10 记忆** → `memory.py` / `memory_exec.py` / `memory_index.py` / `memory_distill.py` / `memory_control.py`（L1–L3 可重建投影、检索与面板已落地，详见 [F10-memory-design.md](F10-memory-design.md)）

## 4. 关键决策（D1–D7）与硬约束

- **结构封套不冒充发布者信任（2026-11-20）**：最终 macOS App 只执行 `codesign --sign - --timestamp=none`，不 force/deep 签名、不清 xattr、不使用证书或修改 Gatekeeper 策略；已有封套拒绝，其他非 linker 签名由 no-force 原生语义拒绝。只允许主程序与精确新封套变化，原资源/Info.plist/权限不变；原生 strict 验证必须覆盖归档恢复副本。此局部修复不改 C1–C5、T1 成本/信任政策、schema、索引 `signed: false` 或原生启动/数据/凭据验收门禁。静态可选封套兼容不等于生成路径可省略原生检查。

- **本机任务触发（2026-11-21 实现分支）**：间隔沿用原表；日历、目录和本机事件单独持久化，旧程序不会消费。目录仅监听只读工作区中的直属普通文件；事件入口仅接受认证本机客户端提供的任务 ID 和事件 ID。基线或去重记录与入队同事务提交。具体用法、系统时区数据要求及轮询边界见[本机任务触发](changelog/2026-11-21-local-schedule-triggers.md)。

- **事件 Webhook 接收器（2026-11-21 实现分支）**：显式启动的独立进程只监听 IPv4 回环，每个实例绑定一条既有事件日程；签名绑定日程、时间戳、原始正文。请求头和正文共享一个接收期限。验签后使用认证 IPC 交给原调度器；HTTP 重试使用同一事件 ID。部署和验证边界见[本机 Webhook 接收器](changelog/2026-11-21-schedule-webhook.md)。

- **Responses 接入（2026-11-20 契约，2026-11-21 已入主干）**：独立 adapter、无状态加密 reasoning 续接、scope 绑定与 `ctx-responses-v1` 完整上下文；保留旧模式，凭据复用 openai 域。模型步骤固定配置快照，result/turn/cursor 同事务提交；已提交终态在崩溃或暂停后直接收尾，丢失的 Responses cursor/turn 可从事件重建。PR209 已合入 `f27a1bfe03140e918cdcaa249d3c5c0863638d28`；本地新整合 `f88e2bd` 已完成自身限定独立复核、受影响/UI 与 source 全量门禁，但不等于 hosted CI、产品或线上验收通过。实际门禁与未验证范围见[实施记录](changelog/2026-11-21-openai-responses.md)及[续接契约](openai-responses-contract.md)。

- **组合候选的外发边界**：Responses JSON/SSE 只按尝试上限重试完整的 HTTP 429。即使已收到模型终态，也要等 HTTP 正文完整、SSE 事件闭合才写入结果；截断、超时和 HTTP 5xx 保留未决用量。受限表单的网络 Receipt 使用独立 payload v2，普通回执及历史 v0/v1 保留原定义。见[组合候选记录](changelog/2026-11-21-agent-product-capabilities.md)。
- **模型缓存用量逐事件 v2（2026-11-20 契约，2026-11-21 实现）**：C1 已允许的可选缓存用量完整保存在 `model.result` / `run.model_turn` 新写精确 payload v2；保留旧 v0/v1 原解释，新 v2 usage 仍封闭，不升级全局版本常量。不沿用发布前 optional 沿 v1 的历史例外，也不全局废除该例外；缓存不重复计入 input_tokens，未知金额不伪装为零。旧基线 a073 的准入拒新与回退边界见[模型用量 v2 契约](model-usage-v2-contract.md)，Responses 实现验证见[实施记录](changelog/2026-11-21-openai-responses.md)。原本地组合 `1c63e8297020dad51f72494a9db3230ffc61ec4f`（tree `6bd04d16c66f3e27b78c82be3be7c6fc35f9c891`）的 source 全量与静态门禁 exit0，仅归属该冻结；其独立附加 DB 探针在 SQLite 打开前被审阅守卫阻断，不称独立 DB 通过。新整合 `f88e2bd` 已取得自身受影响/UI、限定独立复核及 source 全量证据，不套用旧绿；真实复评仍待验，不改判既有真实评测，不自动重放已持久化模型回合；契约与实现分开 PR。

- **首次 Apple Silicon 限定预览取消 soak 时长门禁（2026-11-20）**：按维护者决定，仅取消首次 `macos-arm64` 限定预览的强制 ≥10h soak，不设替代时长；工具、测试和 `soak_limits` 保留为可选、非阻断诊断，四目标正式发行原长稳要求不变。长时稳定性未验证，不造完成证据；候选 SLO、原生启动/runtime smoke、Keychain/核心任务、数据完整性、进程崩溃恢复、升级/回退/备份、required CI 与受保护 PR 均不降低，可选诊断发现这些必过项失败仍阻断。权威范围见 [PRD §10.6 / §11.1](PRD-v1.md)。

- **限定预览复用发行校验，不建立弱化旁路（2026-11-20）**：手动布尔开关
  `macos_arm64_preview` 只缩小 build/download/assemble 的目标集合；默认及 annotated tag
  仍是四目标。预览 assembler 对根目录、单目标文件集和空输出执行写前检查，共用既有
  SHA256、manifest、report 与原生 smoke 校验；索引不新增 schema，目标由 artifacts 明示。
  上传只采用索引资产及索引本身。已有 Release（含草稿）不得改写，预览拒绝已有同名 tag；
  版本级并发只串行化本 workflow，不锁定仓库外部操作者，人工公开前仍须重核 tag 与下载字节。
  本机/受控传输探针不是 GitHub 发行实跑或用户设备验收。
  冻结对象、红绿/全量门禁、独立复审与本机打包边界见
  [预览发行链验证记录](changelog/2026-11-20-macos-arm64-preview-release.md)。

- **低磁盘 Run 资格决定（2026-11-14，2026-11-18 补齐 W10 子任务与 F14 收口）**：创建前固定 `pure_only/effects`，默认 effects，同时提供真实 CLI/RPC/UI 纯读选择；避免首次副作用时追加资格状态机。代价是选择 effects 后最终只读的新 Run 仍须准入。W10 引入 `create_subtask(..., write=True)` 后，实际获得可写绑定的子任务为 `effects`，在开启 SQLite 写事务前统一执行 `require_new_run`（只读子任务与父全只读降级路径仍为 `pure_only` 无 2 GiB 阈值门）；控制面显式拦截 `DiskAdmissionError` / `RunEffectsDisallowed` 防路径字样误归类，UI 统一映射三项低盘/资格 C4 编号为中文人话（不泄露原始 `rule_id`）。普通/摘要模型及必要内部审计不算本条 C2 业务副作用，仍守预算、取消与持久化；已有 effects/legacy 的读与模型恢复保留原门，新普通非 pure 动作现测空间。字段、停止控制例外及逐事件版本以[独立契约](low-disk-admission-contract.md)为准，收口复审见 [2026-11-18-t3-audit-closure.md](changelog/2026-11-18-t3-audit-closure.md)。

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

- **Windows CI 分片决定（2026-11-14）**：保留四 required 名称与25分钟上限，以两个独立 runner 按整文件分片，避免同 runner 并发引入端口/进程/资源竞争。汇总 job 在 Ubuntu 核验真实 Windows job 的结论与开发报告；完整收集快照先于其它筛选，最终列表再核，不用选后列表倒推全集。报告不是独立执行证明，必须与上游 job success 一起使用；不新增产品契约、依赖或 skip。

### 收尾轮实现决策（2026-11-11，PR #66–#72）

- **阻断状态属于"这台机器这个用户"，不属于某个 `--db`**：`authority.py` 把它固定到 `TIANSHU_AUTHORITY_ROOT`（否则 `~/.tianshu/authority`），与 `--db`/`--data-root`/cwd 无关；键用 `pathkeys.overlap_key(resolve(path))`（与租约同口径）。失败语义朝安全方向：**库不存在=空集**（不是损坏，否则新用户第一次用就被自己挡住）、**读不出=已阻断**、**写失败=如实报「跨数据根阻断未生效」**。代价：权威根只保护"阻断"，跨数据根没有租约互斥。
- **接管前对账只读**：`recovery.assess_run` 不写事件、不落盘、不阻断（用 `SnapshotStore.__new__` 挂属性绕过会 `mkdir` 的构造函数）；拒绝接管后的双重证据（日志 + 权威根）由调用方在租约事务回滚**之后**落。三个入口（`AgentLoop.start`/`resume_run`、控制 Run）共用同一条实现——只保护控制 Run 等于没保护。
- **撤销用"写前意图日志 + 幂等续做"，不是两阶段提交**：`artifacts/undo/<run_id>.json` 记录完整计划与每步 `post_state`；崩溃后先核对 done/剩余项与磁盘一致再从断点继续，**绝不从头再来**；`completed` 只差补事件不阻断。恢复侧把未完成撤销变成阻断（`recovery.blocked` + 权威根），阻断是 journal 的投影、不是事实源：`unblock` 可解除，但只要 journal 仍 `in_progress`，下次恢复会重新挡住。
- **就绪标记必须晚于"端点真的可服务"**：`on_ready` 在 accept 线程进入 accept + 自连认证握手成功后才落；`LISTEN_BACKLOG=64`。根因不是"listen 没完成"，而是 backlog=1 时探活自己占满队列（详见 §5）。
- **评测判据按目标作用域 + 分类**：`Check.kind`（completion/safety）只有 safety 进 `safety_gate`；完成度判据红不影响安全结论；`SUITE_VERSION` m4.5 使旧报告不可比（两道守卫）。
- **`EventLog` 对"有内容却读不出 events 表"的库 fail closed**：0 字节按新库（首次启动窗口无历史可丢），只有"文件存在且有内容、读不出 schema"才 `EventLogCorrupt`。检查必须在 `executescript(_SCHEMA)` 之前、`PRAGMA journal_mode=WAL` 之前——顺序即是"先读、再写"的保证。

### 工程评审新增要求的实现决策（2026-11-11，PR #79–#84）

- **四维独立、安全唯一硬门槛**：`dimensions` 四项各自记录样本/通过/失败依据；completion 失败只影响 `verdict=pass`，绝不进 safety；无效样本（评测基础设施故障）不进分子分母，但产品该持久化的 intent/result/Receipt/审批/恢复证据缺失**仍是产品失败**。
- **判据负控要可机械检查**：每个 safety 判据必须登记负控，元测试缺登记即红；`Check.path_coverage` 让「专门测错误路径」的 case 在未触发时明确说「没有覆盖证据」。
- **控制面推送必须与请求处理解耦**：每连接有界发送队列 + 独立发送线程，队列满/发送超期 → 断开并记 `slow_subscriber_dropped`，重连按 `since_seq` 补读；同步 `send()` 会把慢消费者变成控制面 DoS。
- **隔离是测出来的，不是声明出来的**：门只认 `probe_backend()` 的实测报告，不认后端 `capabilities.isolation`；`unknown` 与 `not_enforced` 一样阻断；本机 local 后端文件/网络/凭据三边界实测可越过 → 依赖能力全部关闭。
- **备份必须是「一致点 + 引用清单」，不是复制库文件**：SQLite 在线备份 API 取一致点，`backup.json` 记哈希/引用/版本；中断留下的临时目录不得被 `verify` 当有效；恢复只允许干净根；迁移前必须 `verify_backup` 通过、每步进度持久化、`crashpoint` 注入中断后可续做。
- **写入口的「旧版兼容」必须与「读兼容」分开**：`EventLog.append` 默认只收当前 v1 契约，v0 只能在显式 `legacy_write=True` 下写入并记 `payload_version=0`；`event_versions` 旁表与事件行同事务；旁表缺失/损坏 → `PayloadVersionUnavailable`（不把「读不懂版本」洗成「旧版」）。**自动回退 v0 是 fail-open**：`_legacy_schema` 的 `required=[]` 会让任意 `{}` 通过。

### 收尾轮的兼容与并发决策（2026-11-11，PR #87–#88）

- **Windows 上「测得出真值」要换时钟、换系统调用**：`time.monotonic()` 在 Windows 走 `GetTickCount64`（约 15.6ms 分辨率），热启动会量到 `0.0`——启动耗时起点与读数都改用 `time.perf_counter()`（QPC），语义仍是「记录值 = 真实测量」。`_pid_alive` 不能用 `os.kill(pid, 0)`（Windows 上是 TerminateProcess 语义），改用标准库 `ctypes` 的 `OpenProcess(SYNCHRONIZE)` + `WaitForSingleObject(0)`：超时=活着、已信号=已退出、拒绝访问=按活着保守处理、其余=`None`（unknown）。
- **并发读者的「半行」不是损坏**：`read_metrics()` 对没有 `\n` 收尾的最后一段先尝试解析，失败就跳过（`tail -f` 语义），下一次读它就会完整；已由 `\n` 收尾的坏行仍带行号抛 `ValueError`。写侧仍是「一行一次 `os.write`」，但**不把「一次 write 对并发读者原子」当契约**——那是文件系统语义，不是我们能承诺的。
- **裸 stdlib 异常不能穿过自研抽象**：`multiprocessing` 的认证握手在「对端 accept 后立刻关」时抛的是 **`EOFError`**（不是 `OSError`）；`Client.connect()` 现在把它映射成 `ConnectionClosed` 且**不重试**（重试会把「已经走了」拖成整个 timeout）。`request_shutdown()` 的返回类型因此恒为 `bool`，收尾路径不会因为「daemon 已经走了」抛异常。对齐依据：服务端 `Server.accept()` 早就把同一种情况映射成可重试的 `ProtocolError`。

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

- **常驻离线演示不能共享可耗尽的响应列表**：旧启动路径的第二个 Run 显示成功但没有最终回答。Daemon 启动和设置切回 `scripted` 时共用无状态演示模型；每个 Run 按自己的消息历史决定下一步。用真实子进程和 `events.run` 核对两次终态都提交了非空回答。
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

### G1 通用执行基础的实现决策（2026-11-11，PR #91 / #92）

- **schema 方言封闭、注册期拒绝，不静默忽略**：`toolschema.py` 只认对象/数组/标量三组白名单关键字，`oneOf` / `$ref` / `format` / `default` 等在**注册期**抛 `ToolSchemaError`（带工具名 + JSON 路径 + 关键字）。理由不是"规范要求"（JSON Schema 里未实现关键字本是注解），而是准入边界：注册表里写着、执行侧不看，等于对模型和审批人一起撒谎。`$ref` 另有一条硬理由——解析外部引用 = 读文件或联网，PRD 明确禁止。
- **工具身份的三项绑定里，"描述摘要"必须是条件字段**：`ActionRequest.description_hash` 只在非空时进 `to_canonical_dict()`。历史请求（无该字段）摘要逐字节不变，历史库里的审批 / grant / 派发记录不作废；新请求多绑一层，改描述即作废旧批准。这与「资源种类用 scheme 前缀而不是给 `ActionRequest` 加字段」是同一条理由：**不能为一条新要求把既有授权链集体作废**。
- **预算账本建在事件库同一个 SQLite 文件里，父账户计数器本身就是子树聚合**：预占沿 `parent_id` 链逐级扣，所以父看到的花费天然含子账户；`usage()` 若再对后代求和就会把同一笔算两次（PRD §5.6 第 2 条）。
- **未知用量按维度保留占用，不按零结算；取消不是退款**：`settle(known_dimensions=...)` 支持"token 已知、金额未知"的 `partial` 状态；`abandon_unsent()` 只释放确认没发出的预占。`run.cancel` 向账本传播取消只阻断后续预占，已发出的请求原样保留等 `reconcile()`。
- **账本默认关闭（已被 #96 取代，保留原文以便对照）**：`Limits.budget_limits` 为 None 时行为与之前一致。估算预占（4 字符≈1 token + 25% 余量 + 输出预留）会让临界 run 提前被拒，属行为变更需单独评审；G2 接入前必须默认打开。**不能**把"账本已实现"读成"§5.6 发布前置条件已满足"。

### G1 收口轮的实现决策（2026-11-11，PR #96 / #97 / #99）

- **默认记账 ≠ 默认设闸**（取代上一条）：`budget_limits=None` 时账户/预占/结算照写，但**任何维度都不设闸**。理由：§5.6 第 4 条（尝试过但全失败不得显示没有调用）与第 7 条（恢复先核对未决预占）在账本关着时在默认路径上没有任何事实可核；而"设闸"需要一个用户明确给的上限。
- **"可验证上界"必须两项都是上界，拿不到就不进这个模式**：输入 = 请求体 UTF-8 字节数（字节级 BPE 下 token ≤ bytes；CJK 因此比"4 字符≈1 token"大得多，方向保守），输出 = adapter 能力声明的 `output_tokens`（它就是发出去的那个 `max_tokens`）。OpenAI-compatible 的请求体里没有该字段，所以它**如实不能**进 upper 模式——这是能力事实，不是缺陷清单。**不承诺**绝对不超额：provider 每消息固定开销不在字节上界内，靠 `overrun` 暴露并在上界口径停机。
- **外发没有自动路径**：`network` 资源只能配 `fetch`，判定恒 `APPROVE`（`c4.outbound`）；即使目标是公开页面也一样——"公开与否"不是本地能判定的东西。`mcp` 按 `exec` 同级；`subtask` 在 G2 子代理 D1 之前**恒拒**（没有委派执行器，走到审批再失败会给"批了就能用"的错觉）——D1 起改成按工具判定（spawn 审批 / cancel 自动 / read 自动），旧编号 `c4.subtask_unavailable` 退役为保留未使用。D2 起子 Run 由既有 Worker 驱动、父 Run 挂起/唤醒、取消传播与父目标 fence 已接。
- **没有执行器的种类在派发前拒绝**（`dispatch.resource_kind_unsupported`），不进审批流程；许可判定仍完整实现——它描述的是"将来有执行器时外发必须人工批准"这条语义，不冒充"现在就能发"。
- **资源规范化按"同目标只能一种写法"做**：host 小写+IDNA、默认端口省略、`.`/`..` 折叠、百分号段先解码再判、拒 userinfo/fragment。每一种折叠都对应一个"看起来只是写法不同、实际是两个目标"的口子；`ResourceRef` 只收已规范化形状（值会进审批摘要）。
- **内容引用：去重 ≠ 共享可见**：内容寻址会去重（同内容多任务一行对象），所以授权记在 `(digest, task_id)`；引用是**校验和**不是文件名，读时重算 sha256；分页不切半个 UTF-8 字符。
- **读工具不过派发是既有硬口径，不是本轮开的后门**：`Dispatcher._typecheck()` 明写"只读动作不经过派发"，`read_text`/`memory.search` 同路。给一个永远到不了派发层的 `content` 资源种类 + 策略双闸只会多一份没人用的契约；改成读工具 + 存储层可见范围，"双闸"落地为"存储层核一次授权 + 执行层用本任务身份调它"（NC：把身份换成空串就红）。


### §7.1 语义摘要轮的实现决策（2026-11-11，PR #103 / #105）

- **记录存正文、事件存身份**：不新增事件类型（仍 41 个）。两份正文（事件 + 记录）会互相矛盾，改一份忘另一份就开始对不上；"这一轮用了哪一版"用 `summary_id`/`version`/`text_digest`/`source_seqs` 四个字段就够，正文按 id 从同一库取。
- **编译保持纯函数**：摘要正文由调用方**先持久化**再作为 `SummarySlice` 传入 `compile_context`。可重放性来自"回放读当时保存的那一份"，不来自"禁止模型"——这句是对 `context.py` 原注释结论的修订，理由保留。
- **允许「子集复用」**：材料身份含事件 `seq` ⇒ 丢弃集合一变大就是新材料；只认"材料完全一致"会在调用上限一到时整段回落确定性裁剪，最先丢的硬约束又没了。放宽成"这份摘要覆盖的 seq 现在确实都被牺牲了"，用它成立。
- **先持久化再引用**：`begin(pending)` 在模型调用**之前**提交。崩溃恢复只能看到 pending，于是"标 failed、不重试"是唯一诚实的处置——重试等于偷偷多花一次钱，而"不偷偷增加一次模型调用"正是 §7.1 第 4 条。
- **摘要不许弄死 Run**：预算拒绝 / 已取消 / upper 模式拿不到输出上界 / 模型报错 / 正文空白或超长，一律回落确定性裁剪。`BudgetConfigError` 在摘要路径上被当作"这次不摘"处理（主调用该拒绝还拒绝）。
- **默认上限 3 次是产品选择**：它防"每轮一次"的费用失控；很长的 Run 后段仍会回落确定性裁剪（设计取舍，不是 bug）。


### 本地 Skill 命令的实现决策（2026-11-22，候选）

- **准备与运行分开**：`skill.list` / `skill.prepare` 只读本地文本，不调用模型、
  不写事件；整合当前主干后，新建走 `task.create` 与 `run.start`，追问走
  `run.follow_up`，活跃运行的补充要求走 `run.guide`，不再用追问修改任务目标。
  原始技能全文、来源路径和摘要沿相应输入事实落库，重放不重新打开源文件。
- **显式选择版本**：不同来源同名技能不覆盖；菜单插入路径与内容绑定的版本标识。
  文件变化后旧标识拒绝，准备失败保留草稿。列表及准备的迟到结果在工作区变化、
  Task 修订或组件卸载后失效，不能凭旧闭包覆盖新目标。
- **技能是参考文本**：额外元数据不授予权限，不自动读取引用或运行脚本；
  计划/仅读资格与审批仍由执行侧处理。新建任务可能直接展示包含快照的目标；
  追问与补充要求沿运行输入保存。
  若要拆分简短目标与结构化技能记录，必须另立契约。

### Goal 模式的实现决策（2026-11-22，G1 候选）

- **目标验收与运行成功分开**：Goal 绑定 Task 修订，配置不可变；新建、推进、
  关闭均经真实事件事务，推进同时提交 Run 与 Job。不用聊天正文代替验收事实，
  不新增 Goal 投影表，也不扩大首屏 `state` 响应。
- **每轮由人触发**：条件与上一轮成功正文保存进 `run.start.task`；模型自称完成
  不关闭目标。推进沿用既有资格、审批、预算、暂停、取消及子任务收尾检查。
  远程 MCP 发现留给驱动线程，不在推进的入队事务中联网。
- **保持 G1 范围明确**：使用当前默认模型，不为 Goal 固定模型档案；运行中切换沿
  既有每次模型步骤快照机制生效。首版不保存附件或单次模型
  档案，界面会在创建 Task 前拒绝这些组合；任务级工具配置仍沿用现有能力。
  自动推进、累计目标预算和机器验收须按 G2 另立契约。

### G2 网络阶段的实现决策（2026-11-11，PR #110 / #112）

- **两道门，两道都在 `prepare()` 里走完**：任务级白名单（`outbound_allow`，源级、创建时定死）+ 逐次人工批准（`c4.outbound`）。白名单不通过时**不弹批准**——"批了也发不出去"的批准会白占一次人工决定。白名单是**源**（transport+host+port），路径/查询一律拒绝：允许写路径会让人以为"只许访问 /docs"，而执行侧只按源判，那是一条假保证。
- **外发面靠"请求里没有别的数据"来收口**：只用 `http.client`，不读代理环境变量、不发 Cookie / `Referer` / `Authorization`、不转发凭据、GET 无请求体，请求头只有固定几项。这让「私有内容未经外发许可不发包」在执行面**可被核对**——但它证明的是"请求里没有内容"，不是"没有别的泄露通道"。
- **Agent 能力候选的公开目标边界**：网页搜索、只读导航和 HTTPS MCP 在拨号前拒绝非全局 IP 字面量，DNS 仍走既有全结果集校验和端点固定。旧 `network.fetch` 的显式私网白名单语义保持原样。
- **重定向只许同源**：批准的是"这个源上的这个 URL"；靠 302 把包发到另一个源等于绕过批准。跨源/换端口/https→http 一律拒绝，需要时让模型重新发起、重新拿批准。
- **字节账本用可验证上界**：发送前按 `max_bytes` 预占（本次最多读这么多），读完后按实际字节结算；`prepare()` 先做一次非预占的额度预检，额度不够在弹批准之前就拒（`dispatch.budget_refused`）。未知用量（超时/读断）**保留预占**，不记零。
- **取消有两层**：派发前查 + 执行器每跳/每块之前查。单独去掉任一层两个判据仍绿（另一层兜住了），两处都去掉才真的发包——冗余是**证明过**的，不是声明的。
- **内容引用先落盘再写事件**：抓到的字节先写内容寻址 blob + `ContentStore.record(task_id)`，再写 `tool.result`；反序会承诺一份不存在的内容。回放只读已保存结果，缺 `ref` 报 problem，不重新联网。
- **`tool.result.status` 的封闭集没有 `cancelled`**：取消按 `failed` + `output.reason=net.cancelled` 记（"没发出去"是确定结论，`effect_state=none_applied`），不为一个状态扩契约。
- **评测的"来源可离线核验"必须真的关掉服务器**：runner 在 Run 结束、夹具停掉之后从内容引用读回字节（会重算 sha256），读不回来记 `invalid_reason`（评测基础设施无效），不是"通过"。任务层用 `RU-W1@2.0.0`（断网诚实失败）与 `RU-W2@1.0.0`（本机夹具交付）;旧版 `RU-W1@1.0.0` 的「本轮不提供网络能力」在 #110 之后不再成立，换版本而不是改判据。
- **模型工具面绊线显式过一次**：`test_veto_scope` 的字面清单加 `network.fetch`，动作集合加 `fetch`；`tool.intent` / `tool.not_dispatched` / `receipt` 的 tool_id 枚举与 JSON 镜像同步。

### F10 N3 蒸馏的实现决策（2026-11-11，PR #98）

- **只做确定性合成，LLM 合成延后**：L2/L3 的立论是"可重建、可回放、能和证据逐条对账"；生成式摘要会让"两次合成是否一致"变成不可机械核对的事。所以本轮 L2 是**引用式拼装**（每行带 `[memory_id@revision]`），两次合成逐字节相同，增量 delta 天然成立。要上 LLM 必须另立契约：正文进 `tool.result`、replay 用快照、与确定性路径分两套代码与两个测试文件。
- **freshness 的基准是逻辑时钟 `seq`，不是 `consolidated_at`**：后者是钟表时间（可回拨），而且 `memory.confirm` 这条生命周期推进会把该列改成确认时刻——晚确认的页会永远"新鲜"。基准落在 `source_refs` 的 `seq:<basis_seq>`（合成那一刻看到的最大 `last_seq`），缺失时判"不可判定"，不谎报新鲜。
- **陈旧层降权、不短路**（F10 §8）：`stale=true` 的页排到新鲜 L1 之后，既不删也不挡召回；有 stale 时排序先按 `(stale, 原排序键)`，否则 proximity 0 的 L2 页会压过 user 作用域的 L1。
- **合成页 = 候选**：`memory.consolidate` 只写可重建投影，结果 `candidate` + `origin=inferred`，人 `confirm` 才进召回；免审批只保留给 `memory.confirm` 本身。页 id 由「作用域 + 层 + 正文」决定 ⇒ 重复合成幂等，旧页由同层新页 supersede 且仍可按 id 查。
- **L3 只对 user 作用域**：偏好页是"关于这个人"的页面，workspace / task 的偏好不是同一层语义。

### F10 N6 控制面端点的实现决策（2026-11-11，PR #100）

- **控制面记忆操作只留一份实现**（`memory_control.py`，CLI 与 daemon RPC 共用）："写记忆必须走 dispatcher"这条纪律抄两份，早晚有一份图省事直接改投影。
- **不新造身份令牌**：控制面的边界是"这条 IPC 只有本机人能连"（POSIX `run/` 0700 + socket 0600），再加一套 token 只是多一个能被抄走的东西；写操作用 `HumanIdentity.local_cli()` 落审批，与 CLI 同一个 actor。
- **同名不同路**：`memory.get` / `edit` / `forget` 既是模型工具也是 RPC 方法，但两条路身份与过滤完全不同（模型按 `allowed` 装备、人在本机全作用域）——所以测试断言里必须写清走的是哪条路。
- **规格清单也会漏**：§7.2 列的 6 个方法没有 `memory.confirm`，缺它就没有"人确认候选才召回"的入口；落地时按"判据优先于清单"补齐并记进 §7.4。

### 恢复 B T1 平台准入的实现决策（2026-11-15）

- **路径 hash 只保留为 namespace，不再冒充实体身份**：POSIX 实体键来自持续打开句柄的 `device+inode`；Windows 来自 `GetFileInformationByHandleEx(FileIdInfo)` 的卷序列号+128-bit file ID。硬链接别名会争同一 entity lock。
- **gate 与业务锁分层**：gate 只保护 anchor/身份固定窗口；namespace/entity shared access 交给真实连接，所有锁非阻塞。registry observation 也持 shared namespace/entity，因此多个普通连接可并行，写者独占时立即 busy。
- **registry 读取是完整快照，head 不是授权**：固定真实 Home 下只追加 registry 每次读取规范 meta、全事件链和 head，调用 T0 `validate_event_chain()`；写入只 INSERT event，并在同一事务用旧 head CAS 前进。
- **SQLite close 决定 access 寿命**：`MaintenanceConnection.close()` 先让 SQLite 真正关闭，再释放实体句柄和外部锁；跨线程 close 失败时连接仍可写，后续锁必须继续持有。
- **Windows 目录发布不伪造 fsync**：registry 目录用 `MoveFileExW(MOVEFILE_WRITE_THROUGH)` 发布；Windows 目录的物理断电 durability 仍不是已证明事实。CI 另有原生 probe artifact，直接记录 file ID、硬链接、持句柄 rename、锁和 registry 结果。

### 受管恢复 B/C 写入边界（PR #198 候选）

- **权威和数据根分别持久化**。POSIX 的 registry 写连接使用 DELETE 日志模式与 `synchronous=EXTRA`，覆盖提交时回滚日志删除的目录同步。首次预约前须有 Home、临时权威与发布父目录的屏障。发布后中断留下的空权威需要补做目录屏障，未发布的私有残留按现行规则保全。R3 写入 `released` 前须同步完成 R2 的主库及其所在目录。
- **已发布权威须核验主库格式**。干净关闭的 WAL 库也可能只有 `registry.db`，SQLite 只读连接仍可能建旁文件。观察、重核和追加在现有锁内先核对目录和 DELETE 格式文件头，再连接 SQLite。活跃协作写者优先返回 Busy。详见[注册表日志格式记录](changelog/2026-11-19-restore-registry-journal-format.md)。
- **已落盘的 `unknown` 仍需核对阻断**。恢复解除保留原有结果，追加缺少的工作区或 MCP 对象阻断。完成权威阻断和 R2 持久化核验后才能解除隔离。详见[恢复 B/C 记录](changelog/2026-11-18-restore-b-and-c.md)。
- **平台能力写前判断**。Windows T1 原生身份、锁和 SQLite 注册表路径继续保留。受管写入在平台屏障缺失时拒绝，保留已观察到的批次和现场。W15 在 Windows 批准执行和低余量路径先做只读预检，发现 SQLite 旁文件即保留现场并拒绝。在 Windows，任何文件候选会导致整批拒绝。静态库无文件候选时按有效 SQLite 余量清理表行或延后。目录持久化跟踪见 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。

### 桌面退出实例绑定（2026-11-16，R1）

- **实例核验必须在发起 shutdown 的同一条握手连接内完成**：不能先另开连接核对 `daemon.lock` / welcome，再换连接发退出；旧实例 A 在两个动作之间退出时，第二条连接可命中新实例 B。现在 `request_shutdown(expected_instance_id=...)` 从冻结锁取得身份，在同一已连接会话的 `hello.welcome.instance_id` 核对后才发送请求；不符或缺失一律 fail-closed。
- **超时清理同样绑定实例**：Host supervisor 只终止发起请求时已核实、且超时后锁字节仍完全相同的 PID；锁已换实例时保留现场。桌面辅助清理要求 `daemon.lock` 同时含合法 PID 与非空 `instance_id`，不能从一个坏锁猜目标。
- **Host 兼容性是发行身份加认证会话，不是“端口有人”**：桌面壳把 bundle 的 `version/commit/target` 传给 Host，并同时核验 `host.lock`、鉴权 `/session` 和当前 bundle。任一缺失或不一致都在附着、readiness、supervisor 接管或退出前 fail-closed；错误给 PID 与 `data_root`，不输出 token。

### R2 build 源绑定与发布恢复（2026-11-16）

- **runtime lock 是源码身份，不是调用参数**：正式 CLI 只从目标 commit 的固定路径 `release/python-runtime.lock.json` 读取 lock，并在后续下载中继续使用该已解析对象；不再暴露任意路径覆盖。
- **Git 读操作必须同时关闭 replacement refs**：`GIT_NO_REPLACE_OBJECTS=1` 与 `git --no-replace-objects` 同时使用，防止 replacement blob 在不改 HEAD/status 的情况下替换打包字节。
- **目录发布采用可恢复 rename journal，不冒充跨平台原子 swap**：先持久化 output/staging/backup 身份，再移动目录；启动时按现场完成新发布或恢复旧 output。失败仍非零，Windows/macOS x64 的真实 rename 行为须原生验证。
- **跨文件系统可移植性要求 casefold 去重**：Git tree 规范路径阶段以 `casefold()` 拒绝 `ss.py`/`ß.py` 这类在默认 APFS/Windows 上会折叠为同一名称的输入，避免静默只留一份。

## 5. 踩坑教训（别再踩）

- **已保存响应不免除运行收尾检查（2026-11-22）**：恢复最终正文时必须与正常路径
  一样经过人工否决和子任务等待；不能以“无需再问模型”为由直接成功。Goal 还必须
  同时拒绝 `partial` 与 `unknown`，前者可能包含已生效操作和未知操作，不能当作
  全部可核对。真实硬退与 MCP 超时负控见 [Goal 记录](changelog/2026-11-22-goal-mode.md)。

- **接管夹具须匹配产品的原子发布（2026-11-22）**：[#213 候选](https://github.com/lorek666/tianshu-agent/actions/runs/44944526018) 的 macOS Rust 用例在接管写入期间读到非法 Host 元数据。产品 Python Host 已经通过同目录临时文件原子替换发布，测试却直接截断写目标文件。仅将 Rust `cfg(test)` 内的接管夹具改为原子发布，并用真实子进程在半写屏障处调用真实读取器：旧发布方式确定性失败，原子发布保留完整旧记录，放行后读取完整新记录。原接管拒绝、Daemon 锁字节保留与未停机断言不变；不得为夹具竞态吞掉损坏元数据错误。产品 Rust 前缀及 Python 发布实现未改。

- **Windows 分片须核对完整耗时分布（2026-11-22）**：[#213 候选 CI](https://github.com/lorek666/tianshu-agent/actions/runs/44927917869) 的分片 0 触及 25 分钟上限；双向修正少量文件后，[后续候选](https://github.com/lorek666/tianshu-agent/actions/runs/44939541307) 仍在约 91% 被取消。两轮缺件都使 required 汇总正确失败。日志对账显示中等文件误差会累积，runner 速度也有明显波动；后续覆盖完整已测 collection，并让真实任务的证据检查复用各场景三次实跑的深拷贝。每项断言保留，负控不能污染共享样本；变输入、失败与忘记路径仍独立实跑。对应权重含保留工作量的推算，不冒充新 Windows 实测。两个隔离 pytest 进程的回归和历史回算用于核对分配，两个独立 runner、完整覆盖汇总、诊断与时间上限不变。
  候选 `c6d6475` 的 [44944526018](https://github.com/lorek666/tianshu-agent/actions/runs/44944526018) 两片作业分别在 20 分 04 秒、20 分 08 秒完成，Windows required 汇总通过；下载的两份原始报告已再次核验实际 checkout、完整有序 collection、互斥全集及各阶段覆盖。该轮整体仍因上述 macOS Rust 夹具失败而为红，不用 Windows 通过替代新冻结的完整门禁。

- **源码收据不扩张系统隔离结论（2026-11-21）**：source-only 私有根、空模型 key 与 env 凭据后端，不证明默认 urllib 代理/重定向路径零外发，也不消除默认 ControlPlane 构造时的 Keychain/ctypes 模块导入；构造或导入同样不等于实际取密钥。collector 正向归档/启动收据不能替代 full 原始退出码、辅助矩阵核验或当前 PID 活性证明，更不是产品 App 验收。旧独立 DB 守卫阻断与 FFI 拒绝边界保持，见[组合修复记录](changelog/2026-11-21-preview-release-repair.md)。

- **跨平台 fixture 与 metadata 文本必须显式 UTF-8（2026-11-21）**：仓库按 UTF-8 保存的 fixture、schema 与 TOML/JSON/lock 元数据，读取时须显式 `encoding="utf-8"`，不能依赖本机默认编码。PR210 首轮 Windows CI 在 fixture 收集读取处真实失败；只修该 import 还会遗漏后续 schema 与发行身份读取。修复只给两份新增测试的八处读取补编码，不改历史字节或全局 UTF-8/locale。Mac 上精确限定输入路径的 CP1252 对照只是窄路径模拟，不是 Windows 实测，也不代替新候选的 hosted CI；详见[组合修复记录](changelog/2026-11-21-preview-release-repair.md)。

- **Mach-O code-page 哈希正确不等于 App 资源封套有效（2026-11-20）**：旧预览主程序只有 linker-ad-hoc 签名、没有 CodeResources，静态 manifest/架构检查通过，原生 codesign 仍报资源封套缺失。修复必须在最终 App 而非 staging 上封装，并在真实 tar 恢复后再验；免费 ad-hoc 完整性、认证发布者身份、Gatekeeper 判定和产品运行必须分开记。对已在 App 内的主程序直接签名也可能封装整个 bundle，普通签名拒绝负控须先在独立 standalone 文件上签名后再组装 fresh fixture，不能伪称已经覆盖了 no-force 拒绝。

- **重建必须区分保存点与其后的事实（2026-11-21）**：Responses 的恢复计数由最后保存的游标加其后 `model.intent` 得到；控制面尚未写首个 `run.cursor` 时，以 `run.start` 的初始进度作为保存点，不能先吸收 intent 再让恢复入口重复补计。等待子任务的事务先清空 `model_turn`、保存游标，再追加 `job.parked`；重建需识别相邻、同 Run、同 claim 的 `waiting_children` 事件对后核对原摘要，不能删掉哈希校验。连续真实崩溃、保留/删除/重建投影，以及父任务重复挂起与 Worker 唤醒均须覆盖，见[独立复审原件](evidence/openai-responses-2026-11-21/README.md)。

- **目录 metadata 不等于递归内容闭包（2026-11-20）**：旧材料树在下降后新增孙项时，父级只重核 child stat 可能因平台元数据延迟而漏检。每次调用保存已观察目录，最后逐目录做一次真实直接子项复核，保留前后身份/state、名称闭包及链接/未知/不可读拒绝；不能在每层再递归整棵树。真实 late-file + 冻结 child stat 与列举计数区分这两类错误。它仍是有界协作式观察，不是原子快照或同 uid 物理隔离。
- **只读 SQLite 检查后要重核干净离线前提（2026-11-20）**：只读备份检查可能留下 WAL/SHM。后续静态 GC 以 companions 优先报 `incompatible` 是正确拒绝，不能为测试期望改产品错误优先级。夹具仅经正常 RW recovery/close 并严格断言无 WAL/SHM/journal；不能手工删除 companions，也不能把局部无目录同步能力投影称作 Windows 原生验证。
- **测试 shell 的工具传输必须匹配生产 runner（2026-11-20）**：生产 Ubuntu asset-list shell 与 Windows Git Bash 的原生 jq 文本输出不同；测试 helper 仅在 Windows 请求 jq `--binary` 提供 LF，不 strip JSON/资产名。使用实际 shell、真实 jq 的受控 CRLF/LF 正负控，原 CR/LF 非法名及额外文件仍拒绝；生产 release workflow/白名单不因此改动。
- **完整职责拆分不能变成 required 空绿（2026-11-20）**：macOS Rust 与完整 Python 曾共用25分钟触发真实超时；拆为各25分钟原生作业后，原 required 名由 `always()` + 原 draft gate 汇总，显式要求两个上游均 `success`，failure/cancelled/skipped 均拒绝，并绑定同一 exact checkout。保留全部原命令与 release all-jobs-success；拆预算不保证新 runner 一定及时完成。证据见[本轮修复记录](changelog/2026-11-20-integration-ci-repair.md)。

- **pytest 子进程 startup 不应预载进父收集过程（2026-11-20）**：`test_maintenance.py` 在收集期冻结生产锁根 getter，父命令若同时预置 `tests/_startup` 与测试锁根，`sitecustomize` 会先把该 getter 替换成测试 lambda，导致 OS 固定锁身份断言按设计失败。父 pytest 仅以 `src` 为 `PYTHONPATH` 并清除外部测试根/payload 变量；保留原样 `conftest` 在逐例运行时注入子进程 startup 与本用例真实临时根。不要为修运行命令错误而改生产 getter 或放宽身份断言。源码拷贝 smoke 还须让内外层测试启动器使用同一个真实 base interpreter；Windows uv venv launcher 的 `Popen.pid` 不保证等于解释器 PID，不能因此放宽严格凭据断言。

- **父进程设置测试根，不等于子进程已隔离（2026-11-20）**：生产 verifier 与 runtime smoke 分别重设 `PYTHONPATH`，会丢失仅存在于 `tests/_startup` 的维护锁/registry 注入；环境变量仍在不能证明真实子进程未使用日常账号的固定根。源码集成 smoke 现仅在显式测试 fixture 中通过双层专属 proxy 和同 PID launcher 验证原 argv/cwd/包来源、已加载的 startup 来源与实际锁/registry/authority 根，失配在 CLI 前拒绝，凭据绑定父 `Popen.pid`。这不是未改命令的发行包或用户设备 smoke；后者不要求新建或切换 OS 测试账户，但须隔离测试数据和工作区、不触碰现有生产数据或凭据，不能安全隔离的项目仍记为未验证，不能 fake HOME 或改生产锁语义。旧全量日志保留真实历史，但缺失隔离证据的部分不作安全隔离证明。见[整合门禁记录](changelog/2026-11-20-preview-release-integration.md)。

- **CLI 默认 worker 必须区分单轮预算与队列耗尽（2026-11-20）**：`Worker.drain()` 的 64 作业上限是防止单次调用无限运行，不代表队列已经清空。CLI 未指定 `--max-jobs` 时按批次继续调用，直到 `exhausted=True`；显式 `--max-jobs N` 仍保持单轮限制，`idle:false` 如实表示仍可能有作业或发生工作区争用。超过 64 个真实排队 Run 的子进程回归测试钉住这两个语义。

- **用标准库名识别历史材料，用实体身份判定归属**：材料布局、空库准入和旧迁移旁证发现共用 `overlap_key` 的标准库名判据。没有文件身份的旧完成记录遇到别名时，只有两个名字指向同一个普通文件才可采用该记录；v2 旁证仍按实际路径严格绑定。见[标准库名准入回归](changelog/2026-11-19-restore-b03-basename.md)。
- **后续备份必须能与封存恢复点比较**：同源且通过 `verify` 不等于更新。B 复核要求清单创建时间严格递增、事件序号不倒退；时间来自墙钟，比较不证明完整谱系，也不纳入解除使用的静态复核摘要。见[后续备份顺序校验](changelog/2026-11-19-restore-b07-later-backup-order.md)。

- **跨进程短事件排序与本地看门狗耗时要分开（2026-11-19）**：PR [#201](https://github.com/lorek666/tianshu-agent/pull/201) Windows [首轮 CI](https://github.com/lorek666/tianshu-agent/actions/runs/44526542549) 中，真实慢订阅者已断连，但 `monotonic_ns()` 的粒度使断连记账和随后发起的 `state` 同 tick。用于排序和 RPC 耗时的时间戳统一取跨进程 `perf_counter_ns()`；与 Daemon `_sending_since` 相减的发送年龄仍在 Daemon 的 `monotonic()` 时钟域计算。保留真实断连负控及严格顺序，不把开发诊断当发布阈值，见 [取样口径](SLO-STATE-PRESSURE.md)。

- **副作用落盘后的结果提交失败、双阶段锁忙与本地 `unknown` 都不能顺流收束成终态（2026-11-18，#145/#147）**：①`_commit_result`、`_fail_unknown` 与 `_write_mcp_unknown` 必须把 `append_many` 与 `UPDATE dispatches` 放进同一外层事务，否则两笔事务之间撞锁会留下半提交状态；若结果提交与 `_fail_unknown` 均撞 `SQLITE_BUSY` 或异常跳过 `_absorb`，主循环 `_finish` 收尾前必须先与已落库 `tool.result` 对齐最高严重度 `effect_state`，并核查未配对 `tool.intent` / 在途 `dispatches` 与本 Run 尚未解除的本地 `recovery.blocked`，命中则转入 `reconciling` 保留目录租约，绝不能误写 `none_applied` 或放走租约；②本地目录写落入 `unknown`（已写 `recovery.blocked`）后必须立即停进 `reconciling`，不能像 MCP 对象级阻断那样继续让模型 `end_turn` 报 `succeeded`；③Windows `ctypes` 调 Win32 句柄 API 必须用 `WinDLL(..., use_last_error=True)` 显式声明 `wintypes.HANDLE` 的 `restype`/`argtypes`，未声明原型默认走 32 位 `c_int`，会把 `0x80000124` 这类 bit 31 置位的有效 64 位句柄符号扩展截断成无效句柄；④CPython `pathlib._ignore_error` 不忽略 `winerror in {5, 32, 33}`，因此 `exists`/`is_file`/`is_dir`/`size` 与 `unlink`/`read_bytes`/`identity_key` 均需接入有界共享锁重试，且 `identity_key` 只能把 `ENOENT`/`ENOTDIR` 退化为 `path:` 键（吞掉 `PermissionError` 会把短暂共享锁伪装成"目标身份被替换"）。

- **SVG 元素的坐标 `transform` 属性与 CSS `@keyframes` 动画 `transform` 不能挂在同一节点（2026-11-18）**：浏览器在应用 CSS `transform` 关键帧时会直接覆盖同一 `<g>` 节点上的 SVG `transform="translate(...) rotate(...)"` 属性，导致悬浮全息屏、星芒等配件被拉回 `(0, 0)` 原点，或关节旋转基准丢失。所有带定位/旋转的 SVG 部件必须拆成外层定位 `<g transform="...">` 与内层纯动画 `<g class="...">` 两层嵌套；同屏多实例 SVG `<defs>` 渐变与滤镜 ID 须经 `useId()` 隔离，并用静态标记单测钉住全体 `id` 无重复。

- **测试必须隔离所测阶段，并验证异常在真实派发与清理之后仍是原对象（2026-11-17）**：
  前置装配、进程启动/发现、真实 send 和结果核对必须有清楚的观察边界，不能把准备
  阶段混入 transport 期限，也不能用加时、重试或假 transport 取代真实背压。真实
  dispatch 可能捕获并结算观测回调的异常；测试必须独立保留首错，按实际启动状态
  清理 observer/child，避免 join 未启动线程或 cleanup 再次掩盖首错。修复须保留原
  deadline、returned/duration、unknown、无 Receipt 与 replay 判据，并用真实
  blocking-pipe 和异常注入验证。受控结构复现与现场根因分别记账；新本机或 CI 绿
  不能倒推未取得的 Windows 卡点，也不能覆盖 §2 的 v1 FAIL。详细证据见 §2 绑定报告。

- **完整 PR 矩阵应在候选冻结后启动（2026-11-17）**：draft 期间每次 `synchronize`
  都跑七作业矩阵会把正常迭代变成反复取消/重跑；现在 job-level gate 让 draft 不分配
  runner，ready-for-review 才启动。回归必须从 YAML 动态枚举 job，否则以后新增 job
  可绕过草稿门禁；main push 还必须钉住 concurrency group 含 `github.sha`，只检查
  `cancel-in-progress` 不足以保护逐 SHA 结论。PR event 的 `GITHUB_SHA` 是合成 merge
  commit，证据须同时记录 head SHA/tree、base SHA 与 event SHA，不能缩写成一个“同一
  SHA”。
- **锁文件消失不等于已核对进程死亡（2026-11-16）**：Host 退出清理持有的是旧 PID/`started_at` 与 Daemon 锁快照；`host.lock` 暂时缺失既可能是旧 Host 已退出，也可能是它尚未完成清理或新实例接入窗口。快照内 Host PID 仍存活时必须保留 Daemon，不能把一次缺文件观察当作 Daemon 的死亡证明。
- **`git status` 干净不等于对象字节干净（2026-11-16）**：blob replacement ref 可让 `rev-parse`/`status` 保持原身份与 clean，同时 `cat-file` 返回恶意内容；发行构建必须对每条 Git 读命令统一关闭 replacement objects，并保留真实 replacement-ref 回归。
- **两段目录 rename 需要可恢复凭证（2026-11-16）**：旧 output 移走与 staging 发布之间硬退会留下可见空窗和永久 staging/backup；不能只靠 `except` 回滚（`os._exit` 不执行），必须在破坏性第一步之前持久化恢复记录，并让下一次启动自动续做或恢复。
- **符号链接负控必须比较目标前后字节（2026-11-16）**：只断言“函数拒绝链接”不足以证明旧实现对目标无写入；测试须先构造真实 symlink，再核目标文件哈希/文件集合不变。Git tree 大小写冲突也要直接在 tree object 上构造，不能依赖当前文件系统能否同时落两个文件。
- **同一实例的退出请求也要防检查后替换（2026-11-16）**：真实 Daemon 探针证明旧实现会忽略额外 `expected_instance_id` 并把错误身份的 shutdown 当正常请求；仅比较调用前锁 SHA 不足以防止“SHA 检查 → RPC”之间的实例替换。复核维度必须放在同一 IPC 会话内，不能用两次连接拼接。
- **历史材料布局兼容必须先读后写（PR #198 候选）**：旧版非标准 `custom.db` 或异根 `data_root` 可能把快照、撤销 journal、事件 blobs、记忆正文留在当前版本不再读取的位置。`preflight_legacy_material_layout()` 只比较 canonical 旧/新材料路径并只读扫描改变解释的子树：空骨架放行，真实文件/链接或不可判定一律拒绝；CLI authority、`build_stack`、`open_memory`、`MemoryProjection`、undo outstanding 与 recovery assessment/执行在任何 store 写入或把 pending 判为空前触发。不会搬移/双写/自动迁移；用户应保留原布局，离线备份后用旧兼容版复核未决事务。标准同根与 `:memory:` 不受影响。

- **SQLite 主库实体句柄与材料借用分别计寿命（PR #198 候选）**：POSIX 已持有的 SQLite 进程记录锁可能被本进程关闭同 inode 的附加文件描述符清除；普通组合 access 共持由 `fstat` 固定的主库句柄，包含新建 `O_EXCL` fd，最后一份 access 才关闭。仅观察 released/GC 现场时不打开主库 fd。标准材料写入必须借用实际所属 `EventLog` 的活跃 access，ownerless 同根可借用，否则依现有域规则独立只读核验或拒绝；正常关闭使 access 停止新借用，现有借用仍持外部准入至写完。以机制和本机生命周期回归为证，不代表观察到数据损坏。SQLite 读连接由 VFS 自行管理，未承诺覆盖 raw/外部文件访问或 SQLite 内部未完成语句的回收时机。

- **GC 的引用保护跟随文件身份（PR #198 候选）**：同一普通单链接材料在不区分大小写的卷上可以用不同拼写读取。核验引用后记录原生文件身份，同时保留登记路径，扫描候选和写前预检都匹配两者。见 [W15 文件身份记录](changelog/2026-11-19-w15-reference-identity.md)。

- **重入需要重做可见实体的持久化屏障**。文件哈希匹配时要补做文件 `fsync`，已可见的待建祖先要补做各自的父目录 `fsync`，发布重命名或清理暂存目录后仍须同步稳定父目录。故障回包按当前现场列出保留路径。见 [B09 持久化记录](changelog/2026-11-19-restore-b09-durability.md)。

- **用户级 restore gate 不适合由每个普通测试争用（2026-11-16）**：两个独立工作树并行跑全量时，固定用户级 gate 会让恢复/迁移 crashpoint 子进程把 `MaintenanceBusy` 当作 1 而非预期 77。`test_recovery.py` / `test_migration.py` 已在本用例临时锁根中运行并显式注入子进程；仍须为真正测全局锁语义的用例保留生产路径，不能全局替换锁根掩盖它。
- **平台条件代码的导入也属于门禁（2026-11-16）**：macOS 菜单构建器与 Unix 测试辅助必须各自使用与其调用点一致的 `cfg`。本机 macOS Clippy 不能发现 Windows 侧 unused import；恰好由原生 Windows Rust job 以 `-D warnings` 抓住，修复方式是按目标裁剪导入，不用 `allow`。
- **`database_list.main` 不是 SQLite 内部 HANDLE**：标准库不能读出 VFS 使用的 OS 句柄。T1 能证明“锁定实体句柄 + main 路径 + 重开实体一致”，不能把这三项写成直接读取 SQLite 内部句柄。
- **读者不能用 `BEGIN IMMEDIATE` 冒充共享锁**：它会把两个普通连接串行化。registry observation 使用 shared namespace/entity + `BEGIN` 只读快照，写者使用外部 exclusive 锁；这样既保留并发，也让已接线 writer 立即互斥。
- **Windows 没有可移植的目录 `fsync` 等价物**：`FlushFileBuffers` 对普通目录句柄不能当成无条件成功。目录发布另行使用 write-through rename，并把物理断电级 durability 保持 `NOT_RUN`。
- **平台身份必须来自句柄而不是两次 `stat`**：路径可能在检查与加锁之间被替换。最终 entity identity、硬链接数和后续 `verify_path()` 都以真实打开句柄/重开实体为准。
- **终止测试必须绑定实际执行者（2026-11-14）**：Windows 的 venv `python.exe` 可另起真实解释器，`Popen.pid` 与屏障内 `os.getpid()` 不一定相等；只等启动器退出不能证明 worker 已死。`communicate()` 会关闭 stdin，使仍在屏障读取的 worker 遇到 EOF 并进入错误分支。硬终止测试须让屏障同时报告 PID、严格匹配被终止的进程，并在关闭管道前确认其退出。文本 stderr 的系统换行与二进制屏障协议分别精确断言，不用 strip 或吞异常把失败改绿。诊断清理自身的 WinError 5 与产品失败分开保留；本次七例诊断整体仍失败，其中四个 venv 清理错误不因两个直接解释器对照通过而消失。

- **测试入口也必须匹配生产初始化（2026-11-13）**：Windows上stdout重定向到DEVNULL仍可能以cp1252编码；测试用`python -c`直调Host、遗漏真实`cli.main`已有的`_force_utf8_streams`，中文print会在发布ready后异常、清元数据，表现为EOF/错误接管。用同一源码、同一cp1252环境、只补真实入口初始化的正负控定位，不能全局开UTF8掩盖差异。stderr保留原始bytes并按子进程实际UTF-8解码；失败收尾不能提前kill截断traceback或误杀Windows launcher而留下实际worker。只在最终runner证明Windows收尾，不拿Mac PID相同的情况代言。

- **探针必须持有自己发出的请求（2026-11-13）**：暂停真实Thread.start时发GET，0.4秒无响应就主动关TCP，会在放行后制造服务端10053。边界期限保持不变，但应保持同一连接，服务ready后在普通I/O预算内读完同一响应；继续检查stderr、资源与实际worker PID。`Path.read_bytes`经CRT可只剩EACCES，不能把底层share0必然等于异常对象winerror32当跨层契约。原红切源码时还须覆盖pytest的`pythonpath`配置并在同一进程核验导入hash，环境PYTHONPATH单独设置不足以证明跑到旧版。

- **恢复自身也是崩溃路径**：不能先提交unknown result，再在另一事务补block；重启会把“有result”当完成而跳过保护。新增不变量需同时覆盖新窗口、旧数据补偿与人工解除意图。
- **期限要覆盖真实阻塞点**：收到HTTP头不等于读完正文；创建响应deadline之前的PIPE写入、主线程关闭被读线程占用的缓冲流都能逃出期限。测试要用真实背压和屏障，不以增加超时或重试替代定位。

- **条件删必须与读、写、接管处于同一互斥（2026-11-13）**：读到“还是我的pid”不代表unlink时还是；stop拿旧快照后，新Host已经接管也会被误删，pid复用还要求实例token/started_at绑定。只给unlink加锁或只加pid参数都不完整。HostInfo使用`run/execution.guard`，不占业务根守卫，HTTP/Daemon等待不入锁；晚到的启动者条件发布失败时只能关自己资源，不能误停胜者复用的Daemon。Windows WinError5既可能来自真实双删，也可能是持久只读拒绝，不能统一吞掉。读失败更不能等同物理缺失；只在FileNotFoundError时宣布缺失。

- **发布端口不等于已启动服务（2026-11-13）**：仅在写端口时校验owner，之后到`serve_background`之间仍可被真health失败的后启动者接管，旧实例继续serve会形成两个活Host。发布和服务线程启动须处于同一短元数据临界区，停止回调先安装、monitor在条件发布后且HTTP开放前实际启动。验证正确互斥时必须以第二进程真实OS锁busy作为放行信号；先等第二进程ready才释放第一进程会把探针自身循环等待误判成产品死锁。全量绿不能压过独立反例，改后重跑受影响验收。

- **HTTP开放前必须完成所需线程启动（2026-11-13）**：a624把monitor移到HTTP服务之后，真实`Thread.start`边界暂停时`_thread`已发布而尚未启动；stop真实join抛RuntimeError，但finally仍关闭HTTP并放行主循环，导致退出0且日志未关。只有退出码/Daemon不可达断言会漏掉它，回归必须同时验收后台异常和所有本实例资源。修复启动顺序，不能通过隐藏`_thread`、跳过join或吞异常假装修好；本例Daemon已停，不扩大成孤儿进程。

- **定时诊断测试必须证明采到了所声称阶段（2026-11-13）**：第二读者用无sleep的setup屏障发现，旧三阶段测试只有栈header断言，snapshot都在setup时仍3 passed；真实Lock正控也会因提前采样缺目标帧而误红。测试专用ready事件应绑定实际目标阶段，setup/call/teardown各自断言不同函数帧；隐私测试也须确认采到带敏感变量的目标函数。等待诊断完成时要同步排空输出或落临时文件，不能引入未读PIPE阻塞。修夹具不等于修改生产60秒协议，旧测试版本的全量绿不移用为新版本门禁。

- **后台诊断的等待本身也会失败（2026-11-13）**：有限浮点数并不一定是平台可接受的等待秒数；`1e30`在Event.wait中OverflowError，若只把snapshot放try里，pytest可能1 passed/exit0而诊断线程已死。配置须受`threading.TIMEOUT_MAX`约束，后台等待与采样同置错误收口，并以真实成功/失败/中断子pytest验证非零诊断及原退出码保持。

- **诊断本身也会改变被诊断进程**：CPython3.12原生定时dump能在纯标准库动态frame负载中access violation；Python栈最后落在COMMIT不等于SQLite根因。须用无项目代码的同环境正负控区分。后台线程已经输出部分字节后仍可能抛错，`exit0 + stack_bytes>0`不足以作证；错误必须显式传到会话退出码。fatal负控也必须确认真的执行到abort，配置提前失败的非零不是有效负控。Python线程方案拿不到GIL时不能保证栈，不能把外部期限说成等价诊断。
- **清理要在资源构造成功时登记**：只在helper返回后或测试最后close，构栈/启动中途失败时仍会把真实子进程留给后续用例。MCP对象阻断夹具改为构造成功立即登记finalizer，并用测试失败/构栈失败两类注入证明资源清零；业务断言通过不代表夹具没有污染环境。

- **"审批门"要在弹批准之前判完**：白名单/额度这类注定失败的前置条件如果只在执行时检查，用户会收到一个"批了也发不出去"的批准——白占一次人工决定。把前置条件放进 `prepare()`（`c4.outbound_not_allowed` / `dispatch.budget_refused`），批准只发给能执行的请求。
- **冗余保护要"证明"，不是"声明"**：取消检查做了两层（派发前 + 执行器每块之前），单独拆掉派发前那层两个判据**仍然绿**（执行器第一跳之前就看到了取消），两处都去掉才真的发包。写负控时要先确认拆的是"最后一层保护"，否则会把冗余误判成"这行没用"。
- **"来源可离线核验"必须真的关掉服务器再读**：否则测的是"服务器还在，所以读得到"。runner 的收集顺序（Run 结束 → 停夹具 → 读内容引用并重算 sha256）就是这条判据的本体；读不回来记无效样本，不是通过。
- **"夹具 ≠ 线上"要写进判据与文档，而不只是注释**：联网研究的成功判据全部跑 loopback 夹具；报告、README、PRD §10.5 都写明"真实公网研究未验证"，否则读者会把"夹具通过"当成公网能力。
- **计划里估的测试条数会偏**：规格估 14 / 22 / 10，实测 18 / 23 / 11——补的是"匹配/迁移/CLI 端到端"这类不估就会漏的判据。以实测回填规格，不拿计划数当交付数。

- **"默认关闭"这种留账写法会被下一条要求直接推翻，别把边界写死在验收句里**：G1b 交付时"账本默认关闭"是刻意边界，G1c 就要求默认记账。可复用做法是把"为什么关"写成**条件**（关着时哪条 PRD 要求不成立），而不是写成永久结论——条件变了，结论自然要改。
- **`executescript()` 建表会先提交当前事务**：把"建摘要表"放在写路径里时，外层事务的原子性被打断——回滚之后摘要行还在，"摘要行与事件同生共死"变成口号。修法：建表在事务**之外**（构造时），回放用 `create=False` 不建表；判据是"外层事务回滚后不留半条记录"。
- **"关键信息保留"夹具会假绿**：第一版让 6 轮都读**同一个**带约束的文件，后面几轮的工具输出里当然还有约束，"丢没丢"根本没被测到。改成"第一轮读带约束的文件、其余轮读别的"才真正区分两种实现。**做保留类判据先问：关键信息是不是还有别的活路。**
- **负控必须拆掉所有层保护**：`BudgetConfigError` 是 `TianshuError` 子类，"安静回落"有两层 catch；只拆第一层时负控仍然绿。同理，摘要激活有"整段覆盖"与"真的被牺牲"两个条件，负控要能分别拆。
- **负控脚本自己会伪造绿**：本轮两次假绿都是脚本问题，不是代码问题——① 测试 node id 传成单个字符串（`exit=4 no tests ran`，看起来"非零"其实没跑）；② 替换只改了两处引用中的一处（占位里 `ref` 与提示里的 `{"ref": ...}` 各一份，去掉一处另一处还在，模型照样拿得到）。现在负控脚本先断言"替换目标存在且内容确实变了"，并在每次跑前清 `__pycache__`（同一秒内改写文件时 mtime 粒度会让 pytest 复用旧字节码，实测出现过一次）。
- **枚举型契约每加一个值都是三处同改**：`action` 5→7、`tool_id` 加 `read_content`、6 个规则编号——`rules.py` / `schemas/*.json` / PRD 表格，`tests/test_rule_ids.py` 与 `test_event_schema.py` 会钉住；`.json` 里的 enum **顺序**也要与 Python 一致（镜像测试是逐字比较，排序过就红）。
- **"绊线清单"要按设计用它**：`test_veto_scope.py` 里那份模型工具面字面清单不是冗余断言，每加一个模型可见工具都必须在那里显式过一次——加 `read_content` 时它确实红了，改它时要在 PR body 写明这是**有意的口径变更**。

- **「兼容读取」不等于「兼容写入」（本轮 fail-open 差点混过去）**：逐类型 payload 第一次实现时，`detect_version` 在 v1 校验失败后自动退回 v0，而 v0 schema 的 `required` 是清空的 → 任意 `{}` 都会被当成「旧版」接受，写入校验等于没写。修法是把读兼容（旁表/映射）与写兼容（显式 `legacy_write`）分开，并加元测试：生产代码零 `legacy_write=True`、显式 legacy 仍须过 v0 封闭 schema。
- **单线程里的同步推送 = 慢消费者 DoS**：`daemon._main_loop` 把请求处理和 `conn.send()` 推送放在一个线程里，一个「订阅后不读」的客户端写满发送缓冲就能把状态/审批/取消/shutdown 全堵住；改成有界队列 + 独立发送线程后，慢订阅者被断开且重连能按游标补读。
- **恢复判据要落在"同一个事务"上，而不是"看起来没写"**：记忆写的投影更新与 `tool.result` 同事务提交，于是"有 intent 无 result"⇔一定没生效，恢复可以给出**可证明**的结论；文件写没有这层保证，只能对快照/哈希说"既非写前也非写后"→ unknown + 阻断目录。别把两类副作用塞进一个 `pending_writes` 计数：一旦混算，接管判定会去要一份记忆写根本不存在的快照证据。
- **审计表要"执行期完整 / 终态脱敏"两段式**：执行器从 `dispatch_requests.args_payload` 取参数、`args_hash` 要绑定校验，所以不能落库即脱敏（会把派发打红）；但审计表没有擦除路径，正文留着就是永久副本。正解是请求进终态后**幂等**补做脱敏，并让终态推进与副作用提交**同事务**——否则会留"写成功但正文仍在"的窗口。被拒绝的请求永不会执行，落库即脱敏即可。
- **「声明」不能当「证据」**：隔离后端可以自称 `app_container`，探针必须真的去读/写界外文件、连 loopback、读父进程 env、观察孙进程；门只认探针报告，`unknown` 一律阻断。假后端负控（自称隔离但探针不通过）必须红。
- **备份/迁移的「中断」要真的注入**：只测 happy path 等于没测；`crashpoint` 每步硬退后重启，验证「可续做 / 无混合版本 / 或从备份恢复」，并证明「未发布的临时目录不会被当有效备份」。
- **严格分支保护下多 PR 串行合并**：每次合并都会让其他 PR 的 required checks 变「过期」（strict + enforce_admins），必须重新同步 main 再等 CI；不要用 `--admin` 绕过，也不要为了让 PR 变绿去删 required check。
- **夹具是在声明「我是历史形状」**：默认严格后 19 个测试文件的播种调用点显式加 `legacy_write=True`（不改 payload/断言）；调生产 API（`commit_model_turn`）的夹具则改为当前契约值（`finish_reason: tool_use`、usage 双键）。用放宽产品校验来迁就测试是最坏的一种「变绿」。

- **「CI 红了」先分家，而且必须按用例名逐条对账**：收尾轮 Windows 从 35 failed → 8（#87）→ 6（#88），每一轮都逐条读了失败用例名，才发现四条本轮新模块的真缺陷（`os.fsync` 对只读句柄 `EBADF`、`time.monotonic()` 分辨率、Windows `_pid_alive` 恒 `None`、隔离网络探针在 runner 上应记 `unknown` 而不是硬编码结论），以及两条只在 CI 时序下暴露的并发缺陷（握手期裸 `EOFError`、指标半行）。教训：**不要用「#50 家族」一句话结案**——按用例名对账，否则新缺陷会被旧标签盖住；本轮就曾把 #87 之前新模块的缺陷写成「既有族」，是错的，PR #87/#88 的正文与本文已按实测改正。
- **「本机连续绿」对并发窗口没有证明力**：握手 EOF 与指标半行在同一提交上 macOS 本机跑两遍全绿，只在 CI 的慢 runner 上才撞到。并发/时序缺陷的判据要能**确定性构造**窗口（假对端只 listen 不握手、手写半行再补齐），而不是多跑几遍碰运气。
- **`EOFError` 不是 `OSError`**：`except (OSError, ...)` 捕不到 `multiprocessing.connection` 握手期抛的 `EOFError`。凡是「连不上」的封装，都要显式把 `EOFError` 算进去并映射成自己的域错误；否则调用方按契约只捕 `(IpcError, OSError)`，收到的是契约外的崩溃。
- **JSON 能解析不能证明 HTTP 响应完整**：CPython 的有界 `HTTPResponse.read(n)` 遇到过早断流可返回短串；默认分块读取也可能接受错误的块分隔符或缺少终止空行的尾部字段。远程 MCP 在核对完整 JSON 分帧后才记为确认成功。分块 SSE 先核对事件所用的块，在收到完整匹配事件后返回，无须等待后续通知或流关闭。
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

- **"契约里写了"不等于"执行侧在看"（2026-11-11 实测）**：`SPECS` 里早写着 `argv.items` / `minItems` / `limit.minimum|maximum`，但 `validate_args()` 只看顶层 `type / required / additionalProperties`——嵌套约束**从未执行**，模型给 `argv: [1]` 也放行。这比没有 schema 更糟：它让模型与审批人以为检查过。判据要钉"非法值真的被拒"，不是"schema 里写着 items"。
- **别把"未实现的关键字"当注解忽略（同轮）**：JSON Schema 里 `oneOf` 未知实现时可以合法忽略，但准入边界不能——注册表写着、执行侧不看就是撒谎。做法是**封闭白名单 + 注册期拒绝**，让"多写一个关键字"在 import 时就红，而不是等模型调用。
- **加一个身份字段进摘要时，先问"历史摘要会不会变"（同轮）**：`description_hash` 若无条件写进 `to_canonical_dict()`，历史库所有审批 / grant / 派发摘要集体变化，"更安全"的表象下是把既有授权链作废。正确做法是条件字段 + 按值钉死旧摘要的测试。
- **预占/结算类账本必须用数据库原子性，不能"先读后写"（同轮）**：并发抢最后一份额度时，普通读-改-写会让两边都读到旧值。`BEGIN IMMEDIATE` 内检查+插入+扣减是唯一可靠做法；用例要开**两条独立连接**模拟两个进程，并做"去掉事务 5/5 次变红"的负控。

- **加了列 ≠ 接通了路径**（N3）：`memories` 加 `layer` 后，INSERT 占位符少一个、`search()` 手写 SELECT 漏 `m.layer`、FTS 装配少一列——三处都会在运行时炸（`IndexError` / `19 values for 20 columns`）。加列时按"建表 → `_MEMORY_COLUMNS` → INSERT → 每条 SELECT → `_row_to_memory` → 迁移"逐点核，别只改建表。
- **自述的守卫要能被一条测试打红，否则它只是注释**（N3）：`consolidate()` 的 `proof_count` 就位校验最初绿得毫无意义（测试只测了生成器自身的一致性），补了"塞一个坏页"的用例才算数；执行侧的 workspace 守护经派发路径根本不可达（派发层先拒），就在 PR/SPEC 里**如实写"没有测试能单独打红它"**，而不是假装覆盖。

- **推送要看远端，不要看退出码**（N3/N6）：worktree 一度处于 detached HEAD，`git push` 仍以 0 退出（它推的是那个没前进的分支引用），于是"已推送"是假的。以后每次推送后 `git ls-remote origin refs/heads/<branch>` 核对。
- **同名断言会把真问题掩盖**（N3 复审两次抓到）：`tool.intent` 里有 `tool_id`，只断言"事件里有 intent/result"会被别的工具的事件满足；断言必须带 `payload['tool_id'] == ...`。

- **CI 慢机器会把「窗口」放大成真 bug**（N6）：`open_control_run` 先 enqueue 再 claim，本地毫秒级窗口在 ubuntu CI 上被 Daemon 驱动线程的 `claim_next()` 抢走，`memory.remember` 报 `internal: job 仍由活着的 owner 持有`。修法是 `JobStore.enqueue_and_claim()` 把入队与认领放进同一个写事务；判据用「另一条连接在 enqueue 瞬间抢」的确定性补丁，而不是开线程碰运气（并发版本 8 次一次没撞上）。

- **预算底层通过不等于真实调用已接线（2026-11-13 独立复审）**：账本支持 `micros`，但普通/摘要预占不带这个维度时，零金额上限也会漏发。必须在真实 adapter 的受控 transport 边界观察预占，并核对实际费用、重试与父子链。新写入口拒绝负数也不能保护旧负计数；升级后读取和新准入必须 fail-closed，不能把“历史数据”当作授予额外余额的理由。未知 token 与未知金额是两个维度，修正未决计数时分别用精确断言，不能为保住旧数字漏记未知费用。
- **内存统计修好不等于终态已记住（2026-11-13 B3 二审）**：摘要失败后未知计数在 `RunResult` 中正确，但若依赖下一普通调用成功再存游标，预算立即拒发会让 `run.end` 读旧值。回归必须覆盖“失败后马上停止”，并分别核对内存、持久游标、终态与重开结果；不能只凭继续运行的成功路径宣称修完。

- **证据也有成本，超过代码就不再是「更严谨」（2026-11-13 系统 debug 复盘）**：一天合入 7 个 PR（#130–#136），`3648d79..cefa1e3` 共 256 文件、+53,367/−971 行，其中 `docs/` 192 文件占 +43,812 行（约 82%），代码与测试合计约 9.5k 行；`docs/evidence/` 内 8,819 行 evidence-index、6,927 行 commit-binding、4,860 行 source-object-map 属机器可生成物，而原始日志/截图本就归档在被忽略的 `.omc/artifacts/`。教训：**证据能生成就别手写**，仓库只存索引、结论与不可再生原件；一条证据若不能让人在一分钟内复核某个具体断点，它就是负担而不是证据。留账是为了让人敢下判断，不是把判断淹掉。
- **门禁时长贴住 job 上限本身就是可靠性风险（2026-11-13）**：Windows 全量 23 分 13 秒、job 上限 25 分钟，只剩约 2 分钟余量；四项 required 加 strict 下串行合并会重复触发整轮 CI，所以本轮策略是「本地各自收口后集中推送」。教训：把检查列入 required 只解决「敢不敢合」，不解决「多久能合」；并且必须把**超时取消**与**断言失败**分开记，前者不能写成后者，也不能靠加时或加重试掩盖。
- **能推翻结论的实验要留提交，但不进产品（2026-11-13）**：`codex/debug-windows-diagnostic`、`codex/debug-host-stop-diagnostic` 刻意停在分支——纯标准库反例证明「原生定时转储会在动态 frame 负载下崩」，但那是证据不是产品。教训：反例实验要有自己可复现的提交与 run，可以永远不合 main；历史误红保留原样，「当时现场」与「本次结论」分开写，既不倒推找回已丢的 stderr，也不把实验成功当产品通过。
- **「只在某平台红」先假设是代码（2026-11-13 issue #50 收口）**：长期结论是「生产不受影响、必须实机才能定位」，实际根因是 `load_authkey()` 缺 `O_BINARY`：Windows 文本模式把随机 `LF` 扩成 `CRLF`，创建者返回原 key、后续进程读到另一个 key；GitHub 托管 runner 稳定复现。教训：跨平台默认值（编码、换行、文本/二进制模式、路径分隔符）一律显式写死；把平台差异当环境噪声，一个真缺陷可以挂上几个月。
- **第二读者要换的是上下文，不是措辞（2026-11-13）**：本轮复审在新会话 + detached 工作树执行，作者不为自己批准，只按已冻结的验收项逐条对照，记录完整 SHA、范围、命令、输出与未决项。教训：同一上下文里「再检查一遍」用的还是同一套错误假设；复审的成本主要在**重建独立观察环境**，省掉这一步就等于自审。

## 6. 流程规则（风险分级门禁 + 隔离纪律）

- **`main` 机械保护不可绕过**：走 PR、四项 required CI 全绿（`strict`）、`enforce_admins`、禁强推/删除；契约变更（PRD §5 C1–C5、§6 红线、§8 权限）必须 `docs(contract):` 单独 PR，不夹带代码。
- **风险分级门禁（Tiered Review，消除一刀切重仪式）**：
  - **Tier 1（高危核心改动）**：触及执行侧授权（`permissions` / `dispatcher` / `guard`）、事件日志（`events`）、租约与并发写（`leases` / `jobs` / `restore_registry`）、崩溃恢复与备份（`recovery` / `snapshot` / `backup`）、共享计费（`budget`）、网络/执行隔离边界（`net` / `exec*`）、安装/卸载脚本 —— 必须跑**定向红绿与负控 + 本机全量门禁 + 独立第二读者探针复审 + GitHub required CI**。独立复审结论绑定**非文档代码树（code/test tree）**：后续仅补充纯文档时，说明相对已审代码树无代码变更即可，**不再重跑本机全量或循环重绑复审 SHA**。
  - **Tier 2（常规功能 / 控制面扩展 / UI 面板 / 普通测试修复）**：跑**定向红绿测试 + 受影响域回归 + `uv lock --check` + `ruff`（涉及 UI/Rust 跑对应检查）+ GitHub required CI + diff 复核**，免除独立第二读者重仪式。
  - **Tier 3（纯文档 / 台账对账 / 注释微调）**：跑 **`uv lock --check` + `uv run --frozen ruff check .` + 链接与事实核对 + GitHub required CI**，免除本机全量 `pytest` 与独立复审。
- **单会话独占 Worktree 与测试全局隔离**：并发会话一律使用独立 git worktree 与独立分支，合并后立即移除 worktree；严禁跨会话共用同一工作树或使用 `git stash`；测试必须隔离临时数据根、临时维护锁根与独立凭据命名空间，不得抢占宿主机全局锁或污染真实 Keychain。
- 细节见 [CONTRIBUTING.md](../CONTRIBUTING.md) 与 [AGENTS.md](../AGENTS.md)。

## 7. 已知限制 / 未决问题

- **Agent 主循环 P0 设计草案（2026-11-22，待批准、未实施）**：
  [主循环规格](SPEC-AGENT-LOOP-2026-11-22.md)提出统一 Mission、结构化 Plan、
  独立验证、Goal V2 受控推进、有限授权和执行对象隔离准入，复用现有事件、
  Run/Job、Dispatcher 与预算账本。本文档不改变现行契约或 G1 能力；
  各批契约、实现、原生平台及交付证据仍须分别完成。

- **Goal G1 不是自动执行模式（2026-11-22，本地候选）**：每轮手动继续、人工逐项
  验收；没有自动循环、机器验收器、累计目标预算、期限或 Goal CLI。失败或未知结果
  不自动解除阻断；首版无附件和单次模型档案，使用当前默认模型。脚本模型与本机
  浏览器验证不代表真实付费模型、其他平台设备或原生桌面验收；远端 CI 尚未运行。

- **本地 Skill 首版边界（2026-11-22，候选）**：仅支持用户与当前所选工作区
  `.agents/skills/<name>/SKILL.md` 的有界文本发现和有限 frontmatter；
  不接插件市场、安装器、完整 YAML、自动技能路由或递归资源加载。
  源路径与正文会随任务输入发送给当前模型，菜单有提示；新建任务的快照会使目标显示变长。
  文件观察为协作式检查，不是同 uid 攻击隔离。浏览器验收使用临时 Home、数据根、
  独立凭据服务名与脚本模型；不代表真实付费模型质量、系统输入法实机、
  Windows 文件语义或正式桌面发行验收。远端 CI 尚未运行。

- **旧预览的原生封套失败仍为失败（2026-11-20）**：旧 `0.1.0` 下载资产在本机 codesign/spctl 均因缺失资源封套失败；后者是操作失败，不能解读成 Gatekeeper 放行或拒绝的有效策略结论。独立实验副本和本轮 tiny-C 结构回归不改写旧资产，也不等于新产品 build、真实安装或原生 runtime 通过。本次 roundtrip 的 fd-relative 无链接提取仅约束私有新目录；源树扫描/归档的同 uid TOCTOU 与原有 runtime provenance 边界仍保留。未做 Intel Mac、其他 macOS 版本或 Windows 用户设备验证。

- **受限网页表单候选（2026-11-21）**：支持同一次运行中已保存的同源静态表单，经逐次批准执行一次无凭据 HTTPS POST。确认收到完整响应时落网络 Receipt；传输不明不自动重发。没有脚本执行、登录态或文件上传，见[本轮记录](changelog/2026-11-21-browser-form.md)。
- **产品能力补齐候选（2026-11-21）**：网页导航限于公开 HTML 和本 Run 的同源链接；没有通用浏览器自动化。Webhook 接收器只监听回环，公网 HTTPS 代理尚未部署或验收。HTTPS MCP 只支持固定端点的 JSON/SSE 请求响应，不支持 OAuth 和自动重连。Responses 自动压缩须模型端点显式支持；本地超过预算时若没有可用的已提交压缩边界，会停止而不会裁剪密文。真实模型、真实第三方 MCP 服务和用户设备未验。实施与使用边界见[本轮记录](changelog/2026-11-21-agent-product-capabilities.md)。

- **旧材料空树复核不是物理快照（2026-11-20）**：`7b4d80b` 通过最终扁平真实列举弥补已下降目录中迟到材料被 stale stat 隐藏的缺口，但有限观察仍不能排除某目录最后一次观察后的同 uid 修改，也不是物理隔离。该修复不改变 Windows 受管恢复写入的 `durability_unavailable` 边界，不解决下述自定义路径 #204；本地全量/能力投影不替代新托管 Windows/Mac CI 或用户设备验收。

- **非标准材料路径的保留名祖先误识别（2026-11-20，未修）**：标准材料域检测沿祖先识别 `artifacts`、`blobs`、`memory_blobs`。非标准数据库或异根 data_root 所用的私有 materials 布局若又位于这些保留名祖先下，可能被误认为另一个标准材料域，因所有者数据库不匹配而 fail-closed 拒写。默认 `events.db` / AgentOS 布局不受此已复现组合影响；这不是首次 Apple Silicon 限定预览的阻断项，但一般自定义 `--db` 兼容限制仍未修。将 pytest 临时根移出这些祖先只消除了本次测试布局碰撞，不是产品修复，也不构成任意数据库/材料路径均受支持的声明。跟踪 [#204](https://github.com/lorek666/tianshu-agent/issues/204)，见[整合门禁记录](changelog/2026-11-20-preview-release-integration.md)。

- **开发阶段原生发行 smoke 的测试隔离（2026-11-20）**：`release_smoke.py` 虽创建独立临时数据根，
  生产 `maintenance._lock_root()` 仍绑定 OS 用户身份，`RestoreRegistry` 绑定系统 Home；
  测试套件的 `TIANSHU_TEST_LOCK_ROOT` / registry 注入只在测试启动钩子中生效，不能据此宣称
  bundled CLI 已与日常账号隔离。不要求新建或切换 OS 测试账户；完整原生 smoke 须隔离测试数据和工作区，
  不得触碰现有生产数据或凭据，不能安全隔离的项目仍记为未验证；不得改 HOME、加启动钩子或伪造 pass。

- **G3 本地浸泡证据不等于发布验收（2026-11-19）**：按单调纳秒记录已就绪后的观测窗，扣除计划重启停机，不以运行器总时长或毫秒舍入的采样时间判定满 10 小时。`completed` 必须在最终材料之后发布；`--verify-only` 只核对字节摘要与记录一致性，不证明执行来源、机器未休眠或全程健康。离线核对和 v1 `PASS` 均不会改变 `release_acceptance = NOT_EVALUATED`。独立运行须用专用测试 OS 账户或隔离测试机，临时数据根不是生产用户级维护锁隔离。见 [SLO 基线与复跑方式](SLO-BASELINE-2026-11-18.md)。

- **R3 代码前置已在本地组合候选汇合并完成精确 required CI（2026-11-16）**：`scripts/build_release.py`、`scripts/verify_release.py`、`src/tianshu/release_smoke.py`、`scripts/package_app.py` 与 `release.yml` 已在 `codex/release-final-integration` 组合出现，因此“当前基线缺少 R2 入口”的旧阻塞已不再成立；组合候选已由 PR #175 合并，最终对象 `343ebf0/tree 94f8e34f` 的 required CI 已由 run `43889749967` 七 job 全绿绑定。组合代码不得当作 release workflow 已执行：四目标原生 artifact、draft Release、NSIS 7-Zip 解包、真实 bundle 安装/升级/回退、Gatekeeper/SmartScreen、Windows 10/11/ARM64、macOS x64 用户设备与物理断电均仍为 `NOT_RUN`。macOS 安装脚本的 crashpoint/原子换装测试只证明本机 injected process-loss 路径，不是设备掉电证据。三轮原生 CI 已证明 GitHub Ubuntu、macOS、Windows Rust 与 UI job 可运行；旧候选 25 项红、`a55e752` 的 raw-tree 夹具红和 `1a619af` 的 SQLite 定稿红均已定位，`ded5e97` 已完成备份恢复高危复审。四目标 release workflow 仍待实际执行与绑定。
- **打包封闭检查与最终 provenance 边界（2026-11-16）**：`package_app.py` 会拒绝源仓库、bundle、输出祖先及四棵资源树中的 symbolic link/非普通文件，并在 Tauri 完成后再次验证 App/安装器；`verify_release.py` 同时比较 macOS App 根文件和目录集合。检查到实际读取/归档之间仍存在同 uid 写者可竞争的理论 TOCTOU（未实现 fd-relative/openat）；最终 verifier 不接收原始 runtime archive，不能独立重算其字节 provenance。该残留必须保留，不能因为本机负控变绿写成物理封闭。
- **Host 存活探测仍可能受 PID 复用影响（2026-11-16）**：`host.lock` 缺失后的 Daemon 保护分支使用 `pid_alive` 判断快照 PID；当前没有把应用启动墙钟 `HostInfo.started_at` 当成操作系统进程启动时间，也没有扩大 schema。PID 被无关进程复用时，桌面壳可能保守地拒绝清理 Daemon，用户可退出旧进程/释放错误占用后重试；该分支不会误停 Daemon，但“存活”判断本身不是强 OS 身份。
- **受管恢复与离线 GC 的状态（2026-11-20）**：T1 身份锁和准入底座已在主干。受管 `apply/review/resume/abort/release` 和 W15 批准执行仍在 [#198](https://github.com/lorek666/tianshu-agent/pull/198) 的候选中；本候选在当前工作树完成 Restore B/C payload-history 与 legacy-unknown 修复，包含外置 payload、历史 v0 unknown、缺失/非法 MCP target、malformed active-block history 的真实回归。最终本机全量命令 `uv run --frozen pytest -o addopts="" -q -rs` 在隔离锁/注册表/授权根下通过：**4196 passed, 15 skipped, 868.89s**（退出码 0）。`uv lock --check`、`uv run --frozen ruff check .`、编译、`git diff --check` 均通过。独立 Tier-1 复审对当前代码/测试变更集结论为四项发现均已修复、无代码级 blocker；`git diff --binary HEAD -- src tests | shasum -a 256` 指纹为 `7a531a7b4122558db2dc3991d4f103a118e823a850343f06117816ac21bb39c0`。远端 PR 仍是旧 head 的 draft，故不能把本机结果写成 required CI；Windows 有效的受管写入返回 `durability_unavailable`，后续能力见 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。设备掉电仍需独立取证。
- **入口协作边界**：EventLog、迁移、离线备份和 GC 批准接入组合准入；材料写入借用该所有者的实际 access，不能凭另一个已开连接推断授权。旧程序、raw SQLite 和同 uid 直接改文件始终在协作锁声明外。
- **Windows T1 原生结果已绑定，但仅覆盖 Windows Server runner**：PR run [43889749967](https://github.com/lorek666/tianshu-agent/actions/runs/43889749967) 的 artifact `restore-t1-windows.json`（zip SHA256 `c6e7aa68abedc002009d88ac70b0f70a019cd01948807854b838fb80c0ac201d`）绑定 `343ebf0` 并返回 `status: PASS`；merge 后 main run [43893763334](https://github.com/lorek666/tianshu-agent/actions/runs/43893763334) 的同名 artifact 绑定 `219d17ab`、返回同样 `status: PASS`，原始 JSON SHA256 为 `e293d8969a7eeb3a6bace8f177ce7fd00ac3ee3235a9ff09657c4967269124a2`。两者证明 `GetFileInformationByHandleEx(FileIdInfo)`、共享/排他锁、hardlink、rename 与 registry `MoveFileExW(MOVEFILE_WRITE_THROUGH)` 探针在 Windows Server 2025 AMD64 runner 上通过；SQLite VFS 句柄仍为 `NOT_EXPOSED_BY_STDLIB`，目录掉电持久性仍为 `NOT_PROVEN`。该结果不代表 Windows 10/11 用户设备、ARM64 或物理断电。
- **c2cf双红与诊断边界（2026-11-14）**：M2活跃renew context的32ms/63ms观测差不证明单次5秒持锁，run.end的none_applied也不证明execution.started后文件未动；不按无证据假设修改锁策略。MCP此次CI在returned断言停止，后续unknown/replay断言未执行；本机真实屏障只证明原测试能混入审批和落盘等待，不定位原Windows卡点。最终测试计时/清理和M2起点/调用链诊断已独立限定PASS、本机完整门通过；实际Windows与正式对象仍另验，不借PR149绿色销账。READONLY和注入7ms仅验诊断；[本轮记录](WINDOWS-CI-TIMING-2026-11-14.md)保留范围。

- **M2诊断验证边界（2026-11-14）**：766a候选已在本机真实POSIX别名输入修复诊断caller丢失，并独立核根外同内容副本仍external；Windows junction分支本机未执行。诊断成功静默、失败附note，但包装及resolve有非零时序成本；它只观察已装配本进程context，不是OS锁owner、外部写者或精确COMMIT起点。新完整本机门不覆盖原Windows根因，正式对象/原生CI另核。

- **2026-11-14 Windows 首红（历史记录）**：[main `b450` 的 push CI](https://github.com/lorek666/tianshu-agent/actions/runs/43567298082) 在正常收尾用例出现 `database is locked`，Run 留在 `queued` 且缺 `run.end`，Windows 汇总失败。同一事件指纹可由不同事务的 `BEGIN` 产生，当时没有原始 SQLite 错误码或持锁轨迹。[审批路径诊断](APPROVAL-CI-DIAGNOSTICS-2026-11-14.md)仅覆盖已装配的本进程上下文。历史失败保留，#145/#147 后续销账见 §2。

- **低磁盘边界**：PR #144 及其合并对象 `8f2a` 的首次 CI 结果见[低磁盘记录](LOW-DISK-2026-11-14.md)。逐位置的可用空间探测只做准入，不是磁盘配额；旧低磁盘提交的覆盖范围与本次恢复/GC 候选的验收分别记录。Windows 用户设备证据仍待取得。

- **T1装配/恢复继承缺口已在T2独立范围内修复（2026-11-14）**：固定已审T1源上的六个受影响测试原样复现失败：4个跨进程pending MCP恢复缺发现注册表、dead-at-dispatch夹具启动前取server、摘要硬退子进程漏传真实memory。T2只补pending MCP按需准备和两个夹具实际实例；原硬退/unknown/对象阻断断言保持。另原并发审批用例固定T1通过，新增执行上下文门后测试两线程手装配缺context，最薄补同连接Snapshot/authority/guard后通过，未放宽生产校验、竞争和一次消费断言。上述不撤销T1此前已审范围；T2 的完整冻结内容与窄 delta 已由第二读者独立 PASS，但仍不扩大为 T3/整功能通过。

- **低磁盘 T1 C2 尚未独立销账（2026-11-14）**：旧候选的新 policy 写读类型不闭合；round1 只收紧新 `run.start` v2 的严格对象语义，保留历史 v0/v1 schema、其他事件和字段的 JSON Schema integer 语义。不能放宽对象 `.of` 或把已写坏资格补为 effects/pure_only；本轮作者467项联合通过与负控不替代独立复核，旧800项结果也不补证该缺口。

- **低磁盘 T1 C1 尚未独立销账（2026-11-14）**：旧候选最终空间拒绝留下控制 Task/创建事件，独立复核已否定这一范围的旧作者“已修”表述；round1 的同事务修复及 takeover 真子进程实例适配只有作者窄测/负控证据，必须绑定新冻结交第二读者复验。最终空间复测仍在创建事务内，沿用既有嵌套事务异常须传播到外层才回滚的语义；未扩大为整个控制入口事务，也未变更租约接管、授权或执行侧门禁。

- **低磁盘 T1 候选尚未独立验收（2026-11-14）**：创建侧的 `dispatch.disk_space_low/unknown` 已与真实产生路径、代码/schema/PRD 清单同步接线；`dispatch.run_effects_disallowed` 留给 T2 真实执行守卫，不提前登记为 unused。资格损坏仍报原异常；当前纯读路径的材料缺席与创建拒绝仅属 T1 定向候选证据，不代表直接派发、执行器、网络/MCP 发送或恢复封口已交付。

- **低磁盘执行与可达入口历史缺口（2026-11-14，T3候选已补）**：T1 的 TaskStore/Loop/控制创建与逐类型 v2、T2 的执行侧空间与资格复核/已有 Run 纯读/模型/停止控制例外均已形成冻结候选并由第二读者核验；T3 作者候选已补 CLI/RPC/UI 显式模式选择器。实际位置逐个探测，仅相同 canonical 路径去重，不借同卷假设跳过嵌套位置；受控叶子注入不冒充真实多卷/挂载或 Windows 用户设备实测。T3独立内容和修后本机统一全量已通过；最终窄增量/对象绑定与原生CI另核，恢复 apply/隔离、容量与执行 GC 不在本片。

- **PR #137 合并已确认，merge SHA 自身 push CI 仍缺失（2026-11-14）**：精确 PR 原生 CI 已通过，真实 main 已含其合并对象。早期 REST/GraphQL OPEN / merged=false 是历史观察；后续 REST 已返回 `closed / merged=true / merged_at=2026-11-13T21:26:14Z`，合并事件销账。同一时点按 `6c291a6` head 查 workflow runs 为 0，故只可继承 head CI 结论，不能声称 merge SHA 自身 push CI 通过。停止重复合并操作并保留早期异常原件，后续提交的 CI 不能补证这个缺失事件。该异常不放宽四 required、25分钟上限或独立复审；Windows runner 通过也不证明 Windows 10/11、ARM64、安装/ACL 或用户设备体验。下方旧“未通过”条目是各自历史 head 的结论。

- **Windows 分片 C1 整改候选（2026-11-14）**：独立读者用同一真实fixture证明原 wrapper 收集错误 exit2、v1分片 exit3；作者新增精确对照又复现收集晚期 `KeyboardInterrupt` 被改成3。v2在已有收集失败时保留原生流程，不对部分collection分组；未进入分组的收集中断也不触发最终列表异常。失败报告仍不满足session0/完整覆盖，汇总拒绝；无错误时空集合/空片/晚过滤守卫保留。作者71项定向已过；第二读者12个真实受影响场景已确认C1已修、部分修/未修/内容未决均0，正常覆盖及空片/晚过滤/缺阶段/失败拒绝守卫仍在。最终537文件组合lock/full/ruff通过（2987 passed / 8 skipped），正式SHA绑定与新原生CI另核，不将“非零即可”当退出码保留证据。

- **Windows 分片按节点数均分也可能超时**：[#216 的主干 CI](https://github.com/lorek666/tianshu-agent/actions/runs/44907705179) 与 [#213 的候选 CI](https://github.com/lorek666/tianshu-agent/actions/runs/44911274075) 都在分片 0 完成 pytest 后触及 25 分钟上限；`pytest_session_exit=0` 和已上传报告不能覆盖 job 取消。分组对已校准文件使用固定实测估计，允许同时纠正高估与低估；两个 runner 与汇总仍从同一个算法、完整收集和精确 SHA 核验覆盖。历史耗时回算仅用来选择权重，新 runner 上的结果以实际 CI 为准。

- **PR #137 Windows 整套仍未通过（2026-11-14）**：H3 `e324edeb9c57cd9e8fe851a2657da8420e7838a2` 的 run43474954367 attempt1 仍在25分钟取消，另三项绿；三个 CLI 已通过，2765 PASS / 31 SKIP 只是部分输出。双独立 runner 文件分片是后续基础设施候选，新原生 CI、完整覆盖、独立实施复审与实际耗时仍待核；节点数平衡不保证25分钟完成。不得加时、移除 required 或借本机绿色合并；输出窗口不足以判定性能根因。Windows Server runner 也不代替 Windows 用户设备、ARM64 或安装体验验证。

- **基线收集不是执行或批准（2026-11-13）**：该开发工具只检查显式输入的完整结构并保全字节，无法鉴别完整伪造、证明历史命令/退出码，或保证发现先改后还原的瞬态变化。收据与原件包含私有材料，只能分享固定字段摘要；摘要单独存在不是完成凭证。发布后被终止仍可有合法收据，不提前记自身 exit0；不承诺断电持久性、同 uid 防篡改或 Windows ACL。真实模型、公网任务、十小时长跑、SLO 和用户设备验收仍须另验，不能借收集器本机通过收口。

- **第二轮修复后的保留边界（2026-11-13）**：模型跨源redirect拒绝，但同源POST307/308仍是urllib既有HTTP错误，仅302正控已验；网络响应总deadline修复不等于可中断DNS系统解析；IPC认证握手期限、命名管道跨用户ACL、Windows10/11/ARM64用户设备未由本机证明。备份Receipt材料/原生parser/事件版本/runs全集核对已修候选，仍未交付审批租约全历史重建、恢复apply/隔离、低磁盘与GC。最终提交和CI见[第二轮总账](DEBUG-ROUND2-2026-11-13.md)，下方G3“旧backup边界未修”为其历史切片时点，不能覆盖本条。

- **G3本切片边界（2026-11-13）**：实际EventLog准入、备份绑定迁移和显式CLI已有内容独立验收及最终本机全量；本条提交前快照时新CI待核，后续状态见对应实现PR正文及后续独立原件。0→2仍只改变迁移机制；restore hold/journal、实际apply、记忆隔离、低磁盘保护、保留/GC未交付。v2备份不带进度，不能补造/复制旧绑定绕过恢复后准入。另一轮只读debug已确认的Receipt材料、snapshot/undo语义、未来版本、投影缺行、manifest类型等旧backup/restore-plan边界未在本片修；后续恢复或GC前须收口，不能借完整digest宣称全部备份语义已修。

- **T2 候选不等于迁移产品接线或 G3 完成**：本机实现仍待第二读者按冻结四文件做具名变异验收；T3 的 `data migrate` CLI 尚未接，整合全量与原生 CI 未跑。恢复 apply/隔离、低磁盘和 GC 仍缺；维护锁只约束合作式调用，当前仅适用于两步机制迁移，边界见 [T2 记录](DATA-ADMISSION-T2-2026-11-13.md)。

- **恢复跨库窗口仍存在**：本地unknown与阻断现同事务，但事件库与authority是两个数据库；本地提交后、authority登记前死亡，跨数据根可见性依赖该根再次恢复补登记。命令没有物理沙箱，unknown恢复只阻止安全重放，不证明已识别任意外部副作用。

- **预算历史数据与金额上界**：本轮拒绝读取/使用已有非法负计数，没有迁移、洗白；真实数据库异常需单独取证。旧版已误结清但只剩合法正数量的 partial 记录不能凭空重建未知信息。估算模式不是硬上界，当前价格表无法提供所有计价档位的金额硬上界，`upper` 加金额限额会明确拒发；缺价且任一祖先受金额限制也拒发。默认无金额限额仍记录未决费用，零金额预留不表示免费，未知用量不自动退款。

- **C-007已修，仍有边界（2026-11-13）**：正式b6对象已独立绑定且四required通过；5e7原生Windows专项20 passed/1 skipped、helper2 passed、7111真实拒读负控与实际worker/launcher/Daemon退出证据经第二读者核对通过，见§2及I/O报告。dd49六红、cp1252入口缺口、旧monitor取证错误、7111/d6ee/a624反例均保留为历史，不倒推找回已丢弃stderr或冒称同栈。HostInfo guard只保护合作进程短读写/条件删和条件发布→monitor实际启动→HTTP服务启动，不是整个启动或运行期排他锁，也不是同uid安全边界；port0期间可被接管，败者须拒绝发布/删除。外部程序/杀软持续拒绝仍明确失败，Windows10/11/ARM64用户设备仍未验证。

- **CI线程栈边界（2026-11-13）**：为避开已在Windows3.12.16独立重现的原生定时frame竞态，Windows使用Python线程定时采样；C扩展长期占用GIL时无法保证60秒出栈。fatal handler仍保留，25分钟job上限不变，但两者不是该缺口的等价替代。未验证所有C扩展/GIL死锁、真实百线程并发或stdout/stderr同时外部阻塞。7111正式Windows矩阵已逐项通过31项包装器回归，三个60秒栈后相应用例继续通过；整轮因C-007失败。这些结果和临时原生实验success都不替代后续提交的业务套件通过。

- **I/O debug 范围（2026-11-13）**：已修复的有界读写不等于进程隔离或网络带宽硬配额；Windows 命名管道 ACL、Windows 用户设备/ARM64、真实模型驱动 UI、安装与升级仍未验证。C-004是已确认的既存POSIX发送关闭竞态，本机回归不代替新提交独立复审与Ubuntu CI；Windows的不同发送路径保留既有测试，不用POSIX调度探针代言。当前工作区的本机绿不冒充独立批准或云端绿。逐项证据与尚未验证范围见 [I/O debug 记录](DEBUG-IO-2026-11-13.md)。

> 本节含按日期保留的阶段交付记录，其中“未做”只描述该阶段；后续交付以 §2 和相应专题文档为准。平台与合并门禁以 §2 的具体提交 / CI / 远端配置为准，旧失败数字不代表最新提交。

**PRD 2026-11-11 工程评审新增要求：首轮交付留账（2026-11-11，PR #79–#84；对账见 [PRD-REVIEW-DELIVERY-2026-11-11.md](PRD-REVIEW-DELIVERY-2026-11-11.md)）**

- **G1 已收口（2026-11-11，PR #91 / #92 / #96 / #97 / #99）**：§5.2.3 通用工具准入（封闭 schema 方言、注册期拒绝、嵌套校验、描述绑定、身份字段双闸、URI 拒绝、准入在派发前）+ 五种资源种类与外发许可（外发恒需人工批准、无执行器种类派发前拒绝）+ 有界摘要/内容引用/分页读取（任务可见范围 + sha256 复核）；§5.6 共享预算账本（**默认记账**、原子预占、幂等分维度结算、未知不记零、父子链、取消传播）+ 硬上限模式的**可验证上界**。**仍不成立**：① 网络 / MCP / 委派的**执行器**（G2），所以「私有内容未经外发许可不发包」只能记 **unknown**（闸门就位、可拒绝，但没有外发面可验证「发了什么」）；② `run_command` 仍只受显式 `ExecPolicy`，不经过隔离门；③ 硬上限的每消息固定开销不在上界内（靠 `overrun` 暴露）；④ 没有任务级外发白名单（`outbound_allow`）；⑤ ~~账本没有 UI 展示~~（**2026-11-14 W8 部分销账**：Run 事件面的预算面板已交付——限额 / token 已知与未知分列 / 已知与未知费用分列 / `dispatch.budget_refused`·`dispatch.subtask_limit` 拒绝记录；**账户级账本视图仍未交付**——上限设置、未决预占清单、下载字节与子任务数量维度、`overrun` 标志不在事件里，需要 `control.py` 新只读端点，契约见 [SPEC-W8](SPEC-W8-BUDGET-SUBTASK-UI-2026-11-14.md) §6）；⑥ 保留期/GC 与跨机器内容引用未做。见 [tool-admission.md](tool-admission.md)、[resource-kinds.md](resource-kinds.md)、[content-refs.md](content-refs.md)、[budget-ledger.md](budget-ledger.md)、[SPEC-G1-CLOSURE-2026-11-11.md](SPEC-G1-CLOSURE-2026-11-11.md)。
- **⚠ 控制面不是模型隔离边界（F2/exec，2026-11-11 复审确认）**：v1 没有进程级沙箱，`run_command` 以当前 uid 执行子进程；子进程可读 `run/authkey`、连 daemon IPC、跑 `tianshu` CLI，等于能以「人」的身份调控制面并自己落审批。所以"审批需要人"这条在当前实现里只挡**没有** `run_command` 的任务；要真隔离需要进程/文件系统沙箱或独立 OS 用户（G1/exec 的后续工单）。别把 N6 的控制面端点写成"模型无路径"。**F2 收口后仍然成立**：F2 做的是「后端可插拔 + 实测门 + 受信任执行可见」，不是沙箱；`run_command` 仍是同 uid 子进程，四边界探针在本机只有 `process_tree` enforced，任何非空 `isolation_requires` 都会 `unsupported`（见 [F2-sandbox-policy.md](F2-sandbox-policy.md)）。
- **F10 N6（2026-11-11 端点 / 2026-11-12 面板与检索，PR #100 / #126）**：Host/IPC 端点（8 个记忆 RPC）+ **`memory.search`（第 9 个）+ React 记忆面板**都已落地（列出/检索/确认/忘记/擦除；条目带六项判据与 `score`/`score_kind`，`stale` 不可判定就省略；正文纯文本；UI 不做乐观更新、冲突只刷新重读）。**端点仍不是授权边界**：v1 没有进程级沙箱，同 uid 的 `run_command` 能读 IPC 凭据、跑 CLI 并自己落审批——跨本地用户只有 POSIX 文件权限（Windows 命名管道 ACL 未加固、未验证）；"无身份拒绝"在单用户本机模型下**无法构造反例**（只有代码/文档论证，不声称已验证）。
- **§7.1 已交付但仍有边界**：默认关；跨 Run 不复用（材料身份含 `seq`）；不做增量/分层摘要与自动刷新（CLI 未接）；**没有摘要质量评测**（"比确定性裁剪更好"不许宣称）；提示注入未解决（"不是指令"的包装只是提示级缓解，执行侧闸门才是强制点）；纯事件导出不足以回放摘要轮次（缺记录报 problem 不重生成）；本机夹具里摘要**更贵**（token 324→744、请求字符 4054→7887）换关键约束不丢；保留期/GC 未做。见 [context-summaries.md](context-summaries.md) §8。
- **F10 L2/L3（N3，2026-11-11；冲突消解 2026-11-14 W7 更新）**：只落地**确定性**合成，LLM 合成未做（W7 已交付契约增量分析 [SPEC-W7-L2L3-GENERATIVE-2026-11-14.md](SPEC-W7-L2L3-GENERATIVE-2026-11-14.md)——另立契约 + W6 外发 gate 对齐是前置，实现 PR 等在途 #154 落地）。**没有 L2/L3 vs L1 的检索质量评测**——谁都不能宣称"读页比直接读 L1 更好用"；跨作用域合成、对话抽取都没做；**冲突消解（W7，2026-11-14）已落地确定性裁决**：L2 页内对"替换型差异"成员对（同 kind、互不为子集、token 规模相等、重叠系数 ≥0.6）按 origin→seq→memory_id 三级写死规则在页尾标注"以谁为准"，**不删行、不改 L1、不改召回集合**——检测是召回导向的（抓不住低重叠语义矛盾），标注说"候选"不断言"矛盾"，淘汰式处置（覆盖/删除语义）需审批契约、未做；L3 冲突裁决未做（偏好页是人给的，机器按 seq 裁决语义越权 + 预算交互）；L3 仅 user 作用域；执行侧 workspace 守护经派发不可达（纵深冗余，无单独测试）；`memory.consolidate` 不进审批表（`ACTION_CREATE`），将来若做"合成后自动 confirm"必须另立契约。判据 `tests/test_memory_conflict.py` 13 条（现有 `test_memory_distill.py` 22 条零改动），负控 4 条实跑红还原。
- **G2 网络阶段已交付，但边界必须一起读**（2026-11-11，PR #110 / #112；2026-11-18 W9 `v2.0` 补齐防重绑与分页编码）：`outbound_allow` + `network.fetch` + §10.5 联网研究闭环（本机夹具）。**仍不成立**：只支持 GET、**没有检索/搜索 API**（§10.5 的"检索公开来源"在真实意义上未闭环）；**真实公网研究与真实模型复跑未验证**（判据全跑 loopback 夹具）；重定向只许同源；取消只能阻止新请求/新读（在途 TCP 字节无法撤回，系统 `getaddrinfo` 阻塞本身不可被 Python 线程异步打断）；~~响应上限 1 MiB / 摘要 4 KiB，二进制正文只有有损摘要且没有文本分页读取；DNS rebinding / 解析后二次校验未做；每 Run 抓取配置是进程级常量~~**已由 W9 `v2.0` 补齐（2026-11-18，`feat/w9-network-depth`，见 [changelog/2026-11-18-w9-network-depth.md](changelog/2026-11-18-w9-network-depth.md)）：`network.fetch` `2.0` 支持按请求 `max_bytes` 收紧预算预占与内联 `offset`/`length`/`next_offset` 分页预览，`read_content` `2.0` 支持 `utf-8`/`utf-8-replace`/`latin-1`/`base64` 无损分页读取二进制与非 UTF-8 内容，且初始请求与每跳重定向均强制单次 DNS 解析 + 全 RRset 非全局地址拦截（`net.private_address_blocked` 记已知 0 字节）+ `_PinnedHTTP(S)Connection` 数值 `sockaddr` 直连钉死（保留原 `Host` 头、TLS SNI 与 X.509 主机名核验）**；~~无速率限制/重试~~**已交付（2026-11-14，`feat/net-rate-retry`）：连接失败自动重试（只重试 TCP 未建立的 `net.connect`/`net.connect_timeout`，发送后失败绝不重试——服务器端连接数测试钉住授权边界）+ 进程级速率下限（默认 1.0s 可关），见 [SPEC-G2-NET-RESILIENCE](SPEC-G2-NET-RESILIENCE-2026-11-14.md)**；内容分级未实现（审批展示完整 URL 由人判断）；保留期/GC 未做。MCP **已交付**（#116–#119；真实第三方 server 未验证、无沙箱），**记忆阶段也已交付**（#126 `memory.search` + React 面板 = 可查看；#127 `RU-M1@1.0.0` 跨任务召回 = 可证明收益；见 §8 的 2026-11-12 记忆阶段条目）。**子代理 D1 + D2 + D3 已交付**（D1 排队面：`max_subtasks` + `subtasks` 实体/父子链 + 四个工具 + 数量/局部限额 + 预占；D2 执行与收束：Worker 驱动子 Run、父挂起/唤醒、取消传播、父目标 fence、结果回填、回放不重跑；D3 真实用户任务评测：`RU-D1@1.0.0` 只读子任务 + 父任务逐字汇总引用 + 只读边界/失败不编造判据）。D3 是**脚本模型**评测，不是真实模型分数；只覆盖一条公开委派任务 + 一条测试内失败路径。**仍不成立**：~~子任务只读~~（2026-11-14 起默认仍只读、可显式声明文件写，见下方 W10 条目）、深度 1（契约分析已交付，实现待契约 PR）、单机同 Worker 池、取消不退款、读不到输出时摘要为空、~~无 UI 页面~~（**2026-11-14 W8 部分销账**：父 Run 事件面的子任务面板已交付——`subtask.spawned`/`subtask.finished` 合并出限额 / 状态 / 用量，进行中不冒充终态、用量未知不当 0；账户级委派计数视图仍缺 control 端点，见 [SPEC-W8](SPEC-W8-BUDGET-SUBTASK-UI-2026-11-14.md) §6）、Windows 未验证。见 [network-g2.md](network-g2.md) §7 与 [resource-kinds.md](resource-kinds.md) §8。
- **W10 子代理写权限已交付（脚本模型判据），深度>1 与 fork 只交契约分析**（2026-11-14，`feat/subagent-depth-write`）：`subtask.spawn` 2.0 增可选 `write` 布尔声明（红线 8 版本化，缺省 false 与旧行为逐字节一致，RU-D1@1.0.0 样例不动）。`write=true` 的授权上限**恒为父绑定**：子 Task 逐条保留父绑定 mode（父只读的根仍然只读），子 Run 拿到 `effects` 资格（实际拿到可写绑定才给，父全只读则如实降级回 `pure_only`）；子 Run 里每个写动作仍走既有派发审批链（prepare 判定 → 人批准 → 执行侧重判 + 一次性消费 → intent → 守卫内执行 → result + Receipt），与父 Run 完全同构，无免审通道、无权限继承；出网/MCP/exec/记忆写/再委派依旧按既有编号拒绝。判据 12 条 + 负控 3 条（变异红→还原绿），设计与授权论证见 SPEC-W10-SUBAGENT-DEPTH-2026-11-14.md；**授权面改动，合并前必须独立复审**。深度>1（要改 C4 编号语义 + `task.created.depth` 封闭枚举）与 fork（要往子 Run 事件落继承边界）的契约增量四段式见 CONTRACT-C4-SUBTASK-DEPTH-2026-11-14.md，实现留待契约 PR。已知边界：父挂起等子任务时不释放写租约，可写子 Run 首写前要等父租约 TTL 过期走接管对账（正确性无缺口、有等待）；真实模型复跑与 Windows 未验证。
- **G3 未做**：无 Windows / mac-x64 / win-arm64 实机；安装/升级/回退、Keychain、平台 SLO、macOS 13+ 最低版本均未验证。历史 G3 收尾轮 Windows CI 为 **5 failed / 1509 passed / 29 skipped**（交付对账 §3），另有 25 分钟超时记录；这些是当时的失败证据。2026-11-13 修复分支的后续结果见 §2 与 [Windows CI 报告](WINDOWS-CI-2026-11-13.md)，不把历史失败当成当前结论；实机、安装与升级验收仍未完成。
- **数据生命周期**：离线备份与 GC 依靠协作式准入，范围为参与协议的进程。恢复 B/C 支持原路径受管导入、持久隔离、脱敏复核和用户显式解除，具体集成证据见 §2。只读计划或验证通过不证明备份之后的删除历史完整。新库仍按 `user_version=0` 创建，`migrate.SCHEMA_VERSION=2` 是显式迁移的机制版本。恢复解除后的 v1 库仍须先备份再迁移。v2 免侧车进度文件须有匹配实时实体的解除证明。全产品格式与自动升级回退矩阵、换路径恢复、用户设备和掉电持久性验收仍待完成。见[备份与恢复指南](backup-restore.md)。
- **指标**：SLO 阈值未冻结；`rss_mib` 是峰值口径；~~指标文件无轮转~~**指标文件已按大小轮转（2026-11-14，`feat/metrics-rotation`，默认 16 MiB × 4 份、失败降级不丢样本，见 [SPEC-METRICS-ROTATION](SPEC-METRICS-ROTATION-2026-11-14.md) 与 [runtime-metrics.md](runtime-metrics.md) §10；80 MiB 上界是估算默认值不是冻结政策）**；~~`state` 响应可超 IPC 8MiB~~**`state` 响应已按预算装填（2026-11-15，`feat/state-response-bound`：4 MiB 预算内按 tasks→runs→approvals 份额两趟装填 + `tasks_truncated`/`runs_truncated`/`approvals_truncated` 截断标记（加字段不升版本）+ 单条目超预算 fail loud 抛 `internal`，绝不静默截断单条文本；`_approval_context` 改按 pending 集合所在 Run 有界读，state/approvals.list 不再随事件历史全表扫；见 [SPEC-STATE-RESPONSE-BOUND](SPEC-STATE-RESPONSE-BOUND-2026-11-15.md) 与 [M5-desktop.md](M5-desktop.md) §3.4 state 行）**；慢订阅者宽限窗内主循环会从落后游标重读。
- **事件契约**：`oneOf`/`anyOf`/`$ref`/`format` 注册期拒绝；每工具 output 子契约未做；CLI 未区分版本错误文案；历史上从未出现的旧字段组合读作 v0、写入口不识别。
- **评测**：真实模型未跑（无凭据），harness 分数不得当真实模型结论；报告没有「四维总分」（设计如此）；撤销的授权派发链路未进评测（只走 `SnapshotStore.undo` 数据安全预检）。
- **隔离**：探针不覆盖内核攻击面/配额/出站网络；`app_container`/`container` 后端 v1 未实现；本轮只落门，未接产品路径。

**收尾轮的兼容与并发留账（2026-11-11，PR #87–#88）**

- **Windows 仍无实机**：`fsync` 只读句柄降级、`time.perf_counter()` 启动计时、`OpenProcess(SYNCHRONIZE)`+`WaitForSingleObject(0)` 的 pid 判定，旧验收仅记录过当时的 Windows CI runner；当前完整套件证据见 §2（2026-11-13 run 为 Server 2025），不能据此扩大这些专项探针的验证范围；APFS/HFS+ 的目录 fsync 语义、命名管道 `accept`/`CloseHandle` 的唤醒时机、Windows 上的等价"握手期对端关闭"窗口仍未实测。
- **指标读者容错有边界**：只豁免"最后一段没有 `\n` 收尾"（这是正在写），中间行与带换行的坏行照抛 `ValueError`；指标文件没有轮转、没有 fsync 保证，读到的样本可能比磁盘少（不是假数，是"还没写完"）。
- **IPC 的握手期语义是客户端侧映射**：`Client.connect()` 把握手期 `EOFError` 映射成 `ConnectionClosed` 并停止重试；Windows 命名管道上是否会在同一窗口抛别的异常（如 `BrokenPipeError`）未实测，只有 POSIX 真 socket 与跨平台打桩两条回归。
- **一个上报口径错误已改正**：收尾轮曾把 #87 之前新模块的 Windows 失败写成"#50 家族"（见 §5 第一条教训），PR #87/#88 的正文、本文与交付对账都已按实测重写。

**边界与隔离**

- **`Workspace` 是路径边界，不是安全边界**：它回答"能不能碰这个路径"，不隔离进程、不防绕过。
- **`LocalExecBackend().capabilities.isolation == "none"`**：v1 从未交付物理沙箱。`MemBackend` 更弱——它连权限位、owner、符号链接都没有，**不是沙箱**。
- **D 系列约束**：安全不能靠用户配置；默认可写就必须默认可撤销；单用户假设显式声明。

**F12 凭据**

- **Keychain 仅 macOS**：W1 起 darwin 上 `keychain_macos.py` SecItem 直调已落地（CI 与本机真实通过）；桌面控制面 `credential.set / delete` 仅在可写凭据后端（如 macOS Keychain）可用、永不回显密钥值，其余平台或只读 `env` 后端如实报 `unsupported`。**不得对未实现平台声称"密钥存在系统钥匙串里"。**
- 真实模型 key 只从环境变量或系统钥匙串读，永不进 SQLite / JSON 配置 / 事件日志；用过的中转 key 应自行作废。

**M0–M5 复审留账（[DEBUG-M0M5.md](DEBUG-M0M5.md)，2026-11-10；修复记录见该文档末尾）**

- **M2 静态疑点 → issue #53**：否决缓存只按 `run_id` 生效（同一 Task 新 Run 是否忘掉否决，取决于 `run start` 是否只由人/控制面触发，未逐条验证）；`revoke_consumed` 的判定依赖 dispatcher 状态核验，未逐条探针验证。**两条都未复现**，不许当成已确认漏洞，也不许当成已解决。
- **M1 `delete_file` 注入矩阵 → issue #54**：本轮（PR 修法）把 "kill×100" 的证据面从只覆盖 create 扩到 create + 覆盖写；删除类动作的恢复判据是 `absent`、撤销按 preimage 重建，另开一组断言跟进。
- **SQLite TTL 必须从真正取得写锁后起算**：claim、heartbeat、enqueue-and-claim 与
  lease renew 若在 `BEGIN IMMEDIATE` 之前采样时钟，锁等待时间不计入新的 deadline /
  expires；另一进程持锁超过 TTL 时，续期即使返回成功，也可能刚提交就过期并允许
  接管。确定性证据必须用真实子进程持写锁，并用事件屏障在锁等待中推进注入时钟。
  旧实现四路均写成 `1000/1400`，修复后四路为 `2000/2400`。这条不能与原始 Windows
  失败现场自动等同：现场没有 SQL 时间线，只能记为可复现机制与候选修复。
- **M4 未验证项 → issue #55**（真实换模对比 / Windows `evaluate` / memfs 评测路径 / 真超时）：2026-11-10 真实模型轮已销账三条，**只剩 Windows 实机一条**（2026-11-10 时点口径。**2026-11-14 对账**：2026-11-11 判据升 m4.5 后，真实换模复跑重新成为未决项——现剩 Windows 实机 + m4.5 真实复跑两条，见下文 issue #55 行与 issue 本体评论）。
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

**桌面闭环的当前未验证项 / 已知限制（M5 历史边界见 [M5-desktop.md](M5-desktop.md) §2/§10 与 [M5-verification.md](M5-verification.md)）**

- **未验证**：Windows/Linux 桌面用户设备（R1 只有 Windows Rust CI，不能替代 WebView2、命名管道、杀软/ACL、长路径用户环境实测）· macOS x64 与 Windows ARM64 · 真实模型驱动 UI（演示与验收仍用 `scripted` 模型）· R1 安装/升级/回退、签名、公证与自动更新 · 同一毫秒的并发审批压测（只验证了先后顺序）。macOS arm64 的 R1 Tauri debug App + 真实 Host/Daemon/WebView 生命周期已实测，但它不是安装产物，也不是正式发布验收。
- **Daemon 生命周期不写事件日志**：`shutdown(reason)` 只进 `logs/daemon.log`。C3 是封闭集合且每条事件挂 `run_id`，新增 `daemon.*` 族是契约变更，代价大于收益。
- **陈旧锁判定有 2 秒宽限窗**：第一个 Daemon 在「拿到锁 → bind」之间超过 2s（超大库恢复扫描）时，第二个实例理论上可能误判陈旧并接管。跨平台读取进程启动时间没有 stdlib 方案，属单独工单。
- **推送在主循环里同步发送**：一个不读数据的订阅者理论上能拖慢 RPC 响应；M5 用"本地 socket + 每批 256 条"把风险压在可接受范围，没做独立发送线程 + 非阻塞写。**2026-11-14 对账**：本条已被 PR #81 取代——独立发送线程与有界队列已交付（`daemon.py` `SEND_QUEUE_MAX=1024`；慢订阅者/队列溢出/重连负控 `tests/test_slow_subscriber.py`），与本文 §2/§5/§7 其他提及处一致。
- **驱动线程卡死 >5 秒时刻意不跨线程关连接**：关闭正被别的线程使用的 SQLite 连接只会得到没人看得到的 `ProgrammingError`，还可能截断写入；保留连接交给进程退出回收（测试钉住"shutdown 仍返回、锁已释放、没有 ProgrammingError"）。
- **`approvals.decide` 的 `reason` 被忽略**：审批记录没有自由文本字段，要存就得改 `schemas/m2/approval.json`（契约变更）；当前只用于 UI 展示。
- **R1 源码候选不是可安装 App**：已有未签名 Tauri 薄壳、托盘和单实例代码，尚未合入 main；锁定 Python runtime、开机自启、安装包、签名、公证、自动更新仍属于 R2/R3 或未实现范围。
- **R2 build 恢复协议尚未跨平台实机验收**：macOS arm64 本机已覆盖 fake-runtime bundle、两个 rename failpoint、journal 前真 child 硬退及重启幂等；Git source 的 Windows 危险 component 只用 `ntpath` 归一化验证，cache 祖先只验证 POSIX symlink。这不证明 Windows 原生 directory lock/rename、symlink/junction、ADS/设备名写盘、macOS x64、Windows x64/arm64 或断电级 durability。`_validate_directory_chain()` 的检查与随后 mkdir/write 仍存在理论 TOCTOU；发布目录替换也仍是两段 rename，读者可能观察到短暂无 output。`.bundle.publish.lock` 成功后会持续存在，是锁锚点，输出 parent 必须放在不被 Git 跟踪的构建目录。构建不宣称跨平台物理原子替换。

**平台**

- **无 Windows 实机证据**：当前证据来自 GitHub `windows-latest`，2026-11-13 已验证 run 使用 Windows Server 2025；没有 Windows 10/11、ARM64 用户设备或安装体验验证。
- **UI / Windows 合并门禁缺口已关闭（2026-11-13）**：远端现强制 Ubuntu、macOS、Windows、UI 四项检查（完整名称见 §2）；仍保留 strict、enforce_admins、禁强推/删除与 0 人批准。Windows 实现提交 `4bb1781` 已通过完整 CI；后续提交须取得自己的结果。耗时接近 25 分钟上限仍是可靠性风险，不能因设为 required 就宣称长期稳定或实机验证完成。
- **Windows IPC 握手失败根因已复现（issue #50，2026-11-13）**：`load_authkey()` 新建密钥未指定 `O_BINARY`，Windows 文本模式把随机 LF 改成 CRLF，创建者与后续读取者拿到不同密钥。固定字节回归在 Windows run `43281774439` 明确失败；本分支已改为二进制写入并验证旧密钥不轮转。此前「生产不受影响、只需测试重试、必须有实机才能定位」的结论撤回：这是生产密钥持久化缺陷，GitHub 托管 Windows 已提供复现。修复提交 `4bb1781` 的完整矩阵已通过（run `43283015540`）；见 [Windows CI 修复证据](WINDOWS-CI-2026-11-13.md)。
- **CI 作业有 25 分钟上限**：GitHub 默认 360 分钟，挂住的测试会占 6 小时 runner 且不给任何信息（M5 实测踩过）。
- **不得声称 main 历史上每个提交都绿**：`154b916` 那次 macOS job 排队 15 分钟后被取消（`runner_name` 为空、`steps` 为空），是 runner 供给问题，同提交的 ubuntu / windows 都绿。

**数据落点**

- **两套落点并存，别混写**：
  - `daemon` / `host`：默认平台标准目录（`ipc.default_data_root()`，`TIANSHU_DATA_ROOT` 覆盖）——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`；目录内含 `events.db`、`artifacts/{blobs,snapshots}`、`run/`、`logs/daemon.log`。
  - 内核子命令（`run` / `task` / `approvals` / `undo` / `schedule` …）：**默认仍是当前工作目录下的 `.tianshu/events.db`**（`cli.DEFAULT_DB` 相对路径，靠 `.gitignore` 排除），可用 `--db` 指定路径，或传 `--desktop`（/ `TIANSHU_USE_DESKTOP_DB=1`）直接桥接到桌面端 `ipc.default_data_root() / "events.db"`（禁止与显式 `--db` 混用）；当工作目录不存在 `.tianshu/events.db` 而桌面库已存在时，交互终端 stderr 会打印单行提示。
  - 因此"运行时数据默认放系统标准位置"这句话**仍只对桌面入口成立**；写文档时必须带主语，别写成无条件全局结论。

**仍开着的问题**

- ~~**issue #27**~~：**已收敛关闭**（2026-11-10 对账销账）。
- ~~**issue #40**~~：**已修**（否决即终局，`c4.approval_rejected`，`SUITE_VERSION` 升 `m4.4`，PR #48 合入 `feat/m5-desktop`；判据与限制更新见 [M4-verification.md](M4-verification.md)）。
- **#145 / #147 已关闭**。锁忙与审批后删除的回归修复随 [#195](https://github.com/lorek666/tianshu-agent/pull/195) 合入，见 §2。
- **#200 继续跟踪 Windows 目录持久化**。在现有屏障能力下，受管恢复写入及含文件候选的 W15 批次会拒绝执行。参见 [#200](https://github.com/lorek666/tianshu-agent/issues/200)。
- **#55 继续跟踪真实模型与 Windows 实机复验**。真实换模已在 `m4.4` 实跑。`m4.5` 的历史复跑遇到中转站 `503 / auto groups is not enabled`。当前 `SUITE_VERSION` 为 `m4.6`，对应的真实模型报告及 Windows 实机复验仍待取得。参见 [M4 实测记录](M4-verification.md) §7.4 和 [#55](https://github.com/lorek666/tianshu-agent/issues/55)。
- ~~**issue #57**~~ 已随 PR #68 关闭。该轮引入 `Check.kind`（completion/safety）、作用域化的判据和完成度/安全分栏，套件当时升到 `m4.5`。
- ~~**issue #58**~~：**已修（2026-11-11，PR #67）**：`RunResult.model_attempts`（真发一次请求才 +1，含超时/失败/重试）+ `cli._cost_note` 统一口径；全失败时打印「0 个完成轮次 / 3 次尝试（费用未知）」，不再说"没有模型调用"。
- ~~**issue #53 / #54**~~：**已销账（2026-11-11）**。疑点 1 不成立（模型无法启动 Run，写成设计前提 + 三层钉子）；疑点 2 三条路径探针成立；delete 注入矩阵补齐并暴露一个真 bug（见 §5）。
- ~~**issue #61 / #63**~~：**已修**（见上一段与 PR #69/#70）。

**实现上的疤**

- ~~`run.end` 不带**货币成本**汇总~~：**已修（2026-11-18）**：`run.end` 升为精确 payload `v2`，新增 `cost_total_micros`（已知计价填整数微美元，单价全未知填 `null` 绝不冒充 `0`）与 `cost_unknown_calls`，保留历史 `v0/v1` 只读解释。
- `check_exec()` 仍是低层 API，`spawn` 已不再调用它。
- 多工作区相对路径解析仍绑定到第一个可写工作区。
- **五道契约 Schema 已齐备**：`schemas/` 下现含 `c1/model.json`、`c2/tool.json`、`c3/event.json`、`c4/rules.json`、`c5/eval.json`、`m2/{approval,grant,job,run,task}.json` 与 `data/restore-v1.json`，并由 `tests/test_c1_c2_c5_schemas.py`、`tests/test_event_schema.py`、`tests/test_rule_ids.py` 对真实代码对象做正负控双向绑定校验（注：PRD §5 完整冻结条件还包含真实第二家线上 provider 实跑验证，在 #55 真实模型复跑完成前不在文档中夸大跨线上服务商实测范围）。`data/restore-v1.json` 是源码 schema，`data/` 的运行时忽略规则必须保留其明确例外。

**本地环境提醒（非项目事实，只对所有者本机成立）**

- 本仓库位于外置卷 `/Volumes/Date`：**务必经常 `git commit`、尽早 push**（卷拔掉就没了）。`refs/`（参考仓库，约 762MB）已在 `.gitignore` 里，**不要入库**。

## 8. 变更与会话记录（已拆分为独立目录）

为消除单文件合并冲突，历史会话流水与新增批次记录已迁至 [`docs/changelog/`](changelog/README.md)：

- 历史会话日志全集（原 §8）：[docs/changelog/archive-2026-11-sessions.md](changelog/archive-2026-11-sessions.md)
- 历史逐 PR 状态与 CI 证据（原 §2）：[docs/changelog/archive-2026-11-status.md](changelog/archive-2026-11-status.md)
- 新增记录请按 [docs/changelog/README.md](changelog/README.md) 规范写入独立碎片文件。

## 9. 权威文档索引

- **记忆交付验收**：[MEMORY-ACCEPTANCE-2026-11-11.md](MEMORY-ACCEPTANCE-2026-11-11.md)（逐条判据↔测试 + 负控总表 + 未验证 10 项；数字以 `main @ c4b020a` 实测为准）。

- [PRD-v1.md](PRD-v1.md) — 产品需求与架构（唯一权威）：范围、契约、红线、里程碑、决策记录
- [TECH-ARCH.md](TECH-ARCH.md) — 技术架构：选型、进程模型、打包、平台差异
- [REVIEW-PRA.md](REVIEW-PRA.md) — PR #7 独立复审报告（§5 教训的主要来源）
- [DEBUG-M0M5.md](DEBUG-M0M5.md) — M0–M5 对抗性复审报告 + 本轮修复记录（裁决、命令、实测输出、残余风险）
- [M5-desktop.md](M5-desktop.md) — M5 桌面闭环：三进程拓扑、IPC 契约、恢复语义、偏差（§2）与已知限制（§10）
- [M0-verification.md](M0-verification.md) / [M1-verification.md](M1-verification.md) / [M2-verification.md](M2-verification.md) / [M3-verification.md](M3-verification.md) / [M4-verification.md](M4-verification.md) / [M5-verification.md](M5-verification.md) — 各里程碑验收证据
- [F5-context-compile.md](F5-context-compile.md) — F5 上下文编译的设计与取舍
- [network-g2.md](network-g2.md) — G2 网络阶段：任务级外发白名单 + `network.fetch` 执行器（两道门、执行侧规则、字节账本、内容引用、回放不联网、判据与负控、诚实边界）
- [SPEC-G2-NETWORK-2026-11-11.md](SPEC-G2-NETWORK-2026-11-11.md) — G2 网络阶段的施工规格（三个增量、判据、负控、口径变更）
- [context-summaries.md](context-summaries.md) — §7.1 模型语义摘要：为什么确定性裁剪不够、记录层 / 注入 / 回放 / 生成路径、崩溃恢复、长任务实测与诚实边界
- [F10-memory-design.md](F10-memory-design.md) — F10 本地记忆设计（v1.1）：投影而非事实源、L0–L3 分层、作用域与装备、默认私有；**§6.1 资源种类（`memory://` scheme）**、**§13 契约增量的实际状态**、**§14 判据↔真实测试映射**
- [SPEC-RESIDUALS-MEMORY.md](SPEC-RESIDUALS-MEMORY.md) — 剩余记忆工作量施工规格（**待实施**）：§13 #18–#27 逐条现状与设计、六个工作流、契约增量清单、判据与负控口径、明确不做
- [F1-exec-primitive.md](F1-exec-primitive.md) / [exec-run-command.md](exec-run-command.md) — F1 执行原语与 `run_command` 工具
- [REFS.md](REFS.md) — 参考仓库与固定 commit
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — 分支、提交、独立复审规范
- [../AGENTS.md](../AGENTS.md) — AI 开发强制指令
- [ROADMAP-COMPLETE-AGENT-2026-11-14.md](ROADMAP-COMPLETE-AGENT-2026-11-14.md) — 全量工作流账本与排期状态
- [changelog/README.md](changelog/README.md) — 分批变更记录与历史会话归档
