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
- 冲突解决初稿误用 #228 旧分片调度器，漏掉 #225 的 `SLO_FIRST_NODE` /
  `ordered_shard()` 与对应回归；该遗漏由文档—代码对账发现，初稿冻结 `6c36f89` 与中断的
  全量日志保留为调查证据，不作为通过证据。修复提交 `e3e6726` 只补回确定性顺序和测试，
  `a2e0ba0` 再加强断言，要求所有分片成员集合与未改的 `partition()` 完全一致；保留 #228
  的较大文件权重、四 runner/count4、诊断工件和严格 verifier。

## 当前验证与边界

- 正常双父刷新提交：`6c36f89f7a822e52285bffd1fecb314e1d52fe46`；双亲为 `0cacbbe` 与
  `d55568f`。SLO-first 漏合并修复与成员断言加强后的最终代码 head：
  `a2e0ba0274d544e1191eda95ded660ac5bc7370f`；Git tree：
  `7e30ed5398b0021aaee169f1b3c61aab96648b4d`。
- 最终 code tree SHA256：`05386a3bc8d0b468c714657a4345f0d3e7a20df7788f93e3d1b3864a645c9515`。
- 最终 review tree SHA256：`99b9b2d5cecdede7f9b9ee964ba6a103df3df3113e7dc6cfb479defeae68539f`。
- shard verifier SHA256：`08b8bcf713462062840125920625592d4f0ccff80519165620993780b9383281`。
- 新 SLO-first 契约在旧冻结 `6c36f89` 的 count2/count4 真子进程探针均真实 RED；修复后
  shard/workflow/macOS/network 受影响域 `225 passed`。旧红日志 SHA256 为
  `dd1bcfc476aae18eb35525854e46387b078992c01602456ede906b299fc195a6`，最终绿日志 SHA256 为
  `56e3553bdc50c35b14e34330a63d149495f7ba60ca10628c204f00ca30f0f434`。
- 最终代码树本机无筛选全量通过：`5926 passed, 15 skipped in 957.30s`；本地全量日志的
  SHA256 为 `7ce17b9c518c2ab71a0ba2f04554954f732bd25fcba53b27fa4a22b6f98934e1`。
- 独立第二读者 Tier 1 动态复审为 `PASS / fixed`：逐文件核验 390/391 个 manifest
  条目零 mismatch；将 `ordered_shard()` 退化为直接 `partition()` 后，count2/count4
  均真实 RED（`2 failed`），逐字节恢复后同节点 GREEN（`2 passed`），定向全套
  `157 passed`，补充契约回归 `118 passed`。复审报告与 summary JSON 的 SHA256 分别为
  `3cf0e6f3b68dd757703c6f0cdfef22306ebd05c025a68538d77657c5de50d1e7` 与
  `640c5e542df8013a0aac72e226cc75532fabb84639783d537415c069cdf3db56`。
- 作者另建全新私有副本重算 manifest 零 mismatch，并回放 SLO-first、严格 verifier 与
  deadline 临界探针：`5 passed in 2.42s`。最终 `uv lock --check`、Ruff、actionlint 与
  frozen-tree diff 检查通过。
- 静态复审未发现 P0/P1；其非阻断 P2 建议后续补充“两分片互换节点但全局覆盖不变”的
  verifier 伪报告负控。当前成员集合断言和有效 SLO-first 变异已覆盖本次修复，不为该
  增强重开已通过全量与复审的代码树。
- 新 head 的 hosted required CI 只按最终 PR 对象的实际 run 判定；定向绿、#225/#227 旧绿
  或重跑后的绿都不能替代。该修复不声称解决所有宿主 OS 负载抖动，也不放宽任何 SLO 或验收阈值。
- 测试使用短 `TIANSHU_TEST_TMP_ROOT` 隔离临时根，不触碰真实 Keychain、生产凭据或全局锁。
