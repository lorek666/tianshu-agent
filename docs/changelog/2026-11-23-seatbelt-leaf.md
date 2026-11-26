# macOS 单进程隔离增量

状态：#224 的受限候选，堆叠于 #227；未合入、未发布，#224 保持开放。
不改已接受的 #225/#227 冻结，不沿用旧候选 CI 为本批背书。

## 原因与决定

旧 Seatbelt 可以派生脱组后代，进程组消失不足以证明整个受控集合退出。
本批新增显式 `seatbelt_leaf`，复用既有文件、网络和凭据策略，固定禁止
`process-fork`。不需要派生的命令和 stdio MCP 可逐 subject 实测准入；
需要 `fork`、`posix_spawn` 或 `subprocess` 的正常程序同样失败，不回落。
默认 `local`、旧 Seatbelt 和 `auto` 的选择保持不变。

探针版本从 `v2` 升至 `v3`，直接验证 `os.fork()` 拒绝、根继续阻塞、
真实超时和受管终止，拒绝用启动失败或 `Popen` 的拒绝 errno 冒充派生隔离。
固定 profile 和私有临时目录均纳入原 subject 摘要，不新增授权来源、事件或工具 schema。
这是已有可扩展后端接缝的受限实现，不改变 C1–C5 契约或 PRD 的交付范围。

独立设计审查发现受管根已回收后继续 `killpg` 的身份复用窗口。
新单进程路径只等待/终止未回收的受管根；已回收则直接确认退出。
旧允许派生的后端保留保守边界。MCP 未完成调用及崩溃后身份未知继续阻断。

首轮冻结独立复审另发现 P1：内部 `python -c` 从待审工作区导入同名模块。
真实回归复现普通探针、detached 根和其子进程会执行项目模块；
修复仅为内部根/后代探针增加 `-I -S`，真实业务 argv 保持原样。
新增工作区模块、`PYTHONPATH`、site 钩子及无 fork 平台分支的回归；
移除四个入口中任一处隔离参数，均要求测试真实失败并核验源码字节恢复。

## 验证

原始证据保存在本工作树忽略目录 `.omc/artifacts/native-process-isolation/`；
不提交运行数据。最终 PR 正文绑定完整提交、code/test tree 和本批 hosted CI。

- `red-review.log`：新后端注册前，原生业务测试因后端不存在真实失败。
- `green-first.log`：首次实现后的原生测试通过；后续增补的负控与全量另行留证。
- `targeted.log`：原生新后端、旧后端隔离、subject、注册与门禁的受影响回归。
- `negative-controls.json` / `negative-*.log`：移除禁止派生、恢复回收后裸 PGID
  清理及移除根终止证据检查，要求相应测试真实失败；恢复原字节并核对 SHA256。

首轮冻结后的本机 `uv run --frozen pytest`、`uv lock --check`、
`uv run --frozen ruff check .` 和工作区/索引 diff 检查均 exit 0；
命令、退出码、耗时及原始日志 SHA256 见 `local-gates.json`。
本机环境为 macOS 26.6.2 arm64、Python 3.12.13。冻结的 source/test tree 分别为
`c1d5d051dacd6fd08c2113e9201d1d543c845a17` /
`6145ebed7be7dcfa271c27ca9c45fbd9c435c0d2`；
全量日志 SHA256 为
`6af30d3fea91f3353e3bc981c4c201fb28521086ba18954022137fcb818af5a6`。

该首轮本机绿不抵消独立复审的 P1，也不用于修复后新树的验收。
修复后 `src=fc2e66ea60b5befb489f840248a80eaae3a3fa58`、
`tests=14588a275dbf2bd28acce3a566600ce243ae5d39`，非文档清单 SHA256 为
`dc27f3b69a3aec63bf53ec40bbc195be2395d838ab664f13e821cdbe86791e76`。
`seatbelt_leaf_frozen_review` 的限定增量复审已将 P1 标为「已修」：
原红测转绿，四个内部解释器入口与真实业务导入对照均通过，未发现新 P0/P1。
原件在 `second-reader-v2/report.md`、`native-v3.log`、`original-red-now.log`、
`targeted-v3.log` 和 `preserved.log`。该复审沿用旧报告未受影响部分的原范围，
不替代 Windows 原生清理、任意后代隔离或整体验收。

中间一次全量因作者修正新增 fallback 测试的清理入口而主动中断，实际 exit 2，
`full-suite-v2.log` 与 `local-gates-v2.json` 保留，不计为绿。
测试现在使用产品完整探针的 PID 清理路径；目录清空断言不等于 Windows 后代死亡证明。
最终新冻结的裸 `uv run --frozen pytest`、`uv lock --check`、
`uv run --frozen ruff check .` 和工作区/索引 diff 检查均 exit 0。
完整命令、退出码及证据哈希绑定在 `local-acceptance-v3.json`；
全量原始日志 `full-suite-v3.log` 的 SHA256 为
`53fb9e54812a5caf51a61c33aed1f6fa6bd1b986d12a21e05615c3edce0efcc5`。
最终代码/测试树与上述独立增量复审对象一致；后续提交前只有文档回填，
不把纯文档 Git tree 的变化冒充代码变化，也不借旧候选的全量通过。

本记录冻结于提交前。该提交自身 hosted required CI 的 run/head/base、
合成 merge `GITHUB_SHA`、全部 job 结论及 Windows 两片原始报告严格核验，
由最终 PR 正文和对应 Actions run 单独绑定；本机绿不能替代。
CI/本机动态总计与跳过原因以原始日志为准。

## 未交付边界

Windows AppContainer/Job Object、允许任意派生后代的进程树监管和远程 MCP 沙箱
仍未实现。`process_tree=enforced` 对 leaf 仅表示集合最多有根且根退出已确认。
CI runner 不替代用户设备验收，不扩张到未实测的 OS/架构或桌面安装体验。
