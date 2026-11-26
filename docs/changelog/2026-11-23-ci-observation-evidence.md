# CI 观测与证据假设修复

状态：PR #228 的后续修复候选，未合入、未发布；#224 保持开放。

## 原始失败

v9 head `a4e48a0c2f77aedc198391feeeed55c1906cf1b3` 的完整 run
[45170896526](https://github.com/lorek666/tianshu-agent/actions/runs/45170896526)
整体失败。两台 Windows job 和 macOS Python shard 1 断言失败，两个 required
汇总正确拒绝；各片已完成其选集，不是收集或超时问题。四份 ZIP 已核 GitHub
digest，全部 job 日志与六份原始报告保留在忽略目录
`.omc/artifacts/native-process-isolation/`，不重跑原 run 追绿。

## 修复范围

- Windows 并发测试不再假设 supervisor 的 `Popen.pid` 就是解释器 PPID。
  使用真实 checkout、插件分片索引、对应 pair 的精确 basetemp/cache 路径及四 worker
  同时等待的 TCP 屏障，仍验证私有维护锁、registry、authority 和子进程继承。
  真实中间 launcher 已使原 PPID 断言失败；整组交换分片及目录的负控须被
  checkout 对照拒绝。先放行并回收屏障中的进程，再判断已取得的归属样本。
- SLO/soak 孤儿观测记录 PID 与原生创建身份，保留同 PID 的每次观察。
  已知不同实例不计为旧进程残留；初始或最终身份不可读、且进程仍活则保守报告。
  POSIX marker 扫描及零孤儿判据保留。真实存活子进程配合受控身份映射证明
  原纯 PID 判据会误报；没有声称托管现场已证实 PID 复用。
  锁文件仍是在所属阶段读取号码并查询当前身份，不构成经过认证的父链或句柄归属；
  旧锁被重复读取等观测歧义未被全面排除。本修复不根据历史裸 PID 发信号，
  若确有原实例残留，零孤儿门禁仍须失败。
- 开发 `state` 诊断在 `subscribe(0)` 回放已有大历史时立即测量，不再串行等待
  一次多余大写。回归等同一订阅者真实 watchdog drop 后才放行旧大写，原流程
  得到无效样本；原 after-drop 负控仍拒绝。正式 SLO 工作负载、timeout、watchdog、
  报告 schema、样本资格及阈值不变。
- 网络重试测试保留真实 socket 连接失败和错误映射，仅用注入时钟区分共享
  deadline 尚余或耗尽，严格断言两次或一次。旧测试无条件要求两次，与已有
  总期限语义冲突。受控耗尽已复现相同断言失败，但没有原生首试耗时可证明
  CI 现场的精确时序；产品网络实现不变。

不新增依赖、授权来源或产品契约；不放宽报告验证、不增加重试或 job 时限、
不删除测试或增加 skip。新的本机冻结、独立复审与完整 hosted CI 均须另行绑定，
不能套用 v9 的本机通过。Windows 原生沙箱、任意后代监管及远程 MCP 隔离仍未交付。

## 最终本机冻结

v10 的裸全量曾退出 0，但暂存区差异检查因新测试末尾多余空行退出 2，
整轮未验收。v10b 仅删除该额外 LF，已核对前后 AST 相同；历史日志与失败门禁保留。
此后重新冻结，并在自己的候选上运行裸 `uv run --frozen pytest`、
`uv lock --check`、Ruff、工作树及暂存区 `diff --check`，均实际退出 0。
验证环境为 macOS 26.6.2 arm64 / Python 3.12.13，不是 Windows 用户设备证明。

- 非文档清单 SHA256：`667dd6561a44065091e1cded195428891d2a0ec8d6b38c80c76953b351645902`。
- 产品 `src` 树保持 `fc2e66ea60b5befb489f840248a80eaae3a3fa58`；
  测试树为 `6c1a0c3d42d34a71d657256430d7bf4a08e6351b`。
- 本轮裸全量日志 SHA256：`c1b23b5feb998f6c6d6dcf281b48111fd07a4ff84ecb33ff5ba90277c537728f`。
- 第二读者限定报告 SHA256：`be68d51afc531c2d3c88a94a32cd0e75ee37a3704e71b1d87d00bddb1a58cce8`；主控逐项核验原件，
  并在新的私有副本完整重放正常及负控探针，验收回执 SHA256：
  `f5d11a4473a4fb464f0db8b4d0c8126d087de25f7e86b3b111b35899109768bc`。

最终提交 SHA、固定 base、合成 merge、全部 job 与原始分片报告的托管验收，
以 [PR #228](https://github.com/lorek666/tianshu-agent/pull/228) 对精确对象的记录为准。
本段只记录本机冻结；draft 跳过或历史 CI 绿色均不能证明新提交通过。
回填文档后须再核非文档清单不变，并完成 lock/Ruff/diff 与本地链接检查。
