# 项目状态与门禁

> 本文件从 README 拆出。**不手写测试总条数与模块总数**（AGENTS.md §2）：
> 通过/失败数字一律以该次提交的 CI 日志与验证记录为准，本文件只记「在哪个 SHA、用什么口径验的」。

## 里程碑

**形态**：M0–M4 是命令行内核；M5 交付 Daemon、Python Host 与 React UI 的三进程闭环；
R0–R3 是 Tauri 桌面壳与发行构建链。

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** 骨架 | EventLog + E1 闭环 + StopReason/Limits | ✅ 完成 |
| **M1** 副作用 | intent/result + Receipt + 写前快照 + 恢复 | ✅ 完成 |
| **M2** 权限与任务 | C4 审批状态机（写 / 删分级）+ Task/Run + 作业表租约 | ✅ 完成 |
| **M3** 可替换 | 第二个 chat provider + 内存文件后端（不提供进程隔离） | ✅ 完成 |
| **M4** 可评估 | 固定案例套件，`replay` 与 `evaluate` 分离 | ✅ 完成 |
| **M5** 桌面闭环 | Daemon/Host/UI 三进程打通 + 权限审批 UI | ✅ 完成（PR #49） |
| **R0–R3（拆分）** | R1 Tauri 薄壳；R2 锁定 Python/UI bundle 与构建；R3 安装/卸载与发行草稿 | 🟡 四目标基线已并入 `main`（PR #170 / #175 / #186）；`e87a38c` 的单目标预览 workflow 已成功生成未公开的 `v0.1.1` prerelease 草稿，但不是当前主干候选。真机安装/升级/回退、候选 SLO、真实模型 UI 与四目标正式发行仍待验收 |

**当前主干与预览草稿（2026-11-24）**：`e87a38c` 的 [release run 44892061206](https://github.com/lorek666/tianshu-agent/actions/runs/44892061206) 成功，以 `workflow_dispatch` 的 `version=0.1.1`、`macos_arm64_preview=true` 生成[未公开的 prerelease 草稿](https://github.com/lorek666/tianshu-agent/releases/tag/v0.1.1)，含六项资产；`runtime_smoke=pass`、`device_verification=unknown`、`signed=false`。其同 SHA [main push CI 44890059535](https://github.com/lorek666/tianshu-agent/actions/runs/44890059535) 九项 job 全部 success，但当前 `origin/main=c5fcff0`；该主干的 [push CI 45078725688](https://github.com/lorek666/tianshu-agent/actions/runs/45078725688) 因 Windows shard0 达到 25 分钟上限而整体 cancelled。因此该草稿只证明 `e87a38c` 的预览构建已实跑，不是当前主干候选，也不等于已发布或真机验收。

W9 网络校验与分页、W14 macOS 和 Linux 子进程沙箱、SLO/Soak 测量工具已随 [PR #196](https://github.com/lorek666/tianshu-agent/pull/196) 合入。首发 `macos-arm64` 限定预览的最终候选 SLO、资源采样和用户设备验收仍待执行；按 [PRD §10.6 / §11.1](PRD-v1.md)，**已取消强制 ≥10h soak，无替代时长**，现有工具/测试/阈值仅为可选、非阻断诊断，长时稳定性未验证。四目标正式发行的长时 Soak 要求不变；Windows 系统凭据后端尚待交付。

受管恢复 B/C 和 W15 离线 GC 的集成与验收见 [PR #198](https://github.com/lorek666/tianshu-agent/pull/198)。受管写入需要支持目录 `fsync` 的 POSIX 文件系统。Windows 的受管写入及含文件候选的 GC 批次会拒绝执行。操作与平台范围见[备份与恢复指南](backup-restore.md)。

## M5 桌面闭环实测

**当前测试数字以该次提交的 CI 日志为准**，本文件不手写总条数（AGENTS.md §2）。

M5 收尾轮（macOS 本机，2026-11-11，`43d04ef`）的通过数与完整判据见
[M5-verification.md](M5-verification.md)。该轮验到的**进程语义**（不是计数）：三进程真进程链路跑通——
`tianshu host run` → `tianshu host stop` 后 Host 与 Daemon 都退出（`ipc.probe == False`，不留孤儿），
`scripts/demo_m5.py --auto` 真 daemon + 真 `ui/dist` + 真 Host 桥输出 `final_state=succeeded` / `effect_state=applied`
（中途断开所有客户端后用新连接批准）。完整判据、未验证项与真进程截图见 [M5-verification.md](M5-verification.md)。

## M4 评测口径

**历史 M4 实测**使用 `m4.4`。当前套件为 `m4.6`，判据已变更，报告不可直接横比；安全门槛不能被其他分数抵消。各轮结果与真实模型范围见 [评测边界](limitations.md#4-m4-评测的未验证项)。
M5 同批修掉了 issue #40（**否决即终局**）：`approval="reject"` 的判据改为 `approval.veto_terminates`，
并保留「无重复审批请求」的机械判据。

## CI 门禁

GitHub Actions。**四项 required 全部必过**：Ubuntu / macOS / Windows Python 检查 + UI 检查
（`ui · typecheck / test / build`，含 `git diff --exit-code -- ui/dist` 产物漂移门禁）。
准确检查名称见 [CONTRIBUTING §6](../CONTRIBUTING.md#6-pr-要求)；任一失败或未完成均不能合并。

Python 矩阵为三平台 × Python 3.12。Windows 用多个独立 runner 分片 + 原生命名管道/长等待诊断，
分片数量与权重以冻结工作流为准，不减少测试、不提高 25 分钟上限；当前 `c5fcff0` 的 shard0
超时修复候选见 [PR #229](https://github.com/lorek666/tianshu-agent/pull/229)，其[托管 run 45324054878](https://github.com/lorek666/tianshu-agent/actions/runs/45324054878)
整轮十个 job 已全部成功，包括三个 Windows 分片与 required 汇总、macOS Python、Ubuntu、UI 与桌面 Rust；候选尚未合入 `main`，当前主干状态不变。
由汇总 job 绑定各片 `success`；R1 的 macOS Rust fmt/test/clippy 并入现有 macOS required job，
Windows Rust 检查由现有 Windows required 汇总 job 绑定，**不新增 required 名称**。

两条容易被误用的门禁口径：

- **锁文件漂移只能靠 `uv lock --check` 查**。`uv sync --frozen` **不是**漂移门禁（`pyproject.toml` 变了它照样 exit 0，实测如此）。
- 全程 `uv run --frozen`，防止顺手改写锁文件绕过上面那条。

`main` 已开分支保护：必须走 PR、`strict`（要求分支与 main 同步）、`enforce_admins`（所有者也绕不过）、禁强推 / 禁删除。

完整命令、Windows 分片与诊断参数、25 分钟上限与跳过原因口径见
[CONTRIBUTING.md](../CONTRIBUTING.md) §5「提交前检查」与 §6「PR 要求」，以及 `.github/workflows/ci.yml`。

## 相关文档

- 能力边界与未验证项 → [limitations.md](limitations.md)
- 各里程碑判据与证据 → `M0-verification.md` … [M5-verification.md](M5-verification.md)
- 全量工作流账本与外部阻塞 → [ROADMAP-COMPLETE-AGENT-2026-11-14.md](ROADMAP-COMPLETE-AGENT-2026-11-14.md)
- 当前状态、关键决策与踩坑教训 → [KNOWLEDGE.md](KNOWLEDGE.md)
