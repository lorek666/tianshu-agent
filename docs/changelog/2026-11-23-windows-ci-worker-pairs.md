# Windows CI 隔离 worker 对

状态：PR #228 的后续 CI 修复候选；未合入、未发布。产品 `src` 保持原冻结，
不修改 #225/#227，不把本批 CI 工具当作 Windows 产品沙箱。

## 原因与范围

首轮 `45134199845` 的 Windows 分片0触及25分钟上限，完整报告的 session exit0
没有覆盖 job 取消。保留慢路径估计的 `3eeb577` 后续经历一次并发组替换取消
`45141639988`，再经历完整运行 `45142192772` 的两台 Windows job 均超时。
两者均无完整分片报告；Windows required 正确失败，其余六项成功。
全部原件留存于忽略目录 `.omc/artifacts/native-process-isolation/`，
不重跑旧 run 洗绿，不提高时限或减少测试。

目录编号扫描的优化假设已被真实双进程探针否定；锁定 pytest 的实际临时目录
分配使用计数器。没有修改 `conftest.py`，也不将该假设记为性能改进。

本批保持两个独立 Windows runner，每台先启动两个原诊断 wrapper 真子进程，再等待。
四索引使用同一真实 collection、原有整文件分组和全部测试；正常失败仍等另一 worker
留下原始报告，任一 wrapper 非零都使父调用失败。每 worker 私有 basetemp、
cache_dir；报告上传树不混入运行数据。原默认 wrapper 的诊断/fatal 路径不变。

两个具名 artifact 分开下载并严格核对所属索引，才做 count4 全集核验；四份报告全绿
但错放到另一 runner artifact 仍须失败。count2 历史入口保留，未知 count 拒绝。
四 required 名称、两个 runner、25分钟、锁文件、lint、原生 fatal handler 和60秒
线程栈纪律均不变。无新增依赖或产品契约变更。

## 验证与边界

原 wrapper 不认识 pair 参数的真实 RED 保存在 `ci-pairs-red.log`。
独立设计复审要求保留 runner 归属、真实退出及受管根回收、缓存隔离；
对应实现必须由定向红绿、负控、新冻结全量与绑定的独立增量复审共同核验。
最终 PR 另记录精确代码树及新完整 hosted CI；尚未完成的验证不能沿用旧候选的绿。

v5 冻结的本机裸全量、lock/Ruff/diff 通过；独立复审仍判为部分修：第一个根
`terminate()` 抛出系统错误会跳过后续清理，父进程非零但两个根仍存活。
新回归以真实双子进程及管道就绪确认复现该缺陷，修复后转绿。修复逐根独立处理
系统错误，保留首个错误；清理等待与强制终止后的等待均有界，无法确认回收明确
报告。已回收根不再收到信号。v6 限定独立复核将该 P2 标为已修。

提交前另发现新测试的 LF-only 管道握手不兼容 Windows CRLF。真实管道强制文本
CRLF 后复现失败；测试现显式覆盖 LF/CRLF 两种文本输出。v6 全量为修正此测试
主动中止，实际 exit2 保留，不作通过证据。v7 仅改变这份测试，产品与 supervisor
不变；v7 自己的受影响域、裸全量、lock/Ruff/diff 与测试差异限定复核均已通过，
不沿用 v5 绿或 v6 中断。环境为 macOS 26.6.2 arm64 / Python 3.12.13。
非文档清单 SHA256 为
`d1922a038d823360ac90fe182fe01db6ea81349cc35100f5ee41030793eb3bd8`；
裸全量日志 SHA256 为
`156fe7b31b4cf7ca837e9931f610c94e01e38f7d7847fd351a19613b58cd28bd`。
本机与复审回执分别为忽略目录内的 `local-acceptance-v7.json`、
`review-acceptance-v7.json`；六项变异均真实失败，源码逐字节恢复。
本机模拟两种换行不是 Windows 原生验证。提交时新 hosted CI 待验，
墙钟、OS 行为及最终同 head/base/merge 的验收由 PR #228 与原始报告单独证明。

v7 提交 `416fd45` 的新完整运行
[45160677871](https://github.com/lorek666/tianshu-agent/actions/runs/45160677871)
在两台 Windows runner 的 collection 阶段失败：新增模块级 count2/count4
参数化 fixture 触发 pytest 内建重排，严格顺序守卫正确拒绝。两份原始 ZIP 的
GitHub digest 已核对，四份报告均为 session exit3、选集及执行阶段为空。
该轮不是通过，也不作为 worker 吞吐证据。

后续 v8 仅修测试组织：参数化改为函数级，模块级只保存空缓存，按 count 各复用
一组真实子进程证据。收集守卫、supervisor、诊断、产品源码和覆盖范围不变。
新增回归读取真实 `test_pytest_ci_shards.py`，通过原 wrapper 对复现同一 collection
错误；修复后四 worker 均完成收集。探针刻意不执行用例，原始报告仍被严格核验器
以缺少完成节点拒绝，不能当作执行通过。v8 新冻结的裸全量、lock/Ruff/diff
及限定独立复核均已通过，非文档清单 SHA256 为
`a4e482f6305d4ebd5d4e6682bcdba995608c8586cc5bffa5454a31a74dc86b23`，
全量日志 SHA256 为
`972cda40c19ec7e653fc8e4f0bb226386c06baa9fd2d335d0fc769a2fbf77a7a`。
独立复核确认原断言未变，count2/count4 各仅生成一组真实执行 cohort。
完整 hosted CI 尚未验收，不能沿用 v7 本机的绿。

同一 v7 完整 run 的 macOS Python 也实际触及25分钟 job 上限而取消，
macOS required 因此失败；不是 runner 排队导致的执行超时。PR 已转回 draft。
此独立问题的后续修复见[macOS CI 分片](2026-11-23-macos-ci-shards.md)。

本机并发屏障不证明 hosted Windows 的墙钟。启动失败或可捕获中断仅清理尚受管的
worker 根；父进程被强杀无法保证执行收尾，任意后代仍由 runner 负责。
Windows 原生沙箱、通用进程树监管和远程 MCP 隔离未交付，#224 保持开放。
