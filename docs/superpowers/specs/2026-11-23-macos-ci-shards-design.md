# macOS Python CI 完整分片修复

## 原因与范围

PR #228 的完整 run `45160677871` 中，macOS Python 在实际执行测试后触及
25 分钟 job 上限而取消，macOS required 汇总失败。GitHub annotation 明确确认
超时；runner 排队 notice 不能解释执行超时。Windows 收集顺序修复与此独立。

本修复只调整开发 CI 的执行容量及验证，不更改产品、权限、沙箱或测试语义。
不删测试、不提高时限、不合并、不发布；#224 保持开放。

## 方案选择

- 单 runner 继续调整文件权重不会减少完整套件的串行总工作量。
- 单 runner 内增加两个 worker 需要扩展现有 Windows supervisor，并重新证明
  直接 pytest 路径及进程清理；本次不采用。
- **采用两个独立 macOS runner**：复用已存在的 count2 整文件分片插件和校验器，
  各自直接运行原生 pytest，保留 `-o addopts="" -q -rs`。这是执行侧的工程选择，
  不代表用户曾逐项指定该实现。

两个 runner 使用同一 `github.sha`，各做完整真实 collection，再选择互斥整文件
子集。固定调度权重沿用现有估计，不宣称其适合所有 macOS runner 或保证时限。
不新增 watchdog 线程，不改变 Windows count4、诊断和原 required 名称。

## 报告与门禁

`macos_python` 使用矩阵索引 `0/1`，`fail-fast: false`，每 job 仍限25分钟；
basetemp、cache_dir 和报告均在 runner 私有临时目录。每片原始报告以
`macos-pytest-<run>-<attempt>-<index>` 上传，缺文件为错误。

macOS required 汇总 checkout 同一 SHA，分别下载两个具名 artifact 到
`0/`、`1/`，调用原严格 verifier，要求 count2 的索引、SHA、run/attempt、
完整有序 collection、互斥覆盖、各阶段及完成节点一致。任一上游非 success、
漏件、额外文件、错身份、未执行或失败报告均拒绝；同时严格要求原生 Rust success。

CI 报告是开发证据，不新增 C1–C5 产品契约。旧失败原件保留，完整 hosted
验收必须绑定新 head/base/合成 merge；本机测试不能代替它。

## 验证

先用真实 workflow 守卫复现旧配置未分片的 RED，再验证新矩阵和严格汇总；
从 workflow 提取真实 pytest 命令及 verifier 命令，在独立临时 Git checkout
运行两个 pytest 子进程并校验原始报告。变异丢失报告、错身份、失败和取消时
必须非零。保留原 Windows 回归，最终冻结后跑本机裸全量、lock/Ruff/diff，
绑定独立增量复审与新完整 hosted CI。
