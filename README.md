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
