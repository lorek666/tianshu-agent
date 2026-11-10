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
- **测试**：**1061 passed, 3 skipped**（macOS 本机实测，2026-11-10，`main@6862d8f`：`uv run --python 3.12 pytest -o addopts="" -q` → `1061 passed, 3 skipped in 96.66s`；`uv run --python 3.12 ruff check .` → `All checks passed!`）；测试文件 **39** 个 `tests/test_*.py`。M5 新增 6 个测试文件：`test_ipc` / `test_host` / `test_control` / `test_daemon` / `test_integration_m5`，以及 issue #40 修复带的 `test_approval_reject`；五个 M5 文件共 **110** 条（ipc 26 / host 28 / control 27 / daemon 19 / integration_m5 10），`test_approval_reject` 9 条（均实测 `--collect-only`）。**Windows 的数字本轮没有重核**——无 Windows 实机，只有 CI `windows-latest` 的结论，别把 macOS 数字写成三平台通用。注意 `pyproject.toml` 的 addopts 已带 `-q`：直接 `uv run pytest` 会正常打印汇总；但**再在命令行补一个 `-q`**（叠成 `-qq`）汇总行就没了，那时要 `-o addopts="" -q` 才行。三种情况都实测过。
- **代码规模**：`src/tianshu/*.py` **37** 个模块（M5 新增 `ipc` / `host` / `daemon` / `control`，并把撤销从 `cli` 提成库 `undo`），另有子包 `eval/`（6 个 .py）。
- **平台**：CI 矩阵是 **ubuntu / macos / windows 三个平台 × Python 3.12**，另有一个 `ui` job（ubuntu：typecheck / vitest / build / `ui/dist` 漂移门禁）。Python 三平台：`7abad75` 的 run `42652194348` 三平台全 success；**M5 之后 Windows 不再是绿的**——命名管道上存在**偶发握手失败**（issue #50，`digest sent was rejected` / `digest received was wrong`，每轮失败的用例与条数都不同，本机 macOS 无法复现）。ubuntu / macOS 稳定绿。Windows 只有 `windows-latest`（Server 2022 runner）证据，没有实机验证。
- **门禁**：`main` 已开分支保护——必须走 PR、`strict`（分支须与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推与删除。required status checks 目前是 `test (ubuntu-latest) · py3.12` 与 `test (macos-latest) · py3.12` 两项；Windows job 在矩阵里跑但**还没加进 required 列表**（见 §7）。
- **CI 步骤**：`checkout` → `setup-uv` → `uv python install` → **`uv lock --check`（锁文件漂移门禁）** → `uv sync --frozen` → `uv run --frozen ruff check .` → `uv run --frozen pytest -o addopts="" -q`。权限 `contents: read`，`fail-fast: false`。
- **Windows 跳过项**：按平台跳过的是 memfs 符号链接、POSIX PID 探活、POSIX 权限位 ×2、无 PTY ×2、无进程组/信号 ×2 这几类（上一版记 8 项，本轮无 Windows 环境，**未重核条数**）；跳过原因写在跳过消息里，CI 用 `-rs` 打进日志。
- **M5 新入口**：`tianshu daemon`（常驻守护进程）与 `tianshu host run|stop|status`（桌面外壳）。运行数据根默认平台标准目录，`TIANSHU_DATA_ROOT` 覆盖；**旧的内核子命令（`run` / `task` / `approvals` / …）仍默认 CWD 下的 `.tianshu/events.db`**（`cli.DEFAULT_DB`，`--db` 可改）——两套落点并存，文档必须分开写。
- **数据目录**：`ipc.default_data_root()` 已把平台标准位置实现出来——macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`（不用 Roaming：事件日志会撑爆漫游同步）、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`）。目录内：`events.db`、`artifacts/{blobs,snapshots}`、`run/`（IPC 端点 / `ipc.key` / 锁）、`logs/daemon.log`（Host 重定向的 daemon stderr）。测试与 CI 一律显式传 `--data-root` 到 tmp，**不允许**写用户主目录。
- **模型**：两个线上协议（OpenAI-compatible、Anthropic Messages），各一个 adapter，主循环只认内部规范化形状；契约测试在 `tests/test_model_contract.py`。桌面演示与验收默认 `scripted` 模型（不联网、不花钱）。

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

**实现上的疤**

- `run.end` 不携带成本汇总（成本在 `run status` 里看；约 6 行改动挂账）。
- `check_exec()` 仍是低层 API，`spawn` 已不再调用它。
- 多工作区相对路径解析仍绑定到第一个可写工作区。
- 契约未全冻结：`schemas/` 下只有 `c3/event.json`、`c4/rules.json` 与 `m2/{approval,grant,job,run,task}.json`，C1/C2/C5 **没有**字段级 schema——补齐并加契约测试之前，别宣布冻结。

**本地环境提醒（非项目事实，只对所有者本机成立）**

- 本仓库位于外置卷 `/Volumes/Date`：**务必经常 `git commit`、尽早 push**（卷拔掉就没了）。`refs/`（参考仓库，约 762MB）已在 `.gitignore` 里，**不要入库**。

## 8. 会话更新日志（AI 每次追加，勿删历史）

> 格式：`日期 · 做了什么 · 发现/决定了什么`。最新在上。

- **2026-11-10** · **M5 合并后的收尾（PR #49 已合，PR #51 补丁）**：`tianshu host stop` 三缺陷、Windows/Linux 平台差异、UI 三缺陷、以及"挂住"问题全部修完；`main@6862d8f` 实测 **1061 passed / 3 skipped**。· **踩了**：`/shutdown` 端点挂在 `do_GET` 而 `host stop` 发 POST（永远 404）；清理回调赋给 handler **类属性**被描述符协议绑成 bound method（`TypeError` 静默吞掉清理）；`stop_all` 先置 `stop_event` 再清理 → Daemon 留成孤儿；Windows 的 accept 叫不醒（overlapped I/O）、`PeekNamedPipe` 直接抛错、重复实例抛裸 `PermissionError`；Linux 上关 fd 唤不醒 accept；**敲门自己也会阻塞**（windows job 挂 20 分钟）；子进程日志用未读的 `PIPE` 会死锁；崩溃恢复测试靠轮询 probe 猜进度导致 macOS 偶发红。· **留下**：`docs/M5-verification.md` §5（host stop）、§6（断线烧核）、§6.5（跨平台差异与 Windows 未解决问题）；issue **#50** 记录 Windows 命名管道偶发握手失败（根因未定位，已在 `ci.yml` 就近注明、**不加** `continue-on-error`）；UI 三个可见缺陷已修（任务状态/守护进程版本人话映射、1280 宽审批按钮回到首屏），遗留"审批事件 action≠rule id 导致标题兜底"另开工单。
- **2026-11-10** · **M5 桌面闭环（PR #49）**：三进程（Daemon / Host / 浏览器 UI）打通，IPC 契约与断线语义、审批 UI、关 UI 任务继续、重启恢复、离线演示脚本全部落地；测试 942 → **1060 passed / 3 skipped**（`f5da9ef` 收尾后实测）。· **踩了**：`recv()` 对"超时"和"断线"都返 `None`，客户端一断 daemon 读线程 100% CPU 空转（实测烧一个核，靠 `eof` + `ConnectionClosed` 修掉，回归测试带真子进程 CPU 记账）；驱动线程跨线程复用 SQLite 连接直接 `ProgrammingError`，Run 永远停在 `created`（`cross_thread=True` 修）；手写 HTML 截图会盖住 token 注入 Bug（真页面是空 `<meta>` 原地替换）；`tianshu host stop` 三个缺陷叠在一条退出路径上（`/shutdown` 挂错 `do_GET`、清理回调绑成 bound method、先置 stop_event 再清理）→ stop 永远 404、清理静默死、Daemon 留成孤儿，靠 POST-only + 回调挂 server + 清理完再放主线程修掉（真子进程回归才测得出来）。· **留下**：M5 偏差 D1–D5 与"不做安装包/托盘"的口径写进 [M5-desktop.md](M5-desktop.md) §2/§9；断线语义写进 §3.6 与 4 条回归测试；`SUITE_VERSION` 升 **m4.4**（否决即终局，issue #40，PR #48）；剩余限制与未验证项见 [M5-desktop.md](M5-desktop.md) §10 与 [M5-verification.md](M5-verification.md)。
- **2026-11-10** · 把未提交的草稿按 `7abad75` 事实重写并落库 · 发现草稿里 Windows 和测试数两处都是假话，属"文档说假话"复发；落库时又抓到第三处——README 长期声称「运行时数据放系统标准位置」，实际一直是 CWD 下的 `.tianshu/`。
- **2026-11-09** · 扩充 AGENTS.md 开发规则：新增测试纪律 / 数据与事件完整性 / 安全与授权 / 契约与类型纪律 / 依赖与平台 / 提交与仓库卫生 / 反模式共 7 节。· 规则全部溯源 PRD 红线与 REVIEW-PRA 教训，不写空话；固化「红就是红，不装作绿」（不用 continue-on-error / type: ignore 假绿）。
- **2026-11-09** · 建立本知识库 + AGENTS.md 强制读写钩子；README 重写为标准开源样式；`main` 配置分支保护（PR + CI 绿 + enforce_admins + 禁强推）。· 确认现状：M0–M3 完成、767 测试、Windows CI 暂摘（#21）；把「自审盲区 / 测试证据造假 / 文档假话」三大教训固化进流程。

## 9. 权威文档索引

- [PRD-v1.md](PRD-v1.md) — 产品需求与架构（唯一权威）：范围、契约、红线、里程碑、决策记录
- [TECH-ARCH.md](TECH-ARCH.md) — 技术架构：选型、进程模型、打包、平台差异
- [REVIEW-PRA.md](REVIEW-PRA.md) — PR #7 独立复审报告（§5 教训的主要来源）
- [M5-desktop.md](M5-desktop.md) — M5 桌面闭环：三进程拓扑、IPC 契约、恢复语义、偏差（§2）与已知限制（§10）
- [M0-verification.md](M0-verification.md) / [M1-verification.md](M1-verification.md) / [M2-verification.md](M2-verification.md) / [M3-verification.md](M3-verification.md) / [M4-verification.md](M4-verification.md) / [M5-verification.md](M5-verification.md) — 各里程碑验收证据
- [F5-context-compile.md](F5-context-compile.md) — F5 上下文编译的设计与取舍
- [F1-exec-primitive.md](F1-exec-primitive.md) / [exec-run-command.md](exec-run-command.md) — F1 执行原语与 `run_command` 工具
- [REFS.md](REFS.md) — 参考仓库与固定 commit
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — 分支、提交、独立复审规范
- [../AGENTS.md](../AGENTS.md) — AI 开发强制指令