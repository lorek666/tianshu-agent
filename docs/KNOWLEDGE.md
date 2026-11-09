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
- **当前形态**：命令行内核（M0–M4 已完成并合并）。桌面三进程 + 图形 UI 在 M5，**尚未开始**。**别把 README 的愿景当成已实现的 UI。**
- **核心承诺**：事件可重放、执行侧授权、覆盖/删除需审批、默认可撤销、崩溃可恢复。
- **非目标（v1）**：图片/视频生成、多租户、防本机用户篡改（只防模型越权与失控 worker）。
- **技术栈**：Python 3.12（`requires-python = ">=3.12,<3.13"`）、`uv`、**零第三方运行时依赖**（`dependencies = []`）；dev 依赖只有 `pytest>=8.0` 与 `ruff>=0.6`。
- **版本 / 许可**：`0.1.0`；Apache License 2.0（`LICENSE` 已被 git 跟踪）。

## 2. 当前状态（数字会过期，更新时重核）

- **里程碑**：**M0–M4 已完成并合并**（M4 由 PR #41 / #45 合入，代码在 `src/tianshu/eval/`）；**M5（桌面闭环）未开始**。
- **测试**：**942 passed, 3 skipped**（macOS 本机，`uv run --frozen pytest -o addopts="" -q`）；测试文件 33 个 `tests/test_*.py`。注意 `pyproject.toml` 的 addopts 已带 `-q`：直接 `uv run pytest` 会正常打印汇总；但**再在命令行补一个 `-q`**（叠成 `-qq`）汇总行就没了，那时要 `-o addopts="" -q` 才行。三种情况都实测过。
- **代码规模**：`src/tianshu/*.py` **32** 个模块，另有子包 `eval/`（6 个 .py）。
- **平台**：CI 矩阵是 **ubuntu / macos / windows 三个平台 × Python 3.12**，`7abad75` 的 run `42652194348` 三平台全 success。**Windows 在矩阵里且是绿的**（issue #21 已 closed），但只有 `windows-latest`（Server 2022 runner）证据，没有实机验证。
- **门禁**：`main` 已开分支保护——必须走 PR、`strict`（分支须与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推与删除。required status checks 目前是 `test (ubuntu-latest) · py3.12` 与 `test (macos-latest) · py3.12` 两项；Windows job 在矩阵里跑但**还没加进 required 列表**（见 §7）。
- **CI 步骤**：`checkout` → `setup-uv` → `uv python install` → **`uv lock --check`（锁文件漂移门禁）** → `uv sync --frozen` → `uv run --frozen ruff check .` → `uv run --frozen pytest -o addopts="" -q`。权限 `contents: read`，`fail-fast: false`。
- **Windows 跳过项**：Windows 上按平台跳过 8 项（memfs 符号链接、POSIX PID 探活、POSIX 权限位 ×2、无 PTY ×2、无进程组/信号 ×2），跳过原因写在跳过消息里。
- **模型**：两个线上协议（OpenAI-compatible、Anthropic Messages），各一个 adapter，主循环只认内部规范化形状；契约测试在 `tests/test_model_contract.py`。

## 3. 架构速览

