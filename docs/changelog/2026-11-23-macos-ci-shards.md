# macOS Python CI 完整分片候选

状态：PR #228 的同批 CI 修复，未合入、未发布；#224 保持开放。

## 原因

v7 提交 `416fd45` 的完整 run
[45160677871](https://github.com/lorek666/tianshu-agent/actions/runs/45160677871)
整体失败。Windows collection 失败之外，macOS Python 的测试实际执行至
25分钟 job 上限后取消，macOS required 失败。GitHub annotation 直接确认
时限；安装只占很短时间，不能用另一条 runner 排队 notice 解释本次超时。
Ubuntu、UI、两项 Rust 的成功不能覆盖失败。原件保留在
`.omc/artifacts/native-process-isolation/`，不重跑旧 run 洗绿。

## 变更

macOS Python 使用两个独立原生 runner，直接运行原 pytest，保留
`-o addopts="" -q -rs`、原生 fatal handler、完整 collection 和25分钟上限。
复用原 count2 整文件分片，不新增 supervisor 或 watchdog；各片私有 basetemp、
cache_dir 和报告。上传只选择原始 `shard.json`，运行数据不进入下载树。

macOS required 的 Ubuntu 汇总分别下载本 run 的两个具名 artifact（初版按当前 attempt
命名；2026-11-26 起改为解析每个工件的真实 attempt，见
[重跑修复记录](2026-11-26-pr228-rerun-artifact-attempts.md)），
严格验证实际/预期 SHA、索引、有序 collection、互斥覆盖、各阶段与完成节点，
同时要求 Python 矩阵和原生 Rust job 成功。缺件、额外文件、错身份、
未执行报告或上游失败/取消均不能报绿。四个 required 名称、Windows count4、
原诊断和产品源码不变；无依赖或 C1–C5 产品契约变化。

## 验证边界

新测试从真实 workflow 提取 pytest/verifier 命令，在独立 Git checkout
运行真子进程；只替换解释器入口，不重写被测参数。私有运行根、原生 fatal、
完整原始报告与失败拒绝由真实执行核对。旧配置未分片的 RED 与后续日志保留。
最终冻结的本机裸全量、限定独立复审与新完整 hosted CI 须分别绑定；
在验收前不能宣称此调度已解决托管 runner 的墙钟。

本机 v9 冻结的裸 `uv run --frozen pytest`、lock、Ruff 及工作树/索引 diff
均退出0；验证环境为 macOS26.6.2 arm64 / Python3.12.13。
非文档清单 SHA256：
`1e6aefab77348d1abef038418ca9048e2edbfed566c7793478e3916667f11240`；
全量原始日志 SHA256：
`079319f9dd9b3945b80c0e05a87c703e16333f8767e72b2c72c13f2cbe6778df`。

独立第二读者 `seatbelt_leaf_frozen_review` 对此 macOS CI 增量判定「已修」，
无范围内阻断。真实完整报告通过；上游失败、取消、跳过以及仅清空完成节点的
副本均被拒绝，原件不变。Windows 工作流逐字节不变，脚本只改三处文案。
报告 SHA256：
`0f7d9495e01bd17f9e077c67ba6f4ecca65c911d0438567b8f34f1fe4d23a6b5`。
主控已逐项核验来源哈希并重跑报告负控；原产品及历史复审保留原范围。
新完整 hosted CI 的最终事实由 PR #228 的精确绑定及原始报告另行证明。

设计见[macOS CI 分片](../superpowers/specs/2026-11-23-macos-ci-shards-design.md)。
Windows 原生产品沙箱、任意后代监管和远程 MCP 隔离仍未交付。
