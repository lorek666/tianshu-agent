# 2026-11-26 · 修复失败作业重跑时的分片工件 attempt 绑定（#228）

## 背景与根因

run `45671429051` attempt 1 的 macOS Rust job 被取消；按 `--failed` 重跑后，Rust 在 attempt 2 成功，但 macOS Python 分片没有重跑，仍只有 attempt 1 的工件。新启动的 macOS 汇总 job 用当前 `GITHUB_RUN_ATTEMPT=2` 拼下载名，因此找不到 `macos-pytest-45671429051-2-0/1`，随后严格 verifier 因报告缺失正确失败。

这是汇总作业的工件绑定缺陷，不是分片测试失败。Windows 汇总使用同样的当前 attempt 假设，只是该轮没有触发汇总重跑，风险仍然存在。attempt 1 的取消与 attempt 2 的失败均保留为历史证据，不用后续绿色覆盖。

## 实现与边界

- 新增 `scripts/ci_artifacts.py`，通过 GitHub Actions API 分页读取本 run 的工件，按平台前缀、run ID 和精确 shard 索引选择最新未过期 attempt；缺件或同 attempt 重名直接失败。
- macOS 两个 artifact 各对应一个 report；Windows 两个 artifact 各对应两个 worker report。解析结果同时生成下载名与显式 per-shard attempt map，verifier 逐 report 校验 map。
- `scripts/pytest_ci_shards.py` 保留单 attempt 参数用于既有调用，并新增互斥的 `--run-attempts` JSON map；map 必须精确覆盖请求索引，值必须为十进制字符串。
- 汇总仍为 `always()`，仍硬要求上游 job `success`；SHA、run ID、shard index/count、pytest exit、完整 collection、selected/finished、阶段报告、精确文件树与 runner 归属校验均未放宽。
- Windows 仍为两台 runner × 每台两个真实 worker / count4，报告布局仍为 `0/0、0/1、1/2、1/3`；macOS 仍为 count2。25 分钟上限、60 秒线程栈、原生 fatal handler、wrapper 退出码、SLO 阈值和四个 required 名称均不变。

## 验证

- 新测试先行红：工件解析脚本、workflow 实际 attempt 下载与 verifier map 均缺失时，定向套件真实失败。
- 真实 run `45671429051` 的 API 解析结果为两个平台各选择 attempt 1；Windows map 为 `{"0":"1","1":"1","2":"1","3":"1"}`，证明不依赖汇总 job 的当前 attempt。新 verifier 也用显式 map 重新接受了该轮真实 macOS count2 与 Windows count4 原始报告；日志 SHA256 `3e83d31216e591ed558f2c19761434c0343abed089348cb341da2da6856c9f7b`。
- 覆盖混合 attempt 正控，以及缺索引、多索引、非字符串、空 map、同时传两种 attempt 参数和当前 attempt 下载名回退等负控。
- 定向命令：`uv run --frozen pytest -o addopts="" -q -rs tests/test_ci_artifacts.py tests/test_ci_workflow.py tests/test_pytest_ci_shards.py tests/test_macos_ci_shards.py tests/test_pytest_ci_pairs.py tests/test_pytest_with_stacks.py tests/test_slo_state_pressure.py`；日志 SHA256 `4a15a997f643cbd51654166e88acf4f23ae5d188546a1d0bae061cfa960ce9d1`。
- 静态命令：`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check`、`actionlint .github/workflows/ci.yml`；初次完整静态日志 SHA256 `c171a703d60a4559fc9af11ac0811ae977be7ccf69d330210014c4bb6e22e8e5`。最终静态门禁另含变更 Markdown 本地链接检查，退出 0，日志 SHA256 `aacd6eda193e096e318121c82b8c2fe7dc3c921645271c6ffc4ca1bfa2b2d79f`。
- attempt 2 macOS Rust 成功日志 SHA256：`dad73f2bddb7b358bc9847a70f09be3264731f5259b201a367ef86dfcc68340b`；汇总失败日志 SHA256：`0fd34862d5b1aa7b94afbdd9ce8b0b8876bdce3cca9f78d26ca9aaeeab429f42`。
- 主控在第二份私有快照复现：scoped green 日志 SHA256 `09ffdb43d9cc7d523bb616a463bf291e3a07a4c6bee7221b32d7286a48da18e8`；把 macOS 下载名改回当前 attempt 后，两个正向 workflow 断言按预期失败（`25a6cf917c4cced4176be1acfacae93d8f69522547dd546bb8e1e24b97e08710`）；让 verifier 忽略 per-shard map 后，混合 attempt 正控在身份校验处失败（`61885a609ac2ada5c101c5a9878de04e18a4ff6f758f7e7807f74ff3bc2bdfbf`）。恢复后 scoped green 日志 SHA256 `32fa3738d117a06b9ab974c8cc14848d5c3aead845004629035b01c168a04a1d`，normative/review 树分别仍为 `a4720febd19dda5878890fe86004f4f1ff8a8ae3e0a0953984eec7904092474e` / `8d71021e9c6530e29075ddc2942386040719012ab52ef30dedc9294d67662ee9`。
- 同一冻结代码树的无筛选本机全量 `uv run --frozen pytest -o addopts="" -q -rs` 退出 0；`TMPDIR`、`TIANSHU_TEST_TMP_ROOT`、锁根、注册表与 authority 根隔离到 `/Volumes/Date/tianshu-pytest-rerun.09tWPi`，日志保留全部跳过原因，SHA256 `eee0d707c70c64f4677374b4664d28690d0b97894394c6c0bf0a00032c317112`。本机 macOS 结果不替代 Windows 原生 CI。

