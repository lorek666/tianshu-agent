# PR #228 stacked refresh 与 CI SLO 顺序隔离（2026-11-26）

## 背景与真实失败

- 旧 head `0cacbbe9907368ed37fe6a7a3af5064dc2c790d6` 的 hosted run
  [`45722985924`](https://github.com/lorek666/tianshu-agent/actions/runs/45722985924)
  不再出现 state-pressure 诊断缺 `CI_SHARD_INDEX` 的错误；macOS count2、macOS required、
  UI、Windows shard0/1/2 与两个 Rust job 成功。
- 同轮 Windows shard3 仍由 `startup_cold_ms p95=2264.22ms > 2200.0ms` 失败，
  Windows required 严格拒绝。旧失败保留，不用重跑覆盖。

## 刷新与合并决策

- #225 hosted-green head `d8b47a75affd9ebc5ca004f8ed88d479f97acb8e` 与 #227
  文档收口 head `d55568f928d7d2b09bba167b41da05fb72871235` 通过正常双父合并纳入，
  不 rebase、不改写共享历史、不合并 PR。
- 保留 #228 的显式 macOS `seatbelt_leaf`、Host/停止身份修复、Windows 四独立 runner、
  state 背压诊断工件、真实 artifact attempt 解析和严格报告树。
- 保留 Windows count4、文件权重、25 分钟上限、SLO 阈值、repeats、soak 验收、
  完整 collection 与 required 名称不变。
- `full_collection` 保留 pytest 原始收集顺序；`partition()` 不变。
  `ordered_shard()` 只把 `test_measure_slo_smoke_and_run_soak_real_subprocesses`
  移到所属分片首位；pytest `items` 与 verifier 使用同一确定性顺序。
- `macos_summary` 与 `windows_summary` 均显式授予 `actions: read`；macOS/Windows
  Pytest step 均显式传入 `CI_SHARD_INDEX`，诊断目录仍位于严格报告树之外。
- 网络重试回归合并时保留 #228 的 deadline 参数化，但连接失败改为确定性注入，
  避免 macOS bound-but-not-listening 端口表现为超时后耗尽总 deadline。

## 当前验证与边界

- 合并后的 `tests/test_net_resilience.py`、`tests/test_ci_workflow.py` 与
  `tests/test_pytest_ci_shards.py` 定向回归 exit 0；actionlint 与 diff 检查通过。
- 新代码树仍须冻结并执行本机全量、独立第二读者 Tier 1 复审和最终静态门禁。
- 新 head 的 hosted required CI 必须单独完成；定向绿、#225/#227 旧绿或重跑后的绿
  都不能替代。该修复不声称解决所有宿主 OS 负载抖动，也不放宽任何 SLO 或验收阈值。
- 测试使用短 `TIANSHU_TEST_TMP_ROOT` 隔离临时根，不触碰真实 Keychain、生产凭据或全局锁。
