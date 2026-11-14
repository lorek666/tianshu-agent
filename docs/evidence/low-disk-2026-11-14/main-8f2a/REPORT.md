# main 8f2a 自身 push CI 独立证据验收

结论：**PASS_MAIN_PUSH_CI_8F2A_COMPLETE_EVIDENCE_ONLY_WITH_UNRESOLVED_145**。

本报告只验收 main 自身 push CI 的完整原件与精确对象绑定。受审提交 `8f2a508e68f27efc4100b7466fcd79043341f7fa`，tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`，有序父提交为 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215`、`9413c7ca66af56fce8ac1664e989cf8cd6fc9808`。原生 run `43555557537` attempt 1，event `push`、branch `main`，没有 previous attempt，结论 success。六份 checkout 日志均指向该 main 提交；本报告不套用 open/unmerged PR 的 `verify_pr_objects()` 条件。

## 独立核验与证据

输入为 `/tmp/tianshu-g3-20261113/low-disk-main-8f2a-native-ci` 的最终原件，以及 `/tmp/tianshu-g3-20261113/main-8f2a-closeout-0ap253ev` 的只读 closeout 原件。没有采信 root 的 `result.json` 作为审核结论。`logs/original-source-bindings.json` 记录实际使用的 59 份外部原件路径、字节数和 SHA256；读取结束再次核实这些原件未变。

已核对 run、jobs、check runs 的精确 ID、attempt、head、suite、GitHub Actions app `15368`、名称、URL、status 与 conclusion。final collector 与 fresh closeout 的六 job 集合一致；fresh jobs/checks 的 `total_count` 均为 6，与实际数组长度一致，低于请求的 per_page 100，没有截断。所有 70 个步骤均 completed/success，上传、汇总、typecheck、UI test、build 与 dist 一致性步骤均成功。

六个 job 的结果：

- `132327500933` Ubuntu Python：3178 passed / 13 skipped，445.75 秒。
- `132327501220` macOS Python：3183 passed / 8 skipped，764.93 秒。
- `132327501132` Windows shard 0：1584 passed / 12 skipped / 1595 deselected，792.09 秒。
- `132327501025` Windows shard 1：1570 passed / 25 skipped / 1596 deselected，506.15 秒。
- `132327501172` UI：13 文件、91 测试通过，typecheck/build/dist 检查成功。
- `132331156479` Windows required 汇总：在 Ubuntu runner 验证 upstream success 与完整覆盖，成功。

Windows artifact `14098252539`（shard 0）和 `14098322729`（shard 1）的实际 ZIP SHA256 均等于各自 metadata digest，实际字节数相符；ZIP 唯一成员为 `shard.json`。严格 JSON 解析拒绝重复 key。两份完整有序 collection 一致，共 3191 节点；规范序列化 SHA256 为 `56c351f349470c04fda4682b2bae04e1e0d01750614da5b37e1161184a534f34`。独立重算文件分组得到 1596 / 1595，互斥且并集为全集，selected 和 finished 顺序均精确匹配；所有阶段仅 passed/skipped，没有缺失、重复或失败阶段，pytest session exit 均为 0。

两项目标均在 shard 1，以下精确 nodeid 的 setup、call、teardown 三阶段全部 passed：

- `tests/test_low_disk_run_creation.py::test_two_real_processes_reopen_and_compete`
- `tests/test_m2_acceptance.py::test_normal_finish_and_pause_do_not_report_heartbeat_failure`

`logs/artifact-ordered-coverage.json` 保存阶段计数、跳过阶段和两项目标阶段记录；`logs/jobs-environments-skips-annotations.json` 保存各原日志跳过原因、行号、运行环境、步骤及 annotation。各 Python 日志跳过原因计数与 summary 相符。该次真实环境为 CPython 3.12.16；Ubuntu 26.04.1 x86_64、macOS 26.7.2 / 25G332 arm64、Windows Server 2025 / 10.0.26100 Datacenter x86_64。Windows required 汇总 job 的实际 OS 是 Ubuntu，并非额外一次 Windows 测试。

