# PR #225 CI SLO 顺序隔离与 macOS count2（2026-11-26）

## 背景与真实失败

- head `b1444a944e87b915c338804bbee1fcbd6ad0c0d9` 的 hosted run
  [`45720477289`](https://github.com/lorek666/tianshu-agent/actions/runs/45720477289)
  已让四个 Windows runner 完整执行，但 shard3 的
  `restart_recovery_ms p95=2259.816ms > 2000.0ms`；strict required 汇总正确拒绝。
- 同轮单 macOS Python runner 在99%触及25分钟 job 上限。旧失败保留，不用重跑覆盖。

## 决策与边界

- 保持 Windows count4、macOS count2、文件权重、25分钟上限、SLO 阈值、repeats、
  soak 验收、完整 collection 与 required 名称不变。
- `full_collection` 保留 pytest 原始收集顺序；`partition()` 不变。
- `ordered_shard()` 只把 `test_measure_slo_smoke_and_run_soak_real_subprocesses`
  移到所属分片首位。pytest `items` 和 verifier 使用同一顺序，避免执行顺序与证据顺序分叉。
- macOS Python 从单 runner 改为两个独立 count2 runner；Rust 仍独立。汇总按真实
  artifact attempt 分别下载 `0/shard.json`、`1/shard.json`，严格核验身份、全集与上游结果。
- 该修复只隔离同分片前序测试的累计资源压力，不保证消除 OS 或 hosted runner 负载抖动。

## 本地验证

- SLO-first 真子进程探针：修改前 count2/count4 均因节点不在首位而红；实现后转绿。
- `tests/test_pytest_ci_shards.py`：通过，包含交换 selected/finished 后 verifier 必须拒绝的负控。
- `tests/test_ci_workflow.py`：通过，覆盖 macOS count2、artifact attempt、权限和弱化负控。
- `uv lock --check`、`uv run --frozen ruff check .`、`actionlint .github/workflows/ci.yml`、
  `git diff --check` 均通过；静态日志
  `.omc/artifacts/pr225-slo/static-final4.log` 的 SHA256 为
  `dd3dd0477b7c27e2423312eaad7955fe6b899edf00171bddb9a7c80589638234`。
- 最终无插件、无筛选全量在短隔离根 `p225g` 运行，exit 0；实时测试统计与跳过原因以日志
  `/Volumes/Date/tianshu-test-tmp/pr225-final-full-2.log` 为准，其 SHA256 为
  `b7518f7c66c1097fb8dff2f91f8159ddb4fec3f0b67566d457312b974fb976ec`。
- 测试使用 `TIANSHU_TEST_TMP_ROOT` 隔离临时根，不触碰真实 Keychain、生产凭据或全局锁。
- 本机全量排查记录：隔离根不存在、系统卷低于 2 GiB 的两次运行均无效并保留；随后两次
  Date 卷隔离运行只在 SLO warmup 报 Daemon `exit=2`。根因是调用根过长，
  `warmup_cold/run/daemon.sock` 达到 104/105 字节，超过 macOS `sockaddr_un` 的 103
  字节可绑定上限；短根单节点为 95 字节并通过。该失败不是测试顺序污染，也不改产品阈值。
- 独立第二读者 addendum 判定**已修（PASS）**，P0/P1/P2 均无，原跨 runner 负控 P2 已关闭。
  冻结 code tree 为 `e7980198742c628c1b914f76efeef8ee89444a7014afc7e2c9d14ff30f328888`，
  review tree 为 `526a730c3fc522fe051956f2ad7b0ebd6cffd6433c459b235e7faeeaab3fc05e`。
  私有副本 baseline 通过；最小变异让同一负控在断言处真实失败；恢复原字节后父树回放通过。
  复审报告 `review-addendum.md` 的 SHA256 为
  `6c487301b40871fe15efbc1735b512ac00a8aeb6fa5481dcd9da0eb1e8d06f9a`。

## 待完成证据

最终代码树的本机全量 pytest、静态门禁与独立第二读者 Tier 1 addendum 均已完成并通过。
新 head 的 hosted required CI 仍须单独证明。本文不把定向绿、旧绿或本机绿写成 hosted
候选通过。
