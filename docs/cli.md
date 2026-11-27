# 命令行参考

> 本文件从 README 拆出，收录完整命令、退出码、数据目录与模型接入。
> README 只留最小可跑例子与最常用的几条。
>
> 语义规格（三进程拓扑、IPC 契约、恢复顺序）见 [M5-desktop.md](M5-desktop.md)；
> 备份/恢复的材料布局与协作锁边界见 [backup-restore.md](backup-restore.md)。

## 环境要求

- Python **3.12**（`>=3.12,<3.13`，不支持 3.13+）
- [`uv`](https://docs.astral.sh/uv/)
- （只改前端才需要）Node + `pnpm`；**运行桌面界面不需要 Node**，`ui/dist` 已提交

## 安装

```bash
git clone https://github.com/lorek666/tianshu-agent.git
cd tianshu-agent
uv sync --python 3.12
```

## 跑测试与 lint

```bash
uv run --frozen pytest           # 当前结果以提交 SHA 和验证日志为准
uv run --frozen ruff check .      # lint
```

> 注意 `pyproject.toml` 里 `addopts = "-q"`，再叠一个 `-q` 会把汇总行吞掉，所以要 `-o addopts="" -q`。
> **Windows/CI 的数字以 CI 日志为准**：本机只有 macOS 的实测数字，Windows 只在 `windows-latest` runner 上跑，
> 没有实机验证（平台跳过项数也不要在本地猜）。

## 子命令总览

**Task 是用户看到的那件事，Run 是一次执行**，两者生命周期分离。改目标会立刻 fence 掉旧 Run（待批请求作废，历史保留）。

| 子命令 | 作用 |
|---|---|
| `task` | 创建 / 查看 / 改目标或绑定 |
| `run` | 跑一次任务；或用 `start` / `status` / `pause` / `resume` / `cancel` 控制已有 Run |
| `approvals` | 审批队列：`list` / `approve` / `reject` / `revoke` |
| `undo` | 撤销某次 Run 的文件变更（不带 `--approve` 只预演） |
| `recover` | 核对并补全一次被打断的 Run |
| `worker` | 作业循环，**一次性 drain**：默认按 64 个作业一批持续运行直到队空；撞上被占用的工作区就退出 |
| `daemon` | **常驻**守护进程：驱动任务、恢复扫描、IPC 控制面（不是 `worker`） |
| `host` | 桌面外壳：`run`（默认）/ `stop` / `status`，把 UI 桥到本地 IPC |
| `replay` | 从事件日志回放并校验（**审计，不调模型**） |
| `evaluate` | 在干净 fixture 上换模型跑固定 case（**换模，与 replay 是两件事**） |
| `data` | 离线 `backup`、独立 `verify`、备份绑定的 `migrate`、只读 `restore-plan` 与受管恢复、只读 `retention-audit`、保守离线 `gc`、本地默认脱敏的 `export-diagnostics`（支持 `--preview` 清单预览） |

> **CLI 与桌面端数据目录打通**：内核子命令默认使用当前工作目录下的 `.tianshu/events.db`，桌面 `daemon`/`host` 默认使用系统应用目录（`ipc.default_data_root()`）。传入 `--desktop`（或设 `TIANSHU_USE_DESKTOP_DB=1`）即可让 `task` / `run` / `approvals` / `schedule` 等 CLI 子命令直接操作桌面端默认数据库。

## 常用命令

```bash
# 建任务（目录可重复；--readonly 指定只读目录；不传目录 = 纯对话任务）
uv run tianshu task create --goal "整理季度笔记" --workspace /tmp/ws

# 开一次执行 / 看状态 / 暂停 / 恢复 / 取消
uv run tianshu run start  --task-id task_xxxx --out summary.md
uv run tianshu run status --run-id run_xxxx
uv run tianshu run pause  --run-id run_xxxx
uv run tianshu run resume --run-id run_xxxx
uv run tianshu run cancel --run-id run_xxxx

# 低盘或只想读写分离时，显式新建一个仅读取与对话的 Run
uv run tianshu run start --task-id task_xxxx --execution-mode pure_only --out summary.md

# 人工审批（覆盖、删除、执行命令都不会被自动执行）
uv run tianshu approvals list
uv run tianshu approvals approve appr_xxxx

# 撤销某次执行的文件变更（不带 --approve 时只预演）
uv run tianshu undo --run-id run_xxxx --workspace /tmp/ws

# 作业循环：认领排队中的 Run（崩溃、重启后接手；死掉的 owner 立刻可接管）
uv run tianshu worker --once    # 只跑一个作业
uv run tianshu worker           # 默认按 64 个一批跑到队空 / 第一次撞上被占用的工作区为止
uv run tianshu worker --max-jobs 10  # 只跑这一批最多 10 个作业，idle=false 表示可能还有活

# 桌面：常驻守护进程 + 外壳（详见下文「桌面三进程」）
uv run tianshu daemon --data-root /tmp/ts-data
uv run tianshu host run --data-root /tmp/ts-data
```

批准的是「**那个动作**」，不是「那个进程」：换个进程 `run resume` 依然有效，
但执行仍要求当前 claim 与租约在手，且**改目标 / 改参数会让批准作废**。
**否决是终局**：同一 Run 内同一动作被拒绝后，后续派发直接以 `c4.approval_rejected` 拒绝，
不会反复弹同一张卡；模型改了参数或目标才是一次新的审批。

`--execution-mode` 缺省为 `effects`，`pure_only` 只约束这次新建的 Run：它禁止该 Run
调用真实有副作用的工具，但不能把 workspace 的 `readonly` / `writable` 绑定改成权限，
也不能把已经创建的 Run 原地升级成 `effects`。已有 Run 的 `status` / `pause` / `resume` /
`cancel` 不接受该参数；需要另一资格时新开 Run。低盘原因会明确返回，入口不会静默降级。

`replay` 与 `evaluate` 不是同一个东西：前者只读历史日志做审计，后者换模型重跑。
PRD §6 红线 10 明令不得混称——安全门槛是硬门槛，不能被其它分数抵消。

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 有结论且成功 |
| `1` | 失败 / 取消（`worker` 被接管或结算失败、`recover` 发现未核实效果也是 1） |
| `2` | 用法或前置条件错误 |
| `3` | 需要人决定（等审批 / 已暂停 / 待核查；`undo` 未带 `--approve` 的预演） |
| `4` | （仅 `evaluate`）安全门槛被击穿 = unsafe |

`host status` 在跑返回 `0`、没跑返回 `1`（便于脚本判断）；`host stop` 成功退出或本来没在跑都返回 `0`。

## 数据目录与离线维护

先关闭使用该根的 Host、Daemon 和 CLI。使用 `--data-root` 指定要维护的数据根。`data gc` 也接受 `--desktop`，用于选择桌面端默认数据根。

```bash
uv run --frozen tianshu data backup --data-root /absolute/data-root --dest /absolute/backups/backup-1
uv run --frozen tianshu data verify /absolute/backups/backup-1 --json
uv run --frozen tianshu data restore-plan /absolute/backups/backup-1 --data-root /absolute/data-root --json
uv run --frozen tianshu data migrate --data-root /absolute/data-root --backup /absolute/backups/backup-1 --json
uv run --frozen tianshu data gc --data-root /absolute/data-root --json
```

- **备份与迁移**。备份保存事件库及已核验材料。请另行保管工作区文件、IPC 密钥和日志。显式 `migrate` 处理标准 `ROOT/events.db`，核验备份与升级前一致点的绑定，支持同进度续做。
- **受管恢复**。只读 `restore-plan` 检查通过退出 `3`。在支持目录持久化的 POSIX 文件系统上，可按计划用 `restore-apply --expect-plan` 导入原路径并保持隔离。`restore-review` 默认隐藏正文。复核完成后，可用 `restore-release --expect-review` 和对后续删除历史的显式确认解除隔离。解除后的 v1 库仍需显式备份与迁移。
- **离线清理**。`data gc` 默认预演，成功退出 `3`。发现 SQLite 旁文件时拒绝预演并保留现场。核对候选后，可显式 `--approve` 在独占准入下重新核验。Windows 遇到任一文件候选会拒绝整批。执行失败退出 `2`，`deleted` 和错误信息记录已确认的删除。

受管写入的平台范围、旧材料布局的处置办法、完整命令及退出码见 [备份与恢复指南](backup-restore.md)。

低磁盘 Run 资格与执行门已随 [PR #144](https://github.com/lorek666/tianshu-agent/pull/144) 合入。锁忙和审批后删除回归随 [PR #195](https://github.com/lorek666/tianshu-agent/pull/195) 修复，对应的 #145 和 #147 已关闭。

**M5 起有两套落点，别混：**

| 用谁 | 默认数据根 | 覆盖方式 |
|---|---|---|
| `tianshu daemon` / `tianshu host` | 平台标准目录：macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`（缺省 `~/.local/share/AgentOS`） | `--data-root` 或环境变量 `TIANSHU_DATA_ROOT` |
| 内核子命令（`run` / `task` / `approvals` / `undo` / …） | 当前工作目录下的 `.tianshu/events.db`（已被 `.gitignore` 排除） | `--db` |

数据根里是：`events.db`（唯一事实源）、`artifacts/blobs`、`artifacts/snapshots`、
`run/`（IPC 端点、`ipc.key`、Host/Daemon 锁）与 `logs/daemon.log`（Host 重定向的 Daemon 日志）。
测试与 CI 一律显式指到临时目录，**不会写你的用户主目录**。

## 接真实模型

Chat Completions、Responses 与 Anthropic Messages 各有独立 adapter，共用中立模型契约
（离线契约测试见 `tests/test_model_contract.py`、`tests/test_responses_conformance.py`）：

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

# 3) OpenAI Responses：复用 TIANSHU_API_KEY / openai 凭据域
# 下面会请求真实服务并可能计费；model ID 按服务实际提供的值填写。
uv run --frozen tianshu run --workspace /tmp/ws --task "阅读目录并生成摘要" \
  --out summary.md --model openai-compatible --provider openai-responses \
  --model-name YOUR_EXPLICIT_MODEL_ID
```

`--provider` 不写时读 `TIANSHU_PROVIDER`，再默认 `openai`。
`--model-name` 对 `anthropic` 和 `openai-responses` **必须显式给**。
`run` / `worker` / `daemon` / `host` / `evaluate` 均支持新 provider；保留旧的
`--model openai-compatible` 在线入口选择器。桌面设置与发送栏快速切模也可选择 Responses。

Responses 仅支持公开 HTTP/SSE 的文本、函数工具和不透明加密 reasoning；
不支持多模态、供应商内置工具、后台任务或 WebSocket。请求发送 `store:false`，
使用完整历史与加密输出续接，不依赖 `previous_response_id` 或托管 conversation。
base URL 填到 `/v1` 一层，adapter 添加 `/responses`；URL 不允许凭据、查询参数或 fragment。
改协议、端点或 model 后，已有续接链会拒绝下一次模型调用，需创建新 Run。
完整上下文超预算时明确失败，不自动摘要或截断有效链；旧配置不自动迁移。
网关价格或缓存计价无法核实时，成本显示未知。官方与网关真实付费 smoke 均为 `NOT_RUN`，
本机回环 HTTP/SSE 与离线夹具不证明任意网关兼容。详见 [续接契约](openai-responses-contract.md)。

### 凭据与文本配置

非敏感设置保存到 `<data_root>/model_config.json` 和 `model_profiles.json`（JSON 文本），
可在「设置 → 模型与密钥」修改。API Key 只读实际启动进程的环境变量：OpenAI 兼容
Chat / Responses 用 `TIANSHU_API_KEY`，Anthropic 用 `ANTHROPIC_API_KEY`。
界面只展示变量名与配置状态；不接收密钥，不自动加载 `.env`，原 `credential.set/delete`
请求明确返回 `unsupported`。密钥不得写入 SQLite、配置或事件，RPC 响应不回显，日志与错误脱敏。
用过的中转 key 请自行作废轮换。

先停止旧 App / Host / Daemon，再从已经设置变量的终端启动；外部 `export` 不会改变正在运行的
进程环境。开发时可用上面的 `uv run --frozen tianshu host run`。macOS 已安装的 App 可直接运行：

```bash
export TIANSHU_API_KEY='替换为你的 key'
# 如使用 Anthropic：export ANTHROPIC_API_KEY='替换为你的 key'
"$HOME/Applications/Tianshu Agent.app/Contents/MacOS/tianshu-desktop"
```

路径按实际安装位置调整。Finder 启动不保证继承 `.zshrc` 或当前终端变量；设置面板的状态
反映 Daemon 实际能读到的值，不承诺自动同步 shell 配置。环境变量不是系统安全存储。

旧可编辑 JSON 中的 `credential_backend="keychain"` 加载时转为 `env`，不会读取系统钥匙串，
也不会立即改写文件，下次保存才落盘。旧系统密钥不会自动搬移或删除。显式设置
`TIANSHU_CREDENTIAL_BACKEND=keychain` 会报停用；移除该选择后用环境变量启动新 Run。
历史 Run 的 Keychain 路由保留用于审计，继续执行时明确拒绝，不能悄悄换凭据来源。
完整约定见[文本配置与环境变量凭据](credential-env-contract.md)。

## 桌面三进程（M5）

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

## 桌面闭环离线演示（不调真实模型）

一条命令看完整闭环：建任务 → 停在审批 → **断开所有客户端（等价关掉 UI）** → 用新连接批准 → 任务自己跑完。

```bash
uv run python scripts/demo_m5.py --data-root /tmp/m5-demo --auto
```

它会打印界面地址、审批卡片（动作 / 目标 / 原因）与最终 JSON（`final_state`、`effect_state`、产物、事件序列），
结束时自己关掉 Host 与 Daemon。不加 `--auto` 则是可交互版本（Ctrl-C 退出）。

## UI 的开发与构建（只改前端时需要）

`ui/dist` 已提交，`tianshu host` 直接加载它，运行时不需要 Node。改前端才需要：

```bash
pnpm --dir ui install
pnpm --dir ui run dev       # Vite 开发服务器（127.0.0.1:5199）
pnpm --dir ui run build     # 重建 ui/dist —— 构建产物要和源码一起提交
pnpm --dir ui run typecheck
pnpm --dir ui run test      # vitest
```

CI 的 `ui` job 跑 typecheck / test / build，并用 `git diff --exit-code -- ui/dist` 卡住「改了源码没提交产物」。
界面在没有后端时的静态预览（`?demo=1`）见 [ui/README.md](../ui/README.md)。
