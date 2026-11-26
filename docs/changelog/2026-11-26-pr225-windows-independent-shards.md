# PR #225：Windows 改用四个独立 count4 分片 runner

## 背景与失败证据

刷新后的 head `54bf0ed66b23901d814c0f862cd3957a1cd5b3c2` 在 hosted run
[45697427085](https://github.com/lorek666/tianshu-agent/actions/runs/45697427085)
中先出现 Windows RPC 超时；failed-only attempt 2 的两个 count2 分片又都运行约
25 分钟，在约 97% 处被取消，未形成完整报告。旧 count2 拓扑已构成结构性超时，
不能靠重跑或放宽 25 分钟上限掩盖。

下游 #228 曾尝试两个 runner 各并行两个 count4 worker，但 hosted run
[45705494683](https://github.com/lorek666/tianshu-agent/actions/runs/45705494683)
的两个 attempt 都证明同机并行 worker 会污染冷启动 SLO。因此本 PR 不移植该中间
拓扑，也不串行退回每台半套测试，而直接采用四个独立 runner、每台一个 count4
整文件分片。

## 决策与实现

- Windows matrix 改为 `[0, 1, 2, 3]`，每个 runner 直接执行一个真实
  `pytest_ci_shards` 分片，并只上传对应的 `shard.json`。
- summary 为四个精确 shard 分别解析最新未过期 artifact，下载到独立的
  `0/`、`1/`、`2/`、`3/` 目录，并把每个报告绑定到其真实 artifact attempt。
- verifier 继续严格核对完整有序 collection、partition、selected/finished、
  setup/call/teardown、SHA/run/shard/attempt 和上游整体成功；缺失、额外、错归属、
  错 attempt、漏阶段或上游非成功均拒绝。
- `windows_summary` 仅增加 `actions: read`，不扩大 workflow-global 权限。
- 保留 25 分钟上限、文件权重、SLO 阈值、60 秒 Python 线程栈诊断、strict
  verifier 与四个 required 名称。本分支权重表逐项核对，与刷新前 HEAD 完全一致。
- 不夹带 #228 的 macOS count2 或 Windows SLO 上传功能；`CONTRIBUTING.md` 与
  `docs/KNOWLEDGE.md` 同步当前拓扑和证据归属。

## 验证

- 定向 workflow/artifact/verifier/wrapper 套件 exit 0；静态门禁
  `uv lock --check`、`uv run --frozen ruff check .`、`actionlint` 与
  `git diff --check` 均 exit 0。日志 `/tmp/pr225-four-runner-static.log` 的
  SHA256 为 `d9d4ffa5ac75f0b0276277e81ef3ae1d3c04dc100983a65cd89b1113029864ca`。
- 作者私有副本对三类有效变异执行真实 RED/GREEN：退回两 runner、verifier 回到
  pair 路径、删除 `actions: read`。每次均先 RED，按冻结字节恢复后 GREEN，最终树
  回到同一哈希。日志 `/tmp/pr225-four-runner-mutations.log` 的 SHA256 为
  `4ea72c825c44a4d826c1aadb30db2ab2353939b30494758559226eb475b16bd2`。
- 同一冻结代码树在 `/Volumes/Date/tianshu-pr225-four-runner.4ZhrPy` 隔离测试根
  运行无筛选 `uv run --frozen pytest -o addopts="" -q -rs`，exit 0，保留全部跳过
  原因。日志 `/tmp/pr225-four-runner-full-pytest.log` 的 SHA256 为
  `91cdbd3e0c91fdae9ff0dc86c8dee7c378faf627cd75c97f14d535e041f638d8`。
- 规范树（`src/`、`tests/`、`scripts/`）SHA256 为
  `6656bc74be9490e740a65d74034bf53adae0348b0132fb71260a33bdb01ed130`；含
  `.github/workflows/ci.yml` 的复审树 SHA256 为
  `f7ddb167e91628cebdfeadbeae6a51196c4f8e5391e1ff73f85e39c8bdc68b6c`。
  口径为按仓库相对 UTF-8 路径全局排序，逐文件写入
  `UTF8(path) + NUL + hex(SHA256(file bytes)) + NUL`。
- 独立第二读者结论 PASS，P0/P1/P2 均无；另以旧 attempt、额外文件、混合 attempt
  折叠三类变异完成 RED/GREEN，并复核作者树未修改。报告 SHA256 为
  `52111a5d609c4953e09f6266a2276493018296fba42f1e67148fbd8a7dbc2f4d`，机器证据
  SHA256 为 `ddd35dd2221c5a170c3d66d467dfa09ddbc7f332b1176aed0d702333cdef5351`。

## 边界

本地全量和独立复审不等于 live hosted required CI 通过。新 head 的四个 Windows
runner、strict summary 与其它 required 作业仍须在 GitHub Actions 上取得完整绿色；
旧 head 的失败或历史绿色均不继承为新候选结论。
