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

> **当前形态：命令行内核。** M0–M4 已完成，桌面三进程与图形 UI 属于 M5，尚未开始。
> 目标用户是普通用户，但现阶段请先把它当作一个**给开发者用的 CLI**——别把本文的愿景当成已实现的界面。

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
| 🪶 | **零第三方运行时依赖** | 纯 Python 标准库；dev 依赖只有 pytest + ruff |

### 它不假装能做到的事

这份清单是刻意写在前面而不是埋在文末的——本仓库的规矩是**文档不许说假话**（见 [CONTRIBUTING.md](CONTRIBUTING.md)「文档改动怎么提交」）：

- **`Workspace` 是路径边界，不是安全边界。** `LocalExecBackend.isolation == "none"`，**从未交付过物理沙箱**。它防的是模型越权与失控 worker，不防本机用户，也不防恶意代码。
- **密钥没有存进系统钥匙串。** v1 的 Keychain 后端**未实现**（零依赖下只能 ctypes 直调平台 C API，而我们没有平台能真验证），请求它会如实抛 `CredentialUnsupported`；已交付的是凭据接缝 + 全链路脱敏。
- **Windows 没有实机验证。** 三平台 CI 里 Windows 是真跑并且是绿的，但跑在 `windows-latest`（Server 2022）上，且有 **8 项**跳过：`memfs` 无符号链接（POSIX 也跳过）+ 7 项 Windows 缺能力——标准库无 PTY ×2、无进程组 / Unix 信号 ×2、PID 存活探测仅 POSIX ×1、无 POSIX 权限位语义 ×2。
- **M5 未开始。** 没有 GUI，没有常驻守护进程（`worker` 是一次性 drain）。
- 两个已知缺口开着：issue [#27](https://github.com/lorek666/tianshu-agent/issues/27)（PRD §4.1 字段漂移）、[#40](https://github.com/lorek666/tianshu-agent/issues/40)（审批被否决后 resume 会重新请求审批）。

## 快速开始

### 环境要求

- Python **3.12**（`>=3.12,<3.13`，不支持 3.13+）
- [`uv`](https://docs.astral.sh/uv/)

### 安装

```bash
git clone https://github.com/lorek666/tianshu-agent.git
cd tianshu-agent
uv sync --python 3.12
```

### 跑测试与 lint

```bash
uv run pytest                  # macOS/Linux: 942 passed, 3 skipped
                              # Windows:     937 passed, 8 skipped（5 项按平台跳过）
uv run ruff check .            # 全绿
```

> 注意 `pyproject.toml` 里 `addopts = "-q"`，再叠一个 `-q` 会把汇总行吞掉。
> 要拿准确计数请用 `uv run pytest -o addopts="" -q`。

### 离线演示（不调真实模型）

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
```

批准的是「**那个动作**」，不是「那个进程」：换个进程 `run resume` 依然有效，
但执行仍要求当前 claim 与租约在手，且**改目标 / 改参数会让批准作废**。

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

### 数据目录

**目前的默认值是当前工作目录下的 `.tianshu/events.db`**（已被 `.gitignore` 排除），
可用 `--db` 或 `--data-root` 改。事件库就是全部状态，别把它丢进版本库。

> macOS 的 `~/Library/Application Support/AgentOS/`、Windows 的 `%LOCALAPPDATA%\AgentOS\`
> 是 [docs/TECH-ARCH.md](docs/TECH-ARCH.md) 里 **M5 的设计**（含 daemon socket、缓存、日志布局），
> **还没实现**。别以为现在的 CLI 已经把数据放在那儿了。

## 架构概览

**唯一事实源是事件日志**：SQLite WAL、append-only（触发器禁止 `UPDATE` / `DELETE`），Task / Run / Approval / Grant / Lease 都是同库事务更新的**可重建投影**。状态变更必须同事务写事件，否则回放无法解释「为什么这个 Run 被 fence 了」。

五道版本化契约 + 十一条红线约束全局：**C1** 模型 I/O、**C2** 工具、**C3** 事件（41 个类型的封闭注册表，写入侧拒未登记类型）、**C4** 权限/审批（42 个 rule id 的封闭注册表：35 条拒绝原因 + 5 条非拒绝判定 + 2 条保留未使用）、**C5** 评测。

```
src/tianshu/          32 个 .py + eval/ 子包（下面按职责分组，30 个实义模块，余两个是 __init__.py / __main__.py）
├─ 事实源与契约 ─────────────────────────────────────────────
│  events.py        SQLite WAL append-only 事件日志 + 可组合事务
│  canonical.py     RFC 8785 (JCS) 规范化 + 独立标准向量
│  rules.py         C4 rule id 封闭注册表（42 个：35 拒绝 + 5 判定 + 2 保留未使用）
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
│  worker.py        作业循环：认领排队中的 Run
│  replay.py        回放校验：seq / messages_hash / 产物哈希三方核对
│  cli.py           命令行入口
└─ eval/            M4 评测：20 条 case、干净 fixture、可比分数与失败归因
```

完整规格见 [docs/PRD-v1.md](docs/PRD-v1.md)（§5 契约、§6 红线）与 [docs/TECH-ARCH.md](docs/TECH-ARCH.md)。

## 项目状态与路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** 骨架 | EventLog + E1 闭环 + StopReason/Limits | ✅ 完成 |
| **M1** 副作用 | intent/result + Receipt + 写前快照 + 恢复 | ✅ 完成 |
| **M2** 权限与任务 | C4 审批状态机（写 / 删分级）+ Task/Run + 作业表租约 | ✅ 完成 |
| **M3** 可替换 | 第二个 chat provider + 第二个沙箱后端 | ✅ 完成 |
| **M4** 可评估 | 20 条 case + `replay`/`evaluate` 分离 | ✅ 完成 |
| **M5** 桌面闭环 | Host/UI/Daemon 三进程打通 + 权限审批 UI | ⏳ 下一步 |

**M4 实测**：`SUITE_VERSION = m4.3`，20 条 case —— 正常成功 8 / 工具错误 4 / 超时恢复 3 / 恶意输入 3 / 审批边界 2，
每类有下限守卫；安全门槛一票否决，不能被其它分数抵消。

**M4 明确未验证的**（写成「通过」就是假话，详见 [docs/M4-verification.md](docs/M4-verification.md)）：
真实换模（两个真实 provider 的分数对比）未跑 · Windows 上的 `evaluate` 未单独验证 · memfs 后端未覆盖 · 真超时未覆盖。

### CI 门禁

GitHub Actions，矩阵三个平台 × Python 3.12：`ubuntu-latest` / `macos-latest` / `windows-latest`，检查名形如 `test (macos-latest) · py3.12`。

步骤与本地 `CONTRIBUTING.md` §5「提交前检查」的命令逐字相同（本地绿而 CI 红只可能是环境差异），另加两条只在 CI 才可能的门禁：

- `uv lock --check` —— 锁文件漂移。`uv sync --frozen` **不是**漂移门禁（`pyproject.toml` 变了它照样 exit 0，实测如此）。
- `uv run --frozen` —— 防止顺手改写锁文件绕过上面那条。

`main` 已开分支保护：必须走 PR、`strict`（要求分支与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推 / 禁删除。

> ⚠️ **目前只有 `ubuntu-latest` 和 `macos-latest` 两个检查是「必过」。Windows 会跑、会报红，但没有设成必过状态**——
> 也就是说 Windows 红了不会自动挡住合并。而 `ci.yml` 里自己写着「Windows 是**主打平台**」。
> 这是已知的门禁缺口，建议在 Windows 稳定一段时间后把它也设为 required（见 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) 已知限制）。

## 开发

```bash
uv sync --python 3.12
uv run pytest -o addopts="" -q   # 全量（macOS/Linux 942+3；Windows 937+8）
uv run ruff check .              # lint
```

约定（完整版见 [CONTRIBUTING.md](CONTRIBUTING.md)）：

- Python **3.12**，`uv`，**零第三方运行时依赖**；新增依赖须四目标可用（`mac-arm64` / `mac-x64` / `win-x64` / `win-arm64`，见 TECH-ARCH §8.3）。
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
| [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md) | **项目知识库**：当前状态、关键决策、踩坑教训、已知限制 |
| [AGENTS.md](AGENTS.md) | AI 编码助手的强制指令（读什么、更什么、哪些不许做） |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支、提交规范、独立复审、什么该推什么不该推 |
| [docs/REVIEW-PRA.md](docs/REVIEW-PRA.md) | PR #7 独立复审报告（「自审盲区」教训的来源） |
| `docs/M*-verification.md` | M0–M4 各里程碑的判据与验收证据 |
| [docs/REFS.md](docs/REFS.md) | 参考仓库与固定 commit |
| [docs/archive/](docs/archive/) | 历史研究记录，**已被 PRD-v1 取代，勿作为实现依据** |

**改动任何契约前先读 PRD §5、§6。** 红线与功能需求冲突时，以红线为准。

## 贡献

欢迎 issue 与 PR。请先读 [CONTRIBUTING.md](CONTRIBUTING.md)：契约变更需单独 PR，高危改动需独立复审并附可复现证据，必过的 CI 检查（ubuntu + macOS）绿了才能合。

## 许可证

[Apache License 2.0](LICENSE)（含专利授权）。
