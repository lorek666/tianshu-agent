<div align="center">

# 天枢 Agent · Tianshu Agent

**可回放、可审批、可撤销的桌面 Agent OS 内核**

让 AI 在你指定的目录里干活——但每一步副作用都留痕，越权要你点同意，
改坏了能撤销，崩溃了能判定「到底有没有生效」。

[![CI](https://github.com/lorek666/tianshu-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/lorek666/tianshu-agent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/runtime%20deps-0-success.svg)](pyproject.toml)

</div>

---

## 这是什么

面向**普通用户**的桌面 Agent OS：模型自选、在你指定的目录里读写文件、完成长任务。v1 不含图片与视频生成。

它把「AI 可能失控」当成一等工程问题来解，而不是靠 prompt 里写几句「请谨慎操作」：

- **事件溯源**：所有状态变更写进 append-only 事件日志，任何一次执行都能从零重放出相同结果。
- **执行侧授权**：授权只在执行侧判定，模型说什么都不算数；覆盖 / 删除 / 执行命令必须**人工审批**。
- **默认可撤销**：写前快照 + 可独立核对的凭据（Receipt），改坏了能撤销，撤销本身也走审批。
- **崩溃可恢复**：中断后能判定「已生效 / 未生效 / 未知」，**未知绝不盲目重放**。

> **当前形态：M5 桌面闭环已完成。** M0–M4 是命令行内核；M5 交付**三进程桌面闭环**——
> Daemon（常驻）、Host（桌面外壳）与浏览器里的 React 界面：关掉 UI 任务继续，重启后恢复。
> 但外壳是**系统浏览器**，不是安装包：没有 Tauri/WebView2、托盘、签名与自动更新
> （偏差逐条登记在 [docs/M5-desktop.md](docs/M5-desktop.md) §2）。
> 现阶段仍请把它当作**给开发者用的工具**——别把本文的愿景当成已交付的成品 App。

## 特性

| | 能力 | 怎么做到 |
|---|---|---|
| 🔁 | **可重放** | 事件日志三方核对（seq / `messages_hash` / 产物哈希）；`replay` 只读历史，不调模型 |
| 🔐 | **执行侧授权** | `dispatcher` 是唯一派发入口：消费审批 → 登记 intent → 守卫内核验 → 才执行 |
| ↩️ | **写前快照 + 冲突安全撤销** | 默认可写就默认可撤销；撤销前核对内容哈希，被外部改过就拒绝 |
| 🧯 | **崩溃恢复** | `applied` / `none_applied` / `unknown` 三态**不合并**，只有 `none_applied` 可安全重试 |
| 🧵 | **跨进程租约 + fencing** | 防同用户双实例重复执行；旧 worker 不得覆盖新 owner |
| 🧠 | **上下文编译** | 纯函数、按**字节**预算裁历史，丢弃只记账不摘要（摘要会毁掉可重放性） |
| 🛠️ | **执行工具** | `run_command` 走 argv 不经 shell、默认全拒、需审批、有超时与输出截断 |
| 💰 | **成本可见** | 整数微美元记账；价格未知记 `None` 并单独计数，**不伪装成 0** |
| 🔌 | **模型可替换** | OpenAI-compatible 与 Anthropic 两套协议，换 provider 主循环零改动 |
| 🖥️ | **桌面闭环** | 关掉 UI / 刷新页面任务照跑；重启后 Daemon 恢复扫描并续跑（判据：关 UI 后任务继续、重启后恢复） |
| 🧭 | **审批 UI** | 覆盖 / 删除 / 执行命令在界面上点「允许 / 拒绝」，决定经 IPC 落库；**否决即终局**，不再重复问同一个动作 |
| 🪶 | **零第三方运行时依赖** | 纯 Python 标准库；dev 依赖只有 pytest + ruff；UI 构建产物已提交，运行时不需要 Node |

### 它不假装能做到的事

这份清单是刻意写在前面而不是埋在文末的——本仓库的规矩是**文档不许说假话**（见 [CONTRIBUTING.md](CONTRIBUTING.md)「文档改动怎么提交」）：

- **`Workspace` 是路径边界，不是安全边界。** `LocalExecBackend().capabilities.isolation == "none"`，**从未交付过物理沙箱**。它防的是模型越权与失控 worker，不防本机用户，也不防恶意代码。
- **密钥没有存进系统钥匙串。** v1 的 Keychain 后端**未实现**（零依赖下只能 ctypes 直调平台 C API，而我们没有平台能真验证），请求它会如实抛 `CredentialUnsupported`；已交付的是凭据接缝 + 全链路脱敏。
- **Windows 没有实机验证。** 三平台 CI 里 Windows 是真跑，但**不稳定**：命名管道上偶发握手失败（issue [#50](https://github.com/lorek666/tianshu-agent/issues/50)，根因未定位，有些不稳定的轮次会让作业撞上超时被取消）；跑在 `windows-latest`（Server 2022）上，并按平台跳过若干项（`memfs` 符号链接、标准库无 PTY、无进程组 / Unix 信号、无 POSIX 权限位语义、PID 存活探测仅 POSIX……），跳过原因逐条写在测试的跳过消息里，条数以 CI `-rs` 日志为准。
- **M5 交付的是进程闭环，不是可安装的 App。** 外壳是系统浏览器：没有 Tauri/WebView2、系统托盘、开机自启、安装包、签名、公证与自动更新。关掉浏览器标签只是**少一个订阅者**，Host 一直活到 `tianshu host stop`（或被杀）。
- **F10 记忆：L1 + 生命周期（N1）+ 擦除（N4）+ 恢复/审计（N5）已落地；L2/L3 与向量召回仍未。** 已落地：事件日志上的可重建投影（`memory.py`）、9 个 `memory.*` 工具（`memory.propose` 升 2.0，可写 `user` / **本任务** `task` 作用域；`memory.remember` / `memory.promote` / `memory.erase` 是控制面专属、`exposed=False`，不进模型清单；`memory.edit` 一次审批保留旧版）、`memory://` 资源种类、C4 审批与 Receipt、CLI 控制面（`list|search|get|confirm|forget|remember|edit|promote|erase`，`--task-id` 可替旧 task 作用域签字且不绑它的目录）、`origin` 两档（`user_explicit` 由 `remember` 产生，`user_confirmed` 由人批准 `confirm` 升级）。**未实现**：L2/L3 蒸馏、向量召回、§8 的作用域优先级排序、`memory.consolidate`（契约已定义，代码未落地）。**崩溃恢复**：`recovery.recover_run` 有记忆专用分支，判据分三层——投影/事件层（与 `tool.result` 同事务，"有 intent 无 result"⇔这一层一定没生效）、正文 blob 层（提交前已落盘，恢复按引用计数清孤儿副本）、`memory.erase` 例外（先删文件、后提交，删文件不可逆：文件还在才算没生效，文件已没报 unknown，不冒充"没写"）。记忆写不阻断目录（没有 preimage），`dispatches` 行会一并推进到 `error`。**脱敏**：`tool.intent` 对带正文的记忆写（propose / remember / edit）只留 `content_sha256` + `content_bytes`；`dispatch_requests.args_payload` 在请求进入终态（成功 / 拒绝 / 执行中途未知）后脱敏，老库里的终态行打开时补扫，执行期仍保留完整参数（`args_hash` 绑定 + 执行器取参）。**残留**：停在 waiting/admitted 的请求仍带正文；payload 损坏的行不补扫（fail-open，反查口径是 `args_redacted=0`）；模型工具调用里的正文另存于 `model.result` / `run_turns`（replay 必需，物理擦除见 F10 §10）；擦除崩溃按文件是否还在分层：还在 ⇒ 确未生效，已没 ⇒ unknown 待人工核对；`assess_run` 对擦除一律保守判 unknown；正文目录不是 DB 旁默认值时，库调用方要传 `recover_run(memory=...)` 或 `blob_dir=...`（`memory` 优先；CLI/daemon 已传）；文件写请求的审计不受影响。`memory.erase` 会删正文副本与索引（引用计数为 0 才删，事件日志里的历史读快照保留）；workspace 的 propose 是**无条件**拒绝的（即便同一 Task 上后开的模型 Run 继承了控制面标记也一样），共享只能由人 `memory.promote` 进入。逐条状态见 [docs/F10-memory-design.md](docs/F10-memory-design.md) §13/§14；剩余施工规格见 [docs/SPEC-RESIDUALS-MEMORY.md](docs/SPEC-RESIDUALS-MEMORY.md)——那是**待实施**方案，不是已完成。
- **真实模型没有接过 UI。** 演示与验收用 `scripted` 模型（不联网、不花钱），它证明的是进程语义，**不代表真实模型能力**；真实模型路径见下文「接真实模型」。CLI 与评测已跑过真实模型（2026-11-10，两家模型完整 20 条 × 3 次，见 [docs/M4-verification.md](docs/M4-verification.md) §7.1），但**还没有真实模型驱动过 UI**。
- 已知缺口：issue [#40](https://github.com/lorek666/tianshu-agent/issues/40)（审批被否决后 resume 会重新请求审批）**已修**（PR #48）——否决即终局，`SUITE_VERSION` 升 `m4.4`，工单仍开着等确认；issue [#27](https://github.com/lorek666/tianshu-agent/issues/27)（PRD §4.1 字段漂移）已收敛关闭。

## 快速开始

### 环境要求

- Python **3.12**（`>=3.12,<3.13`，不支持 3.13+）
- [`uv`](https://docs.astral.sh/uv/)
- （只改前端才需要）Node + `pnpm`；**运行桌面界面不需要 Node**，`ui/dist` 已提交

### 安装

```bash
git clone https://github.com/lorek666/tianshu-agent.git
cd tianshu-agent
uv sync --python 3.12
```

### 跑测试与 lint

```bash
uv run pytest -o addopts="" -q   # macOS 本机实测：1601 passed, 7 skipped（main @ cc2240e，含 G1 与 N5）
uv run ruff check .              # 全绿（All checks passed!）
```

> 注意 `pyproject.toml` 里 `addopts = "-q"`，再叠一个 `-q` 会把汇总行吞掉，所以要 `-o addopts="" -q`。
> **Windows/CI 的数字以 CI 日志为准**：本机只有 macOS 的实测数字，Windows 只在 `windows-latest` runner 上跑，
> 没有实机验证（平台跳过项数也不要在本地猜）。

### 桌面闭环离线演示（M5，不调真实模型）

一条命令看完整闭环：建任务 → 停在审批 → **断开所有客户端（等价关掉 UI）** → 用新连接批准 → 任务自己跑完。

```bash
uv run python scripts/demo_m5.py --data-root /tmp/m5-demo --auto
```

它会打印界面地址、审批卡片（动作 / 目标 / 原因）与最终 JSON（`final_state`、`effect_state`、产物、事件序列），
结束时自己关掉 Host 与 Daemon。不加 `--auto` 则是可交互版本（Ctrl-C 退出）。

### 桌面三进程（M5）

```bash
# 1) 常驻守护进程：一般不用手动起——host 会自动拉起并监护它
uv run tianshu daemon --data-root /tmp/ts-data

# 2) 桌面外壳：单实例 + 监护 Daemon + 把 UI 的 HTTP/SSE 桥到本地 IPC
uv run tianshu host run --data-root /tmp/ts-data        # 打印 http://127.0.0.1:<随机端口>/
uv run tianshu host status --data-root /tmp/ts-data     # 在跑退出 0，没跑退出 1
uv run tianshu host stop --data-root /tmp/ts-data       # 让 Host 自己退出（它负责关 Daemon）
```

`host run` 的顺序是**先恢复、再对外服务**：拿 Host 单例锁 → 探测/拉起 Daemon（等端点就绪，上限 15s）→
Daemon 完成恢复扫描后才开 HTTP 服务 → 把访问 token 注入 `index.html` 的 `<meta name="tianshu-token">`。
页面与 `/rpc`、`/state`、`/events` 同源；数据端点都要求 `X-Tianshu-Token` 请求头
（**token 不进 URL**——URL 会进浏览器历史、shell 历史、聊天记录和截图）。`--open` 直接开浏览器，`--port 0`（默认）随机端口。

关浏览器标签 / 刷新页面**只是少一个订阅者**，任务继续跑；要停才用 `tianshu host stop`。
`stop` 是**先收掉 Daemon、再让 Host 退出**（收尾顺序本身是契约）：不会留下孤儿 Daemon 继续占着 socket、
继续接活。带 `--no-supervise` 可以不监护 Daemon 崩溃重启（调试用）。

### UI 的开发与构建（只改前端时需要）

`ui/dist` 已提交，`tianshu host` 直接加载它，运行时不需要 Node。改前端才需要：

```bash
pnpm --dir ui install
pnpm --dir ui run dev       # Vite 开发服务器（127.0.0.1:5199）
pnpm --dir ui run build     # 重建 ui/dist —— 构建产物要和源码一起提交
pnpm --dir ui run typecheck
pnpm --dir ui run test      # vitest
```

CI 的 `ui` job 跑 typecheck / test / build，并用 `git diff --exit-code -- ui/dist` 卡住「改了源码没提交产物」。
界面在没有后端时的静态预览（`?demo=1`）见 [ui/README.md](ui/README.md)。

### 命令行跑一次（不调真实模型）

用 scripted 模型跑通「阅读目录并生成摘要」的闭环：

```bash
uv run tianshu run \
  --workspace /tmp/ws --task "阅读并生成摘要" --out summary.md

# 回放校验这次执行（审计：不调模型）
uv run tianshu replay --run-id run_xxxx
```

### 接真实模型

两套线上协议，主循环一行都不用改（契约测试见 `tests/test_model_contract.py`）：

```bash
# 1) OpenAI-compatible：OpenAI / DeepSeek / Moonshot / vLLM / Ollama 都走这条
export TIANSHU_API_KEY=sk-...
export TIANSHU_BASE_URL=https://api.openai.com/v1   # 可选，支持任意兼容端点
uv run tianshu run --workspace /tmp/ws --task "..." \
  --out summary.md --model openai-compatible --model-name gpt-4o-mini

# 2) Anthropic Messages API：协议不同（system 顶层 / tool_use 内容块），所以是另一个 adapter
export ANTHROPIC_API_KEY=sk-ant-...
uv run tianshu run --workspace /tmp/ws --task "..." \
  --out summary.md --model openai-compatible --provider anthropic \
  --model-name claude-sonnet-4-5
```

`--provider` 不写时读 `TIANSHU_PROVIDER`，再默认 `openai`。
`--model-name` 对 anthropic **必须显式给**——不替你猜模型 id，猜错的表现是 404。

> 密钥只从环境变量读，永不落库、永不进事件日志。用过的 key 请自行作废轮换。

## 用法

**Task 是用户看到的那件事，Run 是一次执行**，两者生命周期分离。改目标会立刻 fence 掉旧 Run（待批请求作废，历史保留）。

| 子命令 | 作用 |
|---|---|
| `task` | 创建 / 查看 / 改目标或绑定 |
| `run` | 跑一次任务；或用 `start` / `status` / `pause` / `resume` / `cancel` 控制已有 Run |
| `approvals` | 审批队列：`list` / `approve` / `reject` / `revoke` |
| `undo` | 撤销某次 Run 的文件变更（不带 `--approve` 只预演） |
| `recover` | 核对并补全一次被打断的 Run |
| `worker` | 作业循环，**一次性 drain**：队空或撞上被占用的工作区就退出 |
| `daemon` | **常驻**守护进程：驱动任务、恢复扫描、IPC 控制面（不是 `worker`） |
| `host` | 桌面外壳：`run`（默认）/ `stop` / `status`，把 UI 桥到本地 IPC |
| `replay` | 从事件日志回放并校验（**审计，不调模型**） |
| `evaluate` | 在干净 fixture 上换模型跑固定 case（**换模，与 replay 是两件事**） |

```bash
# 建任务（目录可重复；--readonly 指定只读目录；不传目录 = 纯对话任务）
uv run tianshu task create --goal "整理季度笔记" --workspace /tmp/ws

# 开一次执行 / 看状态 / 暂停 / 恢复 / 取消
uv run tianshu run start  --task-id task_xxxx --out summary.md
uv run tianshu run status --run-id run_xxxx
uv run tianshu run pause  --run-id run_xxxx
uv run tianshu run resume --run-id run_xxxx
uv run tianshu run cancel --run-id run_xxxx

# 人工审批（覆盖、删除、执行命令都不会被自动执行）
uv run tianshu approvals list
uv run tianshu approvals approve appr_xxxx

# 撤销某次执行的文件变更（不带 --approve 时只预演）
uv run tianshu undo --run-id run_xxxx --workspace /tmp/ws

# 作业循环：认领排队中的 Run（崩溃、重启后接手；死掉的 owner 立刻可接管）
uv run tianshu worker --once    # 只跑一个作业
uv run tianshu worker           # 跑到队空 / 第一次撞上被占用的工作区为止

# 桌面：常驻守护进程 + 外壳（详见上方「桌面三进程」）
uv run tianshu daemon --data-root /tmp/ts-data
uv run tianshu host run --data-root /tmp/ts-data
```

批准的是「**那个动作**」，不是「那个进程」：换个进程 `run resume` 依然有效，
但执行仍要求当前 claim 与租约在手，且**改目标 / 改参数会让批准作废**。
**否决是终局**：同一 Run 内同一动作被拒绝后，后续派发直接以 `c4.approval_rejected` 拒绝，
不会反复弹同一张卡；模型改了参数或目标才是一次新的审批。

`replay` 与 `evaluate` 不是同一个东西：前者只读历史日志做审计，后者换模型重跑。
PRD §6 红线 10 明令不得混称——安全门槛是硬门槛，不能被其它分数抵消。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 有结论且成功 |
| `1` | 失败 / 取消（`worker` 被接管或结算失败也是 1） |
| `2` | 用法或前置条件错误（含 `recover` 发现未核实效果） |
| `3` | 需要人决定（等审批 / 已暂停 / 待核查；`undo` 未带 `--approve` 的预演） |
| `4` | （仅 `evaluate`）安全门槛被击穿 = unsafe |

`host status` 在跑返回 `0`、没跑返回 `1`（便于脚本判断）；`host stop` 成功退出或本来没在跑都返回 `0`。

### 数据目录

**M5 起有两套落点，别混：**

| 用谁 | 默认数据根 | 覆盖方式 |
|---|---|---|
| `tianshu daemon` / `tianshu host` | 平台标准目录：macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`） | `--data-root` 或环境变量 `TIANSHU_DATA_ROOT` |
| 内核子命令（`run` / `task` / `approvals` / `undo` / …） | 当前工作目录下的 `.tianshu/events.db`（已被 `.gitignore` 排除） | `--db` |

数据根里是：`events.db`（唯一事实源）、`artifacts/blobs`、`artifacts/snapshots`、
`run/`（IPC 端点、`ipc.key`、Host/Daemon 锁）与 `logs/daemon.log`（Host 重定向的 Daemon 日志）。
测试与 CI 一律显式指到临时目录，**不会写你的用户主目录**。

## 架构概览

**唯一事实源是事件日志**：SQLite WAL、append-only（触发器禁止 `UPDATE` / `DELETE`），Task / Run / Approval / Grant / Lease 都是同库事务更新的**可重建投影**。状态变更必须同事务写事件，否则回放无法解释「为什么这个 Run 被 fence 了」。

**M5 的三进程**：UI（浏览器里的 React）→ loopback HTTP + SSE → Host（Python：单实例、监护 Daemon、注入 token）→ IPC（macOS/Linux = AF_UNIX，Windows = 命名管道，`multiprocessing.connection` + `authkey`）→ Daemon（任务 / 事件 / 审批 / 驱动线程）。**UI 断开只减少订阅者**；Daemon 重启时先做恢复扫描（有 intent 无 result 的调用交给既有 `recover_run`、重新排队 `queued`/`parked`），再对外服务。

五道版本化契约 + 十一条红线约束全局：**C1** 模型 I/O、**C2** 工具、**C3** 事件（41 个类型的封闭注册表，写入侧拒未登记类型）、**C4** 权限/审批（43 个 rule id 的封闭注册表：36 条拒绝原因 + 5 条非拒绝判定 + 2 条保留未使用）、**C5** 评测（`SUITE_VERSION = m4.4`）。

```
src/tianshu/          47 个 .py + eval/ 子包（下面按职责分组，45 个实义模块，余两个是 __init__.py / __main__.py）
├─ 事实源与契约 ─────────────────────────────────────────────
│  events.py        SQLite WAL append-only 事件日志 + 可组合事务
│  canonical.py     RFC 8785 (JCS) 规范化 + 独立标准向量
│  rules.py         C4 rule id 封闭注册表（43 个：36 拒绝 + 5 判定 + 2 保留未使用）
│  errors.py        区分「模型想错了」与「执行侧跳过了校验」（后者是 bug）
├─ 路径与磁盘 ───────────────────────────────────────────────
│  workspace.py     路径规范化 / 资源身份（st_dev:st_ino）/ 0..n 目录绑定
│  pathkeys.py      路径键：大小写 / Unicode 规范化与祖先判断
│  fsutil.py        原子写入：临时文件 → fsync → os.replace → fsync 目录
│  sandbox.py       存储后端接缝：LocalBackend（真实盘）/ MemBackend（进程内，不是沙箱）
├─ 授权与执行 ───────────────────────────────────────────────
│  permissions.py   C4 动作分类 / 审批 / 一次性 grant
│  dispatcher.py    唯一派发入口：消费审批 + 登记 intent + 守卫内核验后执行
│  execpolicy.py    F2 执行侧命令策略（默认全拒）
│  exec.py          F1 执行原语：argv 模式（非 shell）、超时、截断、进程组
│  tools.py         受限工具 + FileExecutor（自己回库核验票据，不信调用方）
│  guard.py         工作区阻断检查（未知副作用不得继续写）
├─ 副作用可撤销 ─────────────────────────────────────────────
│  snapshot.py      写前快照与冲突安全撤销
│  receipt.py       操作凭据（能独立核对，不必信调用方）
│  undo.py          撤销：把一次 Run 的写入还原或删除（本身也走审批/租约/receipt）
│  recovery.py      中断核查：applied / none_applied / unknown 三者不合并
│  crashpoint.py    故障注入点（os._exit 硬退，仅 TIANSHU_FAILPOINT 生效）
├─ 任务、租约、作业 ─────────────────────────────────────────
│  tasks.py         Task / Run / RunCursor 状态机，fencing，控制请求
│  leases.py        跨进程资源租约 + 本机执行守卫（POSIX / Windows）
│  jobs.py          作业认领与 claim_epoch（旧 worker 不得覆盖新 owner）
├─ 模型层 ───────────────────────────────────────────────────
│  model.py         C1 接缝：adapter 只做双向翻译，不改写内部形状
│  capability.py    F9a 能力声明与准入（只用于路由，不用于授权）
│  credentials.py   F12 凭据分域查找 + 全链路脱敏（Keychain 后端 v1 未实现）
│  pricing.py       F13 成本账：整数微美元，未知价格记 None 并单独计数
│  context.py       F5 上下文编译：纯函数、按字节预算、丢弃只记账不摘要
├─ 驱动 ─────────────────────────────────────────────────────
│  loop.py          E1 主循环（持久游标、暂停 / 取消 / 恢复）+ 栈装配
│  worker.py        作业循环：认领排队中的 Run（一次性 drain）
│  replay.py        回放校验：seq / messages_hash / 产物哈希三方核对
│  cli.py           命令行入口（含 daemon / host 两个常驻入口）
├─ 桌面三进程（M5）─────────────────────────────────────────
│  ipc.py           本地 IPC：AF_UNIX / 命名管道、版本区间协商、推送游标、断线语义
│  daemon.py        常驻守护进程：锁、恢复扫描、请求线程、驱动线程、优雅退出
│  control.py       RPC 方法 → 既有库函数的映射（不自己写文件、不重判策略）
│  host.py          外壳：单实例、监护 Daemon、UI 的 HTTP/SSE 桥、token 注入
└─ eval/            M4 评测：20 条 case、干净 fixture、可比分数与失败归因
```

平台标准数据目录、单实例判据、断线语义与恢复顺序的完整规格见 [docs/M5-desktop.md](docs/M5-desktop.md)；
产品规格见 [docs/PRD-v1.md](docs/PRD-v1.md)（§5 契约、§6 红线）与 [docs/TECH-ARCH.md](docs/TECH-ARCH.md)。

UI 在 `ui/`：Vite + React + TS，构建产物 `ui/dist` 已提交（Host 直接加载，运行时不需要 Node）。

## 项目状态与路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** 骨架 | EventLog + E1 闭环 + StopReason/Limits | ✅ 完成 |
| **M1** 副作用 | intent/result + Receipt + 写前快照 + 恢复 | ✅ 完成 |
| **M2** 权限与任务 | C4 审批状态机（写 / 删分级）+ Task/Run + 作业表租约 | ✅ 完成 |
| **M3** 可替换 | 第二个 chat provider + 第二个沙箱后端 | ✅ 完成 |
| **M4** 可评估 | 20 条 case + `replay`/`evaluate` 分离 | ✅ 完成 |
| **M5** 桌面闭环 | Daemon/Host/UI 三进程打通 + 权限审批 UI | ✅ 完成（PR #49） |

**M5 实测**：全量 **1316 passed, 6 skipped**（macOS 本机，2026-11-11 收尾轮 `43d04ef`）；三进程真进程链路跑通——
`tianshu host run` → `tianshu host stop` 后 Host 与 Daemon 都退出（`ipc.probe == False`，不留孤儿），
`scripts/demo_m5.py --auto` 真 daemon + 真 `ui/dist` + 真 Host 桥输出 `final_state=succeeded` / `effect_state=applied`
（中途断开所有客户端后用新连接批准）。完整判据、未验证项与真进程截图见 [docs/M5-verification.md](docs/M5-verification.md)。

**M5 明确未验证的**（写成「通过」就是假话）：**Windows 实机**（只有 CI `windows-latest`，命名管道的杀软/ACL/长路径差异未覆盖）·
**真实模型驱动 UI**（CLI / `evaluate` 已用真实模型跑过：deepseek-v4.1-flash 18/20、glm-5.2 15/20，见 [docs/M4-verification.md](docs/M4-verification.md) §7.1；UI 仍未接过真实模型）· Tauri/WebView2 外壳与托盘（M5 没做）· 同毫秒并发审批压测。
完整清单见 [docs/M5-verification.md](docs/M5-verification.md)。

**M4 实测**：`SUITE_VERSION = m4.4`，20 条 case —— 正常成功 8 / 工具错误 4 / 超时恢复 3 / 恶意输入 3 / 审批边界 2，
每类有下限守卫；安全门槛一票否决，不能被其它分数抵消。
M5 同批修掉了 issue #40（**否决即终局**）：`approval="reject"` 的判据改为 `approval.veto_terminates`，
并保留「无重复审批请求」的机械判据。

**M4 明确未验证的**（详见 [docs/M4-verification.md](docs/M4-verification.md)）：
真实换模（两个真实 provider 的分数对比）未跑 · Windows 上的 `evaluate` 未单独验证 · memfs 后端未覆盖 · 真超时未覆盖。

### CI 门禁

GitHub Actions。Python 矩阵三个平台 × Python 3.12：`ubuntu-latest` / `macos-latest` / `windows-latest`，检查名形如 `test (macos-latest) · py3.12`；
另有一个前端 job `ui · typecheck / test / build`（ubuntu）：typecheck → vitest → build → `git diff --exit-code -- ui/dist`（产物漂移门禁）。

步骤与本地 `CONTRIBUTING.md` §5「提交前检查」的命令逐字相同（本地绿而 CI 红只可能是环境差异），另加两条只在 CI 才可能的门禁：

- `uv lock --check` —— 锁文件漂移。`uv sync --frozen` **不是**漂移门禁（`pyproject.toml` 变了它照样 exit 0，实测如此）。
- `uv run --frozen` —— 防止顺手改写锁文件绕过上面那条。

`main` 已开分支保护：必须走 PR、`strict`（要求分支与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推 / 禁删除。

> ⚠️ **目前只有 `ubuntu-latest` 和 `macos-latest` 两个 Python 检查是「必过」。Windows 会跑、会报红，但没有设成必过状态**——
> 也就是说 Windows 红了不会自动挡住合并。而 `ci.yml` 里自己写着「Windows 是**主打平台**」。
> 这是已知的门禁缺口，建议在 Windows 稳定一段时间后把它也设为 required（见 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) 已知限制）。

## 开发

```bash
uv sync --python 3.12
uv run pytest -o addopts="" -q   # macOS 本机实测：1601 passed, 7 skipped（main @ cc2240e，含 G1 与 N5）
uv run ruff check .              # lint
```

约定（完整版见 [CONTRIBUTING.md](CONTRIBUTING.md)）：

- Python **3.12**，`uv`，**零第三方运行时依赖**；新增依赖须四目标可用（`mac-arm64` / `mac-x64` / `win-x64` / `win-arm64`，见 TECH-ARCH §8.3）。
- UI 用 Vite + React + TS；`ui/dist` 是**要提交的产物**（Host 直接加载），改了前端必须重建并一起提交，CI 有漂移门禁。
- 事件日志 = SQLite WAL 单提交点，事务统一走 `EventLog.transaction()`（可组合，不嵌套 `BEGIN`）。
- 授权只在执行侧判定。
- 契约变更单独提 `docs(contract):`，body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」。
- 高危改动（授权 / 租约 / 并发写 / 事件日志 / 计费）**须独立复审并附可复现证据**。

> **AI 辅助开发**：每次开发前必读 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)（项目知识库），结束后更新它。
> 强制指令见 [AGENTS.md](AGENTS.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/PRD-v1.md](docs/PRD-v1.md) | **产品需求与架构**：范围、五道契约、十一条红线、里程碑（唯一权威版本） |
| [docs/TECH-ARCH.md](docs/TECH-ARCH.md) | **技术架构**：选型、进程模型、打包、平台差异 |
| [docs/M5-desktop.md](docs/M5-desktop.md) | **M5 桌面闭环**：三进程拓扑、IPC 契约、恢复语义、偏差登记（§2）、已知限制（§10） |
| [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) | **项目知识库**：当前状态、关键决策、踩坑教训、已知限制 |
| [docs/F10-memory-design.md](docs/F10-memory-design.md) | **F10 记忆设计（v1.1）**：记忆作为事件日志的可重建投影、L0–L3 分层、作用域与装备、写入分级审批；**§6.1 资源种类（`memory://`）**、**§13 契约增量的实际状态**、**§14 判据↔真实测试** |
| [docs/SPEC-RESIDUALS-MEMORY.md](docs/SPEC-RESIDUALS-MEMORY.md) | **剩余记忆工作量施工规格**：§13 #18–#27 逐条现状（代码事实）、六个工作流、契约增量清单、判据与负控口径、明确不做 |
| [AGENTS.md](AGENTS.md) | AI 编码助手的强制指令（读什么、更什么、哪些不许做） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支、提交规范、独立复审、什么该推什么不该推 |
| [docs/REVIEW-PRA.md](docs/REVIEW-PRA.md) | PR #7 独立复审报告（「自审盲区」教训的来源） |
| `docs/M*-verification.md` | M0–M5 各里程碑的判据与验收证据 |
| [docs/REFS.md](docs/REFS.md) | 参考仓库与固定 commit |
| [docs/archive/](docs/archive/) | 历史研究记录，**已被 PRD-v1 取代，勿作为实现依据** |

**改动任何契约前先读 PRD §5、§6。** 红线与功能需求冲突时，以红线为准。

## 贡献

欢迎 issue 与 PR。请先读 [CONTRIBUTING.md](CONTRIBUTING.md)：契约变更需单独 PR，高危改动需独立复审并附可复现证据，必过的 CI 检查（ubuntu + macOS）绿了才能合。

## 许可证

[Apache License 2.0](LICENSE)（含专利授权）。