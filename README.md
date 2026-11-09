# 天枢 Agent（Tianshu Agent）

桌面 Agent OS：Windows / macOS 双端，普通用户可用，模型自选，v1 不含图片与视频生成。

> 状态：架构与 PRD 已定稿，进入 M0（骨架闭环）。
> 本目录是唯一开发目录。文档在 `docs/`。

---

## 目录

```
天枢Agent/
├── docs/              架构与 PRD（唯一权威版本）；archive/ 为历史记录
├── src/tianshu/       Python 主包
├── tests/             测试与验收样例
├── playground/        学习练习（不进版本库）
└── README.md
```

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/PRD-v1.md](docs/PRD-v1.md) | **产品需求与架构：范围、契约、红线、里程碑** |
| [docs/TECH-ARCH.md](docs/TECH-ARCH.md) | **技术架构：选型、进程模型、打包、平台差异** |
| [docs/REFS.md](docs/REFS.md) | 参考仓库与固定 commit |
| [docs/archive/](docs/archive/) | 历史研究记录，**已被 PRD-v1 取代，勿作为实现依据** |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 分支、提交规范、什么该推什么不该推 |

**改动任何契约前先读 PRD §5、§6**（五道契约 + 十一条红线）。红线与功能需求冲突时以红线为准。

## 当前阶段：M0

M0 目标：单 run 闭环 + 事件日志可重放。

第一张工单范围（来自评审）：

> 实现「指定目录阅读与摘要」的命令行原型：任务边界与验收样例先行，接一家真实模型，三个受限文件工具（`list_files` / `read_text` / `create_report`），SQLite 执行记录，轮数/预算控制。交付可运行示例 + 实际生成的报告 + 逐项验收结果。

**验收（M0 退出条件）**：一条 run 的事件日志能从零重放出相同结果。

### 已实现

```
src/tianshu/
├── events.py      SQLite WAL append-only 事件日志（触发器禁止 UPDATE/DELETE）+ 可组合事务
├── canonical.py   RFC 8785 (JCS) 规范化 + 独立标准向量
├── workspace.py   路径规范化 / 资源身份（st_dev:st_ino）/ 0..n 目录绑定
├── guard.py       工作区阻断检查（未知副作用不得继续写）
├── tasks.py       Task / Run / RunCursor 状态机，fencing，控制请求，执行守卫入口
├── leases.py      跨进程资源租约 + 本机执行守卫（POSIX/Windows）
├── jobs.py        作业认领与 claim_epoch（旧 worker 不得覆盖新 owner）
├── permissions.py C4 动作分类 / 审批 / 一次性 grant
├── dispatcher.py  唯一派发入口：消费审批 + 登记 intent + 守卫内核验后执行
├── tools.py       受限工具 + FileExecutor（自己回库核验票据，不信调用方）
├── snapshot.py    写前快照与冲突安全撤销
├── receipt.py     操作凭据（凭据能独立核对）
├── recovery.py    中断核查：applied / none_applied / unknown 三者不合并
├── loop.py        E1 主循环（持久游标、暂停/取消、恢复）+ 栈装配
├── worker.py      作业循环：认领排队中的 Run、阻塞期间后台续心跳
├── replay.py      回放校验：seq / messages_hash / 产物哈希三方核对
└── cli.py         命令行入口
```

### 跑起来

```bash
uv sync --python 3.12
uv run pytest                       # M0/M1/M2 全量（数量随工单增长，见提交记录）
uv run ruff check .

# 离线演示（scripted 模型，不调真实模型）
uv run python -m tianshu run \
  --workspace /tmp/ws --task "阅读并生成摘要" --out summary.md

# 回放校验
uv run python -m tianshu replay --run-id run_xxxx
```

任务与执行的生命周期是分开的（M2）：**Task 是用户看到的那件事，Run 是一次执行**。
覆盖/删除类写入需要人工批准，暂停只在安全点生效，恢复不重跑已完成的调用。

```bash
# 建任务（目录可重复；--readonly 指定只读目录；不传目录 = 纯对话任务）
uv run python -m tianshu task create --goal "整理季度笔记" --workspace /tmp/ws

# 开一次执行
uv run python -m tianshu run start --task-id task_xxxx --out summary.md

# 看状态 / 暂停 / 恢复 / 取消
uv run python -m tianshu run status --run-id run_xxxx
uv run python -m tianshu run pause  --run-id run_xxxx
uv run python -m tianshu run resume --run-id run_xxxx
uv run python -m tianshu run cancel --run-id run_xxxx

# 人工审批（覆盖、删除类操作不会被自动执行）
uv run python -m tianshu approvals list
uv run python -m tianshu approvals approve appr_xxxx
# 批准的是"那个动作"，不是"那个进程"：换一个进程 run resume 依然有效，
# 但执行仍要求当前 claim 与租约在手，且改目标/改参数会让批准作废。

# 作业循环：认领排队中的 Run（进程崩溃、重启后接手；死掉的 owner 立刻可接管）
# 注意：这是一次性 drain，不是常驻守护进程——跑完队列（或第一次撞上
# "工作区被别的 Run 占着"）就退出，由调用方决定隔多久再来一次。
uv run python -m tianshu worker --once    # 只跑一个作业
uv run python -m tianshu worker           # 跑到队空 / 第一次 contended 为止

# 改目标：旧 Run 立刻失效，待批请求作废，历史保留
uv run python -m tianshu task revise --task-id task_xxxx --goal "换个目标"
```

退出码：`0` 有结论且成功，`1` 失败/取消（`worker` 有被接管/结算失败时也是 1），
`2` 用法或前置条件错误（含 `recover` 发现未核实效果），`3` 需要人决定
（等审批 / 已暂停 / 待核查）。
`undo` 不带 `--approve` 时只预演并返回 `3`。

接真实模型（OpenAI-compatible）：

```bash
export TIANSHU_API_KEY=sk-...
export TIANSHU_BASE_URL=https://api.openai.com/v1   # 可选，支持任意兼容端点
uv run python -m tianshu run --workspace /tmp/ws --task "..." \
  --out summary.md --model openai-compatible --model-name gpt-4o-mini
```

## 开发约定

- Python **3.12**（不是 3.14）。用 `uv`。
- 依赖必须四目标可用：`mac-arm64` / `mac-x64` / `win-x64` / `win-arm64`。见 TECH-ARCH §8.3。
- 事件日志 = SQLite WAL 单提交点。**不要**用文件级 event log。见 PRD §5.3。
- 授权只在执行侧判定。见 PRD §8.1。
- 提交前跑 `uv run pytest`。

## 数据目录

运行时数据放在系统标准位置，不放进本仓库：

- macOS：`~/Library/Application Support/AgentOS/`
- Windows：`%LOCALAPPDATA%\AgentOS\`

## 注意

本目录在**外置卷** `/Volumes/Date` 上。务必：

1. 经常 `git commit`
2. 尽早配置远端并 push（卷拔掉就没了）
3. 不要把 `refs/`（762MB 参考仓库）放进本仓库