- **唯一事实源**：SQLite WAL append-only 事件日志（触发器禁 UPDATE/DELETE）。Task/Run/Approval/Grant/Lease 都是同库事务更新的可重建投影。
- **Task ≠ Run**：Task 是用户看到的那件事，Run 是一次执行；改目标会 fence 旧 Run。
- **授权只在执行侧**：模型输出/工具输出/MCP 注解都不构成授权依据；dispatcher 是唯一派发入口，执行前登记 intent、执行后写 result + Receipt。
- **五道契约**（版本化；冻结条件＝字段级 JSON Schema 可校验 + 契约测试全绿 + 第二家 provider 验证过）：
  | ID | 名称 | 当前状态（只写可核实的） |
  |---|---|---|
  | C1 | Model I/O | 两个线上协议各一个 adapter；结束原因是封闭集，越界抛 `ModelError` |
  | C2 | Tool | 7 个工具、6 个对模型开放；`tool_manifest()` + 每种工具的参数要求由工具自己声明 |
  | C3 | Event | **已封闭**：41 个事件类型，写入侧拒未登记类型（只校验**类型名**，不校验 payload 字段）；声明了但从未发射的类型 **0** 个 |
  | C4 | Permission/Approval | **rule id 已封闭**：共 42 个——**拒绝原因 35 个** + 非拒绝判定 5 个（`c4.auto` / `c4.overwrite` / `c4.delete` / `c4.exec` / `c4.outside_workspace`）+ 保留未使用 2 个（`c4.unhandled` / `dispatch.denied`）；写入前校验。审批/凭据见 PRD §5.4 |
  | C5 | Eval | `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）已分离，评测套件 `m4.3` |
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
- **模块地图**：`src/tianshu/` 32 个模块 + `eval/` 子包。按功能分：事件与状态（`events`/`tasks`/`jobs`/`leases`/`snapshot`/`recovery`/`replay`）、授权与派发（`permissions`/`rules`/`dispatcher`/`guard`/`receipt`）、模型与执行（`model`/`capability`/`context`/`exec`/`execpolicy`/`credentials`/`pricing`/`sandbox`）、工具与路径（`tools`/`workspace`/`pathkeys`/`fsutil`/`canonical`）、主循环与入口（`loop`/`worker`/`cli`/`errors`/`crashpoint`）、评测（`eval/`）。细节见 PRD / TECH-ARCH。
- **CLI 子命令**（实测 `tianshu --help`）：`run` / `task` / `worker` / `recover` / `undo` / `approvals`（别名 `approval`）/ `replay` / `evaluate`。
  - `worker` 是**一次性 drain**——队空或撞上被占用的工作区就退出，**不是常驻守护进程**。
  - `replay` 审计不调模型；`evaluate` 换模跑固定 case。
  - 退出码：`0` 成功 / `1` 失败或取消 / `2` 前置条件错 / `3` 需要人决定（等审批、已暂停、待核查、`undo` 未带 `--approve` 的预演）/ **`4` = evaluate 判 unsafe**（安全门槛一票否决）。

- **已实现的功能面**（PRD §7 编号 → 落地模块，模块说明取自代码）：
  - **F1 执行原语** → `exec.py`（argv 模式，非 shell；超时/截断/进程组）
  - **F2 执行策略** → `execpolicy.py`（默认全拒）
  - **F5 上下文编译** → `context.py`（纯函数、按字节预算、丢弃只记账不摘要）
  - **F9a 模型准入** → `capability.py`（能力声明与准入，只用于路由，不用于授权）
  - **F12 凭据** → `credentials.py`（分域查找 + 全链路脱敏；**Keychain 后端 v1 未实现**，如实报 `unsupported`）
  - **F13 成本** → `pricing.py`（整数微美元；未知价格记 `None` 并单独计数）
  - 其余 P0 功能落在：`events` / `recovery`（事件与恢复）、`tools`（工具系统）、`permissions`（授权审批）、`tasks` / `jobs` / `leases`（任务与运行）、`snapshot` / `receipt`（产物与回滚）
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

## 5. 踩坑教训（别再踩）

- **自审有盲区**：PR #7 自写自审自合，第二读者十分钟找出 13 项缺陷、5 项高危。作者知道自己的意图，所以看不出实现没兑现意图。→ 高危改动必须独立复审（见 §6）。
- **测试证据会造假**：曾用「同进程两条连接」冒充两个进程、用 `sleep(0.05)` 猜时序、用手写替身盖住真洞。→ 并发要真子进程 + 屏障，时序用注入时钟，契约断言用真实 `tool_manifest()` / 真实 schema。
- **文档会说假话**：README/PRD/复审报告多次与代码漂移，出现专门「修假话 / 对账」的提交。→ 改代码同步改文档；复审报告写错归因要按事实重写。
- **「放在系统标准位置」这类话要能指到代码行**：README 长期写着「运行时数据放在系统标准位置」，而 `cli.DEFAULT_DB` 一直是**当前工作目录下的 `.tianshu/events.db`**（相对路径，靠 `.gitignore` 排除），`~/Library/Application Support/AgentOS/` 那套只存在于 TECH-ARCH、属 M5 设计。这条假话过了多个里程碑没人发现——教训：凡是「放在哪里 / 什么时候清理 / 谁负责」的运行时行为陈述，都要能指出是哪一行代码；指不出来，就说明它还没实现。
- **事务要可组合**：各 store 各自 `BEGIN IMMEDIATE` 会嵌套报错；统一走 `EventLog.transaction()`（已在事务中则复用）。
- **父子目录需要互斥**：`(st_dev, st_ino)` 只答「是否同一目录」，`/ws` 与 `/ws/sub` 是同一棵树，须 ancestor/descendant 一律冲突。
- **租约/fence 必须释放**：Run 收尾或被 fence 后要在同一事务释放租约，否则新 Run 被自己上一个 Run 挡住。
- **进程身份 ≠ pid**：pid 会复用，用 `ProcessIdentity{instance_id, pid, started_at}`；外来实例不得过守卫/续租/释放。
- **封闭集要强制**：`finish_reason` 曾出现越界值 `error`/`unknown` 靠 `type: ignore` 压着；改为封闭集 + `ModelError` 对象，越界即报错。同一条道理后来复用到 C3 事件类型与 C4 拒绝原因上。
- **状态必须是事件的投影**：直接写表不写事件，回放就无法解释为什么被 fence。
- **「定义了」不等于「会产生」**：一个 id/类型可能只在代码里有定义、没有任何发射点。登记契约时要么给出产生路径，要么显式标注「保留未使用」，不能让它默默躺在集合里冒充已实现。

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

**M4 评测未验证项（不许写成"通过"）**

- 真实换模（两个真实 provider 的分数对比）**未跑**；
- Windows 上的 `evaluate` **未单独验证**；
- `memfs` 后端**未覆盖**；
- 真超时**未覆盖**。
- 详见 [M4-verification.md](M4-verification.md)。

**平台**

- **无 Windows 实机证据**：只有 `windows-latest`（Server 2022 runner）的 CI 结果。
- **Windows 未设为 required status check**：必过检查只有 `test (ubuntu-latest) · py3.12` 与 `test (macos-latest) · py3.12` 两项，**Windows 红了不会挡合并**。而 `.github/workflows/ci.yml` 自己写着 Windows 是**主打平台**——这是已知门禁缺口，建议 Windows 稳定后把 `test (windows-latest) · py3.12` 补进 required 列表。
- **不得声称 main 历史上每个提交都绿**：`154b916` 那次 macOS job 排队 15 分钟后被取消（`runner_name` 为空、`steps` 为空），是 runner 供给问题，同提交的 ubuntu / windows 都绿。

**数据落点**

- **系统标准数据目录未实现**：CLI 的事件库默认在**当前工作目录下的 `.tianshu/events.db`**（`cli.DEFAULT_DB` 是相对路径，靠 `.gitignore` 排除）。`docs/TECH-ARCH.md` 里的 `~/Library/Application Support/AgentOS/` / `%LOCALAPPDATA%\AgentOS\` 系统标准布局（daemon socket、缓存、日志）属 **M5 设计，`src/` 里一处都没有**。所以：任何「运行时数据放系统标准位置」的说法现在都是假的。

**仍开着的问题**

- **issue #27**：PRD §4.1 关键字段列与实现双向漂移 11 处，需逐条决定收敛方向。
- **issue #40**：审批被否决后 `resume` 会重新请求审批——缺少「拒绝并终止这次动作」语义。

**实现上的疤**

- `run.end` 不携带成本汇总（成本在 `run status` 里看；约 6 行改动挂账）。
- `check_exec()` 仍是低层 API，`spawn` 已不再调用它。
- 多工作区相对路径解析仍绑定到第一个可写工作区。
- 契约未全冻结：`schemas/` 下只有 `c3/event.json`、`c4/rules.json` 与 `m2/{approval,grant,job,run,task}.json`，C1/C2/C5 **没有**字段级 schema——补齐并加契约测试之前，别宣布冻结。

**本地环境提醒（非项目事实，只对所有者本机成立）**

- 本仓库位于外置卷 `/Volumes/Date`：**务必经常 `git commit`、尽早 push**（卷拔掉就没了）。`refs/`（参考仓库，约 762MB）已在 `.gitignore` 里，**不要入库**。

## 8. 会话更新日志（AI 每次追加，勿删历史）

> 格式：`日期 · 做了什么 · 发现/决定了什么`。最新在上。

- **2026-11-10** · 把未提交的草稿按 `7abad75` 事实重写并落库 · 发现草稿里 Windows 和测试数两处都是假话，属"文档说假话"复发；落库时又抓到第三处——README 长期声称「运行时数据放系统标准位置」，实际一直是 CWD 下的 `.tianshu/`。
- **2026-11-09** · 扩充 AGENTS.md 开发规则：新增测试纪律 / 数据与事件完整性 / 安全与授权 / 契约与类型纪律 / 依赖与平台 / 提交与仓库卫生 / 反模式共 7 节。· 规则全部溯源 PRD 红线与 REVIEW-PRA 教训，不写空话；固化「红就是红，不装作绿」（不用 continue-on-error / type: ignore 假绿）。
- **2026-11-09** · 建立本知识库 + AGENTS.md 强制读写钩子；README 重写为标准开源样式；`main` 配置分支保护（PR + CI 绿 + enforce_admins + 禁强推）。· 确认现状：M0–M3 完成、767 测试、Windows CI 暂摘（#21）；把「自审盲区 / 测试证据造假 / 文档假话」三大教训固化进流程。

## 9. 权威文档索引

- [PRD-v1.md](PRD-v1.md) — 产品需求与架构（唯一权威）：范围、契约、红线、里程碑、决策记录
- [TECH-ARCH.md](TECH-ARCH.md) — 技术架构：选型、进程模型、打包、平台差异
- [REVIEW-PRA.md](REVIEW-PRA.md) — PR #7 独立复审报告（§5 教训的主要来源）
- [M0-verification.md](M0-verification.md) / [M1-verification.md](M1-verification.md) / [M2-verification.md](M2-verification.md) / [M3-verification.md](M3-verification.md) / [M4-verification.md](M4-verification.md) — 各里程碑验收证据
- [F5-context-compile.md](F5-context-compile.md) — F5 上下文编译的设计与取舍
- [F1-exec-primitive.md](F1-exec-primitive.md) / [exec-run-command.md](exec-run-command.md) — F1 执行原语与 `run_command` 工具
- [REFS.md](REFS.md) — 参考仓库与固定 commit
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — 分支、提交、独立复审规范
- [../AGENTS.md](../AGENTS.md) — AI 开发强制指令