这次共 **1 次** `PYTHON STACK WATCHDOG`：Windows shard 0 原日志 1470 行出栈，1540 行 `tests/test_recovery.py::test_100_random_fault_injections PASSED`。短原文在 `logs/watchdog-132327501132-0.txt`，索引见 `logs/watchdog-observations.json`。未见 watchdog failed、stack collection failed、未能停止或 session 结束残留等真实失败标记。后续 passed 不证明所有时刻都能及时出栈；C 扩展长期独占 GIL 时仍不保证 60 秒内取得栈。

annotation 与对应 check-run 数量和分页原件一致：6 warning（Node.js 20 action 弃用）/ 4 notice（Ubuntu label 迁移或 macOS arm64 队列提示）/ 0 failure。它们被明确保留，没有描述成“无警告”。

fresh closeout 的 main ref、commit tree、有序父提交及已合并 PR #144 精确一致；PR head 为 `9413c7ca66af56fce8ac1664e989cf8cd6fc9808`，merge SHA 为受审 main，合并时间 `2026-11-14T04:13:29Z`。四项 required context 及 app 绑定、strict、enforce_admins、禁止 force push/delete 均保持，批准人数配置为 0。本报告没有执行合并或任何远端写操作。工作树之后切换到下一项文档分支不改变这些固定对象和原 CI checkout 证据；workflow/plugin/wrapper 采用 `git show 8f2a…:path` 核 SHA256。

## 未解决项与结论边界

fresh issue #145 仍 open，`closed_at` 为 null。旧 run `43549430150` 的真实 normal-finish Run failed 根因仍未知、未修；没有证据将其归于 baseline、心跳、SQLite、锁或低磁盘逻辑，也不能排除 PR 回归。issue 正文仍含前一个诊断 run “进行中”的过时进度句，本报告没有用它判定当前 run 状态；当前结论来自此次 completed native 原件。

60 次正常收尾加暂停的断言保留情况、诊断注入有效性及旧失败原件，沿用 `facts.json` 中以完整 SHA256 绑定的既有独立报告，本轮没有重跑那些行为、旧门禁、full/UI 或旧平台矩阵。新 main 的绿灯和已合并状态都不自动关闭 #145。既有 BASE-B1 预算接线缺口同样没有被这次 CI 解决。

维护者已在合并提交说明中接受内部研发交付风险；这不是审核者对整 PR 的无条件批准。本轮未发现新的 main CI 证据阻断项；仅能确认该 exact main 的原生 push CI 证据机械完整通过，不扩大先前产品批准范围。没有宣称用户 Windows 10/11、Windows ARM64、实体断电、真实付费模型、10 小时 soak 或发行环境验证。

## 可复算与封存

纯证据解析脚本为 `probes/check_main_native.py`，只读取已有文件和固定 Git 对象，使用已审 v3 纯函数验证身份、分组与阶段；不调 API、不执行产品测试、不修改 live。运行命令：

```text
PYTHONDONTWRITEBYTECODE=1 /Users/ronfee/.codex/worktrees/g3-data-admission/天枢Agent/.venv/bin/python -B /tmp/low-disk-main-8f2a-native-independent-20261114-iv_wwmfk/probes/check_main_native.py
```

首次自有解析脚本误以为六源 freeze 还包含 wrapper，触发 `KeyError`，未产生 PASS 事实。已按实际 freeze 范围修正：workflow/plugin 同时绑定 freeze 和固定 Git 对象；wrapper 绑定已有精确 SHA256 与固定 Git 对象，未放松任何产品/CI 检查。修正后的完整解析退出 0；该过程不属于产品测试失败或重跑。

包内仅保留脚本、派生事实、少量原文和索引，没有复制六份大日志、ZIP 或旧证据包。所有文件 0444、目录 0555；`index.json` 覆盖除自身外全部普通文件。封存后通过独立只读 verifier 核对文件集合、SHA256、字节数、权限和无 symlink。`approval=false`，没有 API 调用、live 写入、CI rerun/dispatch/cancel 或 merge。