- 独立第二读者在第三份私有快照返回 `fixed`，绑定 review tree `8d71021e9c6530e29075ddc2942386040719012ab52ef30dedc9294d67662ee9`；定向基线/恢复均退出 0，恢复后无筛选全量也退出 0。它分别把 macOS 下载名、Windows 每工件 report 数和 verifier 的 per-shard attempt 读取改成旧行为，三种 mutation 均在预期断言处非零，恢复后均回到绿色；恢复树与作者代码树逐文件一致。独立报告 SHA256 `ef8a56ffdf98f350131e2ce07056d8b1cd7191a56dddb42811807668690fc3cc`，探针清单 SHA256 `8a57b7d1f4c7b756c339a616362af027a74046902b11cfc2de469d4f1336345c`，独立全量日志 SHA256 `cbcf60d08df6ca887168683d1d07ca8df37e5ab01239ff600989f03cd99076ba`。两条非阻断低建议（把分页探针固化进测试、评估显式 `actions: read`）不改变本轮修复结论；当前公开仓库无认证工件 API 实测可读。

## 最终代码 head 的 hosted CI

代码 head `26743f27f0cbfbdc9e4d60f831b312db917883cc`（base `a755e2abf7d75b6baf36a1e47f47646142f0bc8d`）触发 [run `45684106116` attempt 1](https://github.com/lorek666/tianshu-agent/actions/runs/45684106116)，结论为 `success`。GitHub PR 合成 merge `0402194cec40beaedb6fb60da199b5fffcea96c9` 的 parents 精确对应该 base 与 head，merge tree 和 head tree 均为 `391ef281d13bded7f7657fbe6d15d1783ae68ca4`。全部 job 与 PR check 成功，四个 required 名称均保持原样：`test (ubuntu-latest) · py3.12`、`test (macos-latest) · py3.12`、`test (windows-latest) · py3.12`、`ui · typecheck / test / build`。

- 汇总日志显示新 resolver 实际通过 API 选中 attempt 1：macOS 下载 `macos-pytest-45684106116-1-0/1`，Windows 下载 `windows-pytest-45684106116-1-0/1`；Windows 显式 map 为 `{"0":"1","1":"1","2":"1","3":"1"}`。严格 verifier 以合成 merge SHA 重放两平台原始报告，分别通过 macOS count2 与 Windows count4 的完整 collection、身份与文件树校验，日志 SHA256 `5aeec017ebb672a8721431ed8cfe395f44b19026d5cd058ff838406027b85351`。
- 九个 artifact ZIP 的本地 SHA256 均与 GitHub metadata 一致；核验日志 SHA256 `af0d8bfbb54cab9f6c2b1b129018c0319c2c86fd1dface5f8f62d8fc3202e0de`。四个 pytest 工件 digest 为：macOS `8dd7c3fbf51c442a3b2bbb68227d0842ee3ca68fb0364e7dae9433c8a1e8f35e` / `906869c8e09cbc4b4f4a7981265beb1ea1c21aca07f76b156b4bebb1d958a6a8`，Windows `ba039c62c1ad6210626eded4ebd8e5d2b27840117fea85f6bcf8125a722564ff` / `48d27fdf00922f6a03c03851b8d8c871d289562b992b47bff314ec13554b046c`。
- 从 raw samples 按 nearest-rank 重算，macOS/Windows SLO 均 `PASS`，冻结阈值 canonical SHA256 `0602a7b1ec84265ef502d72e3385a6cbd3f370e29838a5b1c15a151b2b1b8432` 未变；Windows cold-start p95 `2008.184ms < 2200ms`。两平台 soak 均 `PASS`，orphan PID 均为空。该轮为 CI smoke，不冒充完整十小时 soak 或 min-repeats 基线。
- #229 state-pressure 证据已随配对 shard 保留：macOS/Windows 各有 3 个 qualified、0 invalid，p95 分别为 `147.480583ms` / `37.554ms`，cursor reconnect 均验证通过、orphan 均为 0；证据仍标为 `diagnostic_pre_threshold`，不伪造阈值或 verdict。Windows restore T1 为 `PASS`，`directory_power_loss_durability=NOT_PROVEN` 边界保留；诊断文件未发现本机用户路径泄漏。
- 汇总核验记录 SHA256 `12dc01b790d3a595d0ed276f7a8768c9a1179dd01b3649fdbfd7d13ed90bf040`，CI 元数据记录 SHA256 `2f1e5607cb2d3df78d03aae0de795fde39f85360022e5809e417d7cca87d3a40`；macOS/Windows 汇总日志 SHA256 分别为 `68fb08dda953056da1d8d42f6e5287320706d4ee55c00887eea10fc8010491b3` / `be7243fc059f6f244692a13c5b56f44a77ac62b870ed4546210582ccffacec74`。

上述绿色只绑定代码 head `26743f2`。本次证据回填产生纯文档 head 后，仍需该 head 自己的 hosted required CI；不得把这里的代码 head 结果提前算到文档 head 上。
