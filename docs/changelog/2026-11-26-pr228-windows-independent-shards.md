# PR #228：Windows SLO 改用四个独立分片 runner

## 背景与真实失败

刷新后的 head `281771fdf2389fecb3ceaacbc26ac78c4b7c204a` 在 hosted run
[45705494683](https://github.com/lorek666/tianshu-agent/actions/runs/45705494683)
的两个 attempt 都失败于同一节点
`tests/test_slo_and_soak.py::test_measure_slo_smoke_and_run_soak_real_subprocesses`。
当时拓扑为两个 Windows runner、每台并行两个 pytest worker。

- attempt 1 的 `startup_cold_ms` 样本为 `2951.547 / 2204.879 / 2039.862ms`，
  nearest-rank p95 为 `2951.547ms`，超过冻结的 `2200.0ms`；daemon 内部冷启动
  p50 为 `437.622ms`。
- failed-only attempt 2 的样本为 `3452.569 / 2801.592 / 7228.173ms`，p95/max 为
  `7228.173ms`，同时超过 p95 `2200.0ms` 与 max `4500.0ms`；daemon 内部冷启动
  p50 升到 `1459.525ms`。
- 两次均无 timeout、无 orphan PID，soak 返回 0；同机另一份 shard 报告全绿。
  因此不能把红色归为一次外部启动抖动，也不能用重跑覆盖旧失败。

完整失败证据 JSON 的 SHA256 为
`1f6b7a40f40f23d19a73ffe0cf1aecc9a4f69f807420b9329bfb75be8f0c7577`；两份 shard
job 日志与 attempt 2 summary 日志的 SHA256 分别为
`779990b350cdc8cbf26b9236fce3d66e623cc62444c7df37e7177e089f37418e`、
`690c95e0bebf03369c593f093174791caec724d51bab4a4385a749af6fa28aa4`、
`8c6933e7e7985d16c8d880d3aaf87fcaaaa40e528d456fb546715ee1fa5e02e5`。

## 决策

Windows 改为四个独立 `windows-latest` runner，每个 runner 只执行一个 count4
整文件分片。这样消除同机另一个测试 worker 对冷启动 SLO 的 CPU/IO 争用，同时不把
每台退化成串行执行半套测试——后者会重新接近旧 count2 的 25 分钟结构性超时。

不改变文件权重、SLO 阈值、25 分钟 job 上限、60 秒 Python 线程栈诊断、strict
verifier 语义或四个 required 名称。四个分片仍从同一 checkout 做完整 collection，
报告仍须证明互斥全集、完整阶段和真实身份。

## 实现

- workflow 的 Windows matrix 从 `[0, 1]` 改为 `[0, 1, 2, 3]`，直接调用原
  `pytest_with_stacks.py` 与真实 `pytest_ci_shards` 插件；每个 runner 只上传一个
  `shard.json`。
- summary 分别解析并下载四个 artifact，为每个报告生成独立的 attempt map；
  `windows_summary` 仅在该 job 增加 `actions: read`，不扩大 workflow-global 权限。
- verifier 的 count2/count4 报告路径统一为 `<download>/<index>/shard.json`，
  继续拒绝缺失、额外、错 attempt、错 SHA/run/shard、漏阶段和上游非成功。
- 删除 pair helper、wrapper 的 pair dispatch 与 pair 专用测试；不再保留
  `--ci-pair-*` 或 `--reports-per-artifact` 兼容入口。
- `CONTRIBUTING.md` 与 `docs/KNOWLEDGE.md` 同步当前拓扑、失败教训和边界。

## Hosted 验证与诊断索引修复

四 runner 候选 `d360dd1a51f2520ce6eb51102777609f85abc079` 的 hosted run
[45717065320](https://github.com/lorek666/tianshu-agent/actions/runs/45717065320)
使用合成 merge SHA `ed3ac08a8340c8e70ae3c0ace5e669486a1f69ad`，base 为
`28dc2b9567131af5015e71ecd6248b055da0c076`。四份报告来自同一完整 collection，
selected/finished 均完整；shard 0/2/3 成功，shard 3 的冷启动 p95 为
`1838.809ms`、daemon 内部冷启动 p50 为 `399.091ms`，SLO 与 soak 均 PASS，未出现
旧双 worker 拓扑的阈值失败或 orphan。

但 shard 1 的真实 state-pressure 子进程在写诊断 JSON 时失败：
`ValueError: state-pressure diagnostics require an explicit CI shard index`。
根因是 Windows Pytest step 已启用 `TIANSHU_SLO_DIAGNOSTICS_DIR`，却漏传
`CI_SHARD_INDEX`。汇总按上游 failure 正确拒绝，不能把该 run 写成通过。修复为在
Windows step 显式传入 `CI_SHARD_INDEX: ${{ matrix.shard }}`，并增加 workflow
契约断言与固定为 `0` 的负控；不改变诊断阈值、报告路径或上传策略。

## 验证

- 新契约先在旧实现上得到真实 RED：workflow 只有两个分片，verifier 拒绝四独立目录。
- 对 hosted 根因做直接复现：启用 diagnostics 目录但不设 shard index 时，真实
  state-pressure 测试 exit 1；设置 `CI_SHARD_INDEX=1` 后 exit 0。新增 workflow
  契约在漏传 env 时真实 RED，修复后 GREEN；`actionlint` 与 `git diff --check`
  exit 0。原始日志 `/tmp/pr228-state-pressure-red.log` 与
  `/tmp/pr228-shard-index-green.log` 保留在本地证据区。
- 独立第二读者对索引增量结论 PASS，P0/P1/P2 均无；在私有副本复现删除 env 的
  workflow RED、恢复 GREEN，以及缺 shard index 的真实诊断 RED、设置 index 后
  GREEN。证据清单 `/Volumes/Date/pr228-second-reader-d360dd1-evidence/SHA256SUMS`
  的 SHA256 为 `446c14800b50c3103017338c10ba89335d50725582bdf5e1c91327997e058fa0`。
- 定向 workflow/artifact/verifier/wrapper 套件 exit 0；workflow 负控覆盖退回
  pair、减少 runner/分片、丢失 `actions: read`、放宽 timeout/stack 和改动上传路径。
- `uv lock --check`、`uv run --frozen ruff check .`、staged/unstaged
  `git diff --check` 与 `actionlint .github/workflows/ci.yml` 均 exit 0；静态日志
  `/tmp/pr228-four-runner-static.log` 的 SHA256 为
  `16782f9d4d66930967c32618f3f639a9029881e1096333ed4b017e19d48a35ba`。
  索引修复后最终静态门禁重新 exit 0，日志
  `/tmp/pr228-shard-index-static-final.log` 的 SHA256 为
  `0f7184230f3fef0d31e2dd6780ae4659ab900133d0896aa027f1e5ca45388906`。
- 上一四-runner 候选 `d360dd1` 的同一冻结代码树在
  `/Volumes/Date/tianshu-pr228-four-runner.Vx8I4V` 隔离测试根运行
  无筛选 `uv run --frozen pytest -o addopts="" -q -rs`，exit 0；`-rs` 保留全部跳过
  原因。该全量属于索引修复前对象，不替代修复后候选。日志
  `/tmp/pr228-four-runner-full-pytest.log` 的 SHA256 为
  `a3cdb930493f47283c028f95a3d4ac1ef4a9251d15ecff8b0a50b11d84116809`。
- 上一候选的规范树（`src/`、`tests/`、`scripts/`）SHA256 为
  `422651cbd277cc87156079fe29067ad60e94747507a9fb5901fcc50c35285056`；含
  `.github/workflows/ci.yml` 的最终复审树 SHA256 为
  `377b25aec12eb60935c2b3818da95c212b1142f065eb9e7101fed8c93b10663c`。
  索引修复后的规范树 SHA256 为
  `cd8bfacfe1e4fe60cd7a2c91362a2bebef66a0171e0a67737c0f28eea258f322`，复审树为
  `7f6bfe24f02c4f1515b9511c2d555444f11b2e90502e1eda372f32170a1873e9`。
  哈希口径为按仓库相对 UTF-8 路径排序，逐文件写入
  `UTF8(path) + NUL + hex(SHA256(file bytes)) + NUL`。
- 索引修复后的同一冻结代码树在
  `/Volumes/Date/tianshu-pr228-shard-index.am8Qlu` 隔离测试根运行无筛选
  `uv run --frozen pytest -o addopts="" -q -rs`，exit 0，保留全部跳过原因。日志
  `/tmp/pr228-shard-index-full-pytest.log` 的 SHA256 为
  `03c921a0e9818d1140ac01c32c0e08d47ae052b4d812de3b2e83c5910e39f800`。
- 独立第二读者结论为 `fixed`，P0/P1/P2 均无；三类有效变异均先真实 RED、按冻结字节
  恢复后 GREEN，私有副本最终树逐字节恢复。原报告、机器证据与仅注释增量重绑的 SHA256
  分别为 `1e40b61ee05da46a3ad32dde397b4e2212c93b8d62c2d90d2a8a605040b318ed`、
  `941db22bb0fe26de7a4d5a52eb0c95e9c11b0444310f6d1bce7b15380a518bb1`、
  `99c38e1d1efd00459ed6cb520adc872e0bc755ed6f5075e91f97b9e3b7079204`。
- 两处过期注释修正后，最终静态门禁重新 exit 0；归档日志
  `/tmp/pr228-four-runner-static-final.log` 的 SHA256 为
  `f21f1057b1be818869adb2afa367d98bafdb3ca5c44bbadb430aa70d3f313faf`。
- 本地复审不等于 live hosted required CI 通过；四个 Windows runner 的新 head 实际
  GitHub required CI 仍须单独取得。旧 run 的红色不会被重写为当前候选的通过。

## 边界

本次只修 CI 测量隔离和证据归属，不改变产品进程隔离能力。Windows 强制清理仍只证明
自有 Daemon 根退出；完整进程树监管、原生 Windows 沙箱、任意后代监管和远程 MCP
隔离仍由 #224 跟踪。macOS 本机结果与模拟 Windows 回归不替代原生 Windows CI。
