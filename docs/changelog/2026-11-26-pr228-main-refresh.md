# #228：同步 Agent 主循环与诊断工件

## 与排队验收证据一起交付

#225 已合入 `3017f09740756da0c3db7e02d1568b2eeced4da3`，该主干 CI
[45854841880](https://github.com/lorek666/tianshu-agent/actions/runs/45854841880) 全绿。
#227 刷新 head `74a38eb5f7a449890cbe7e578c24848c1e9d91b3` 已包含该主干，
但其独立 [CI 45855743069](https://github.com/lorek666/tianshu-agent/actions/runs/45855743069)
在 Windows shard 3 的 SLO 烟测失败：重启恢复 p95 为 `2389.939 ms`，超过 `2000 ms`；
报告未发现孤儿进程。该失败保留，不能作为通过证据，也不足以判定延迟根因。

本候选用正常双父合并把 #227 的真实入队验证与 #228 已实现的原生解释器启动、
受管进程回收和原始样本诊断一起提交 `main` 审查。Windows 重启恢复测量包含实际启动，
不变更阈值、计时窗口、数据集或分片验证规则。除文档外，合并树与本地裸全量退出 0、
独立复审以及主控重放绑定的预览提交 `7ced771241591d26416c073c164dd4b73d741095`
逐字节一致。新的组合 head 仍须通过本身的完整 GitHub CI。

- 保留旧 #228 的 `seatbelt_leaf` 后端、执行准入、进程监管与 SLO/soak 门槛；四个 Windows runner 与两个 macOS runner 的 CI 工作流不变。
- 合并 #225 带来的非法分片计划负控时，保留 #228 按分片写入并上传的脱敏背压诊断。直接叠加会产生重复上传，旧路径还可能缺件。用真实测试复现错误工件名，合并后每片仅保留原来的诊断目录上传。
- SLO 对 Windows Python launcher 的替换保留子进程显式源码搜索路径优先级。两份临时模块的真实子进程测试在改动前加载父进程副本，改动后加载指定候选。

冻结树 `f6ca078a6f3fddf3ce52f1852dc7ee4c61b53c50` 已通过裸 `uv run --frozen pytest`、
受影响域回归、lock、Ruff、actionlint 和 Git 空白检查。独立第二读者判定本次增量「已修」，
主控逐项核对证据清单并在独立导出完整重放。重放验证真实子进程读取指定源码、背压报告与
分片验证、原生 `seatbelt_leaf` 准入和根进程回收；两项负控分别检出父路径覆盖候选源码和重复上传。
全量日志 SHA256 为 `8660547605b5bc00e2a88d6fc029110014a43309680e62ea13fe7ea3736340a1`，
独立证据清单 SHA256 为 `e348e9a0dbe86c528043837bdb2a59b4cdf2e058b637c73cfe6bb449bafeee92`。

本机与复审结果不替代实际合入候选的完整 GitHub CI。Windows 原生沙箱、任意后代监管、
远程 MCP 隔离仍不在此 PR 交付范围。[#224](https://github.com/lorek666/tianshu-agent/issues/224)
继续追踪这些未交付能力。
