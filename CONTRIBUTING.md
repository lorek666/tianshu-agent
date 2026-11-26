# 贡献与开发流程

> 本项目由一位维护者驱动 Codex 开发。流程围绕自动门禁、可复现证据和独立复审设计，维护者负责最终合并与发布决定。

---

## 1. 什么进仓库，什么不进

| 内容 | 是否提交 | 理由 |
|---|---|---|
| 架构文档（PRD、TECH-ARCH） | ✅ **必须** | 决策记录比代码更难重建。代码丢了可以重写，决策丢了要重新吵一遍 |
| 源码、测试、验收样例 | ✅ | |
| README、LICENSE、CONTRIBUTING | ✅ | |
| 历史研究记录 | ✅ 但放 `docs/archive/` | 有参考价值，但**不能和现行契约平级**，否则新人会读错 |
| 参考仓库（refs/） | ❌ | 762MB，且已固定 commit，见 `docs/REFS.md` |
| 本地数据、日志、快照 | ❌ | 见 `.gitignore` |
| 密钥、token、`.env` | ❌ **永不** | 公开仓库，推上去等于泄露 |
| `playground/` | ❌ | 学习练习，不是产品代码 |

## 2. 分支

```
main              永远可运行。已开分支保护：必须走 PR、required 检查绿才能合、禁止强推与删除（见 §6）
feat/<scope>      新功能，例：feat/m0-tool-loop
fix/<scope>       缺陷修复
docs/<scope>      纯文档
chore/<scope>     构建、依赖、配置
codex/<scope>     Codex 工作分支，改动类型由提交与 PR 标题说明
```

**每张工单一个分支 + 一个独立 Worktree + 一个 PR，自己开自己合**。多会话并行时严禁共用主工作树或使用 `git stash`；PR 合并后及时移除对应 worktree 与本地分支。历史会话流水已拆至 `docs/changelog/` 独立碎片文件，不再集中挤占 `docs/KNOWLEDGE.md`。

共享同一根因或同一发布门禁的多个症状，先在一个集成分支汇总为一个交付批次，不按每条 CI
报错拆成多个追尾 PR。代码 PR 默认先开 **draft**：draft 期间的同步推送只记录进度，不启动
完整 runner 矩阵；完成代码、测试和证据冻结后才标记 ready-for-review，该事件为最终门禁
信号。GitHub 对 `pull_request` 事件的 `GITHUB_SHA` 是 PR 合成 merge commit，不等于 PR
head SHA；证据必须同时记录 `head.sha`、`head tree`、`base.sha` 和 event `GITHUB_SHA`，
不能把三者混写。最终检查后 head 或 base 发生变化，必须重新绑定新对象，不能沿用旧绿。

只有一位维护者时，GitHub required approving reviews 保持 **0**；高危改动仍必须满足 §7 的独立复审要求，不要求为此再建一个批准账号。`CODEOWNERS`、issue assignee 和 GitHub milestones 按需要启用：当前责任人统一为维护者，里程碑与验收依据沿用 `docs/`，下一步与阻塞记录在 `docs/KNOWLEDGE.md`，不重复维护同一份状态。

`main` 已开分支保护：不走 PR 合不了、`strict`（分支必须与 main 同步）、`enforce_admins`（这道门禁对仓库所有者同样生效）、禁止强推与删除。门禁是机械的，不靠自觉，也不再留"文档错字可直接推"的口子。

## 3. 提交信息（Conventional Commits）

```
<type>(<scope>): <subject>

<body — 为什么这么改，不是改了什么>
```

`type`：`feat` `fix` `docs` `refactor` `test` `chore` `perf`

示例：

```
feat(m0): 实现 read_text 工具的路径白名单校验

参数在进入工具前用 canonicalize 解析符号链接，再与 workspace root 比较。
直接比较原始路径会被 `..` 和符号链接绕过（见 PRD §8.1 L1）。
```

**为什么强制写 body**：三个月后你会想知道「当时为什么要这么写」。`git log` 里只有 "fix bug" 的项目，半年后没人敢动。

新写的人工或 Codex 提交遵守上述规范；Git 自动生成的同步 merge commit 可保留默认消息。合并采用 squash 时，最终提交标题仍须符合 Conventional Commits，正文保留改动原因、验证结果和未决事项，不能只留下提交标题列表。历史上缺正文的提交如实视为流程欠账，不为补格式重写已共享历史；必要的原因与证据补在对应文档中。

## 4. 文档改动怎么提交

分两种，规则不同：

| 文档类型 | 提交规则 |
|---|---|
| **契约变更**（PRD §5 的 C1–C5、§6 十一条红线、§8 权限模型） | **必须单独 commit**，标题带 `docs(contract):`，body 写清「原方案 → 新方案 → 为什么改 → 影响哪些代码」 |
| 一般文档修正（错字、补充说明、结构调整） | 可以和代码同一个 commit |

理由：契约是架构决策，混在功能提交里就再也找不到「哪次提交改变了授权模型」。这条是踩过坑才写的——评审时我们推翻了自己抄来的 EventLog 设计，如果那次改动埋在 `feat: 加工具` 里，半年后不会有人知道为什么不用 OpenHands 那套。

**文档必须与代码对账**：README / PRD / 复审报告里任何「已实现什么、多少测试、覆盖哪些平台、数据放在哪」的陈述，都必须与当前代码和 CI 一致。改代码时同步修正对应文档，不留假话与悬空引用；复审报告写错了归因也要按事实重写，而不是糊过去。

判据很简单：**凡是关于运行时行为的句子，都要能指出是哪一行代码或哪一条 CI 结果**。指不出来的，要么补上证据，要么说明它还没实现。这条是踩过坑的——「运行时数据放系统标准位置」在 README 里挂了好几个里程碑，实际一直是当前工作目录下的 `.tianshu/`。

## 5. 提交前检查

```bash
uv lock --check        # 锁文件漂移门禁：漂移时 exit 1
uv run --frozen pytest
uv run --frozen ruff check .
```

以上是最终候选项提交前检查。开发迭代先跑能真实区分坏实现与正确实现的定向红/绿测试；
全量 pytest 只在代码冻结后运行一次。修复失败后代码树发生变化，属于新候选，必须重跑受影响
范围及最终全量；同一代码树不因无关文档整理重复跑全量。CI 使用同一锁文件和测试套件，并
附加日志参数：Ubuntu 使用 `uv run --frozen pytest -o addopts="" -q -rs`；macOS 在两个
独立 `macos-latest` runner 上使用同一分片插件，各执行一个 count2 真实分片；Windows
在四个独立 `windows-latest` runner 上使用原包装器和分片插件，每台只执行一个真实分片，
避免同机并行测试 worker 污染冷启动 SLO。每片执行以下命令（`INDEX` 为 0 至 3，`REPORT`
为 runner 临时目录下的绝对路径）。完整命令以 `.github/workflows/ci.yml` 为准，不声称本地与
CI 命令逐字相同。

本机 macOS 跑 SLO 时，`TIANSHU_TEST_TMP_ROOT` 必须使用短绝对路径，确保
`<root>/data/tsslo-*/warmup_cold/run/daemon.sock` 不超过 103 个 UTF-8 字节；更长会让
Daemon 在绑定 Unix socket 时失败，这是测试调用环境错误，不是产品回归。

```bash
uv run --frozen python scripts/pytest_with_stacks.py -p pytest_ci_shards --ci-shard-index=INDEX --ci-shard-count=4 --ci-shard-report=REPORT -o addopts="" -vv -rs --tb=short --durations=20 --stack-timeout=60
```

每片从同一 checkout SHA 做真实完整 collection，按文件权重确定性分组，同文件不拆；除时序敏感的
SLO/soak 节点固定在其所属分片首位外，组内保持原收集顺序。`full_collection` 始终保留原始顺序，
分组权重与完整覆盖不变；SLO 阈值、重复次数和 soak 验收均不放宽。未校准文件的权重采用节点数；已校准文件使用完整 Windows 日志区间的固定估计，允许低于节点数，并覆盖整份已测 collection，避免稀疏修正留下累计偏差（见 `scripts/pytest_ci_shards.py`）。真实任务测试复用每个场景三次实跑的独立证据副本；对应权重按保留的实跑工作量估算。候选 `c6d6475` 的 [44944526018](https://github.com/lorek666/tianshu-agent/actions/runs/44944526018) 两片及 Windows required 汇总已通过，并核验原始报告身份和完整覆盖；该轮整体仍因 macOS Rust 失败而拒绝合并。权重只用于分组，不保证后续 runner 的耗时，结果以各冻结对应的 Windows job 为准。各片独立检查锁文件、同步冻结依赖并运行完整 lint，显式清空 `PYTEST_ADDOPTS`，分片开关仅经 argv 传入。原包装器是 pytest step 唯一直接命令，上传报告另设 `always()` step；超时/取消可能没有产物，缺件必须失败。

Windows required 检查由 Ubuntu 汇总 job 承担：job 级 `always()` 保证上游失败仍进入核验，且汇总硬要求四个 Windows shard job 整体 `success`。汇总从本 run API 为每个精确 shard 解析最新未过期 artifact，分别下载到 `0/`、`1/`、`2/`、`3/`；每个目录须恰含对应的 `shard.json`，每个报告的 attempt 独立绑定其真实 artifact。失败作业重跑混用不同 attempt 时，成功分片仍保留旧 attempt，汇总不得假设所有报告都属于当前 attempt。核对实际/预期 SHA、完整有序 collection、文件分组、互斥全集、setup/call/teardown 与 finish 覆盖；各阶段按 pytest 原语义接受通过或跳过（含 teardown skip/xfail），任一失败、漏报、重复或错归属均拒绝。报告中的 pytest session exit 为 0 不能覆盖包装器迟发诊断失败、上传失败或取消。四个 runner 及汇总均保持25分钟上限，四个 required 名称不变；CI 开发报告不是产品 C1–C5 契约。

后续完整 run [45058496468](https://github.com/lorek666/tianshu-agent/actions/runs/45058496468)
因 Windows 分片累计超时失败，旧绿不覆盖该候选。新的权重使用该轮完整文件日志区间，
截断的 workflow 文件保留此前完整测量。报告断言可复用真实三次评测材料的独立深拷贝；
缓存只在函数级隔离生效后生成，每片首个消费者仍需实跑，CLI 对比保留一次全新完整执行。
这种局部复用及固定权重只是成本估计，完整原生 CI 仍须在最终冻结上验证。

macOS 和 Windows 每个 Python 分片在 `always()` step 上传 `slo-state-pressure.json`。
测试在首次断言前原子写出完整脱敏 JSON，工件名绑定 run ID、attempt 与 shard；
缺少可选诊断只告警，不能覆盖 pytest 或包装器的退出码。

Windows 包装器用 Python 线程输出超过60秒的用例（含 setup/call/teardown）的线程栈，避免 CPython 3.12 原生定时转储的 frame 遍历竞态；原生 fatal handler 仍启用。栈只含文件/行号/函数，不打印源码或 locals，且有输出上限。诊断线程等待/采样/写出异常或2秒内不能收尾时不得报绿；原 pytest 失败/中断退出码保留。`--stack-timeout=0` 可在本地明确禁用定时诊断；阈值必须有限且介于0和平台`threading.TIMEOUT_MAX`之间，同时设置 `faulthandler_timeout` 或禁用内建 fatal 插件也会报配置错误。**边界**：C 扩展长期独占 GIL 时，Python 线程无法保证在60秒出栈；fatal handler 与仍为25分钟的 job 上限不是这个诊断缺口的等价替代。相关原生风险的 Windows 实验、故障负控和未验证范围见 [DEBUG-IO](docs/DEBUG-IO-2026-11-13.md)。

候选冻结按以下顺序执行，避免“改一处、提交一次、跑整套、再改一处”：

1. 在 ignored `.omc/artifacts/` 累积问题台账、红/绿日志和候选 SHA；过程变化不持续写进
   `docs/KNOWLEDGE.md`。
2. 定向红测证明旧实现失败，最小修复后定向转绿。
3. 记录产品代码、测试和待提交文档的冻结状态，并让第二读者先做设计/实现预审；只有 P0/P1
   或直接阻断当前发布契约的已确认 P2 可重新打开代码修改，其余问题登记后延后。
4. 同步最新 `origin/main`，解决冲突后再做最终全量、静态检查和独立复审绑定。
5. 一次性更新本会话的 `docs/KNOWLEDGE.md` §8 记录和受影响文档。
6. 推送 draft PR，记录并核对 head SHA/tree 与复审对象一致；标记 ready-for-review 后，
   为该次 event 记录 head/base、合成 merge `GITHUB_SHA` 和一次完整 required CI。
7. CI 绿且复审 PASS 后按保护规则合并。若 head 变化、base 前进或最终检查后又产生实质提交，
   重新绑定新对象；不以旧绿、旧复审或部分重跑冒充新候选。

“仅文档变化”可以复用上一候选对产品探针的结论，但复审报告必须用提交/diff 证明产品代码未变，
并审查文档增量；云端 required checks 仍绑定最终提交。路径级“文档 PR 跳过 Python”属于门禁
语义变更，不能用普通 bugfix 顺手放宽，须另行取证和决策。

改动 UI 源码时，还须运行 `pnpm --dir ui install --frozen-lockfile`、`pnpm --dir ui run typecheck`、`pnpm --dir ui run test` 和 `pnpm --dir ui run build`，将对应 `ui/dist` 产物一并提交。CI 会重建并检查产物是否漂移。

改动 `desktop/src-tauri/` 时，还须在该目录运行以下命令；`rust-toolchain.toml`
固定 Rust 工具链并声明 rustfmt/clippy：

```bash
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
```

macOS 的完整 Python 门禁分到两个 `macos-latest` 原生 runner，各执行一个 count2 真实分片；
Rust fmt/test/clippy 另占一个原生 job，三者均保持25分钟上限。Ubuntu 汇总保留原 macOS
required 名，使用 `always()`，按真实 artifact attempt 下载并严格核验两份分片报告的 SHA、
完整有序 collection、确定性分组与执行覆盖，同时要求 Python 和 Rust 两上游均为 `success`
（失败、取消、跳过均拒绝）。所有原生 job 绑定同一 `github.sha`，并与其他 runner 使用相同
draft/ready/main-push 门禁；拆分只隔离累计负载，不减少测试，也不保证 runner 永不在时限内失败。
Windows 原生 Rust 检查仍作为独立 job，由现有 Windows required 汇总 job 绑定其
`success`。不新增 required check 名称。

**锁文件漂移只能靠 `uv lock --check` 查**：锁文件过期时它会 exit 1 并报 `The lockfile at uv.lock needs to be updated`；同样条件下 `uv sync --frozen` 仍然 exit 0——「用了锁文件」不等于「校验过锁文件」，别拿 `sync --frozen` 当漂移门禁。这也是 CI 里先跑 `uv lock --check` 的原因。

## 6. PR 要求

最小要求，不搞形式主义：

1. 标题同提交规范
2. 描述里写：**为什么改 / 做了什么 / 怎么验证的 / 有无未决问题**；验证结果绑定提交 SHA 与 CI 链接，历史绿色不能冒充最新提交已通过
3. 涉及契约变更 → 单独 PR，不夹带代码
4. **CI 绿了才能合**：分支保护机械强制（见 §2），required checks 为下表四项。任一项失败、取消或未完成，都不能合并；**本地绿不算数**。不要用摘除 required check 或忽略失败来解锁当前 PR。
5. 自己合之前，先看一眼 diff，但**自审不等于独立评审**——高危改动按 §7 走。

| 必过检查（GitHub 名称须逐字匹配） | 覆盖范围 |
|---|---|
| `test (ubuntu-latest) · py3.12` | Linux Python 套件、锁文件与 lint |
| `test (macos-latest) · py3.12` | Ubuntu 严格汇总两个原生 macOS Python 分片（含锁文件/lint/SBOM）与 R1 桌面壳 Rust fmt/test/clippy，均须 success |
| `test (windows-latest) · py3.12` | 四个独立 Windows runner 的完整 Python 套件分片、各自锁文件与 lint；Ubuntu 汇总按真实 attempt 核对上游成功、报告身份与完整覆盖；另绑定原生 Windows 的 Rust fmt/test/clippy |
| `ui · typecheck / test / build` | UI 类型、测试、构建与已提交产物一致性 |

required checks 配置在 GitHub 分支保护中，修改 YAML 不会自动更新该配置。改检查名称或矩阵时必须同步核对远端配置。Windows CI 的通过范围以具体 run 的系统版本和架构为准，不代表已验证 Windows 10/11、ARM64 或桌面安装体验。

## 7. 独立复审与证据

> 教训：PR #7 自写、自审、自合，第二个读者十分钟找出 13 项缺陷、5 项高危。自审的盲区不是能力，是位置——作者知道自己的意图，所以看不出实现没兑现意图。见 `docs/REVIEW-PRA.md`。

1. **按风险分级复审（Tier 1 强制独立复审，Tier 2/3 免重仪式）**：碰授权 / 租约 / 并发写 / 事件日志 / 崩溃恢复与备份 / 计费 / 网络与执行隔离边界 / 安装脚本（Tier 1）的改动，自审之外必须由"第二个读者"（另一个独立会话、子代理或人）复审，不读作者自查结论，只按验收项逐条对照。常规功能/UI 面板（Tier 2）与纯文档对账（Tier 3）按 [AGENTS.md](AGENTS.md) §三走自动化门禁与 diff 复核；Tier 1 已审代码树后续仅补充纯文档时，核对非文档代码树哈希未变即可复用代码复审结论，不再陷入重跑本机全量与重绑 SHA 循环。
2. **复审要给出可复现证据**：写探针脚本、真跑、贴关键输出，不是一句"看起来没问题"。"两个进程"必须是真子进程，不是同进程两条连接；时序不靠 `sleep` 猜。复现不出所声称行为的测试不算证据。
3. **测试测的就是它声称的**：契约断言用真实 `tool_manifest()` / 真实 schema，不用手写替身——上一版正是手写形状盖住了真洞。
4. **结论分三层**：已修 / 部分修 / 未修。后两者不得当作已解决，登记为已知限制并跟踪销账。
5. **复审记录绑定版本**：在 PR 正文或仓库内报告中记录完整提交 SHA、检查范围、验收项、复现命令、关键输出及未决项，并注明第二读者的会话或身份。后续实质改动须复审受影响范围；仅回填文档也要说明相对已审提交的差异，不能沿用旧结论声称新实现已审。

## 8. 发布

- 当前仍处内部研发阶段；首发按 [PRD §11.1](docs/PRD-v1.md) 准备 `v0.x.y` **macOS Apple Silicon 限定预览**，仅分发 `macos-arm64`。未验收的 macOS 版本、Intel Mac、Windows 和 Linux 不列为首发支持；四目标正式发行另验。
- **不变的工程门禁**：必须走受保护 PR，四项 required 检查全绿。发行只采用当前 `origin/main` 精确提交，且该 SHA 最新一次 main push CI 全部 job 成功。不能用本机验证、较老 CI 或跳过其他平台的 required job 替代。
- **生成预览草稿**：政策及实现通过受保护 PR 合入后，在 GitHub Actions 的 **Release candidate** 上选择 `main`，填写未占用的 `version`（如 `0.1.0`，不带 `v`）和当前主干完整 `commit`，勾选 `macos_arm64_preview`。共享 preflight 绑定该工作流提交和最新成功的同 SHA main push CI；预览只构建、验证、下载和汇总 `macos-arm64`。要求原生 `runtime_smoke.status=pass` 与匹配的 `host_target`，只用索引内的资产和索引自身生成带 Apple Silicon Preview 标记的 prerelease 草稿，不自动公开。这不是 required check。
- **四目标路径保留**：取消勾选（默认）或推送指向当前主干的 `v0.x.y` annotated tag，仍走四个原生 target。预览版本不能已有同名 Release（含草稿）或 tag；核对版本后再触发，不在两个路径间复用同一个候选。草稿不等于发布，在人工公开前再次核对 tag、源码 SHA、工作流和下载字节。
- macOS 辅助脚本固定为 `scripts/install-macos.sh` / `scripts/uninstall-macos.sh`，不另加下划线别名；发行索引须绑定归档、两份脚本的 SHA256 与大小及版本、完整源码 SHA 和构建 run。保存 workflow 运行、制品下载与校验的证据。

**首发验收清单（待在最终候选上执行）：**

1. 同一台 Apple Silicon 实机即可，不要求新建或切换 OS 测试账户；使用隔离的测试数据和工作区，不得触碰现有生产数据或凭据，不能安全隔离的原生验收项仍记为未验证。按安装说明校验下载的归档，验证首次安装与启动、Keychain 凭据配置和核心任务；记录实测 macOS 版本、实际 Gatekeeper/系统权限提示及未支持能力。CI runner 的结果不替代本项。
2. 验证手动升级、失败回退、卸载保留数据与备份恢复。首发没有旧正式包时，用上一受控测试构建和隔离历史数据验升级/回退。签名、公证和自动更新不作为首发条件；不要求未交付功能的辅助功能/录屏重授权实测。物理断电证据独立留待后续，进程中断与恢复验收仍必做。
3. 在该 Mac 上完成候选 SLO、空闲 / 执行资源采样与趋势、断连 / 重连、取消、重启与零孤儿检查及适用的用户任务验收；记录检查结果、支持范围、安装/升级/回退说明、变更和已知限制。未满足前不公开发行，不宣称跨平台就绪。CHANGELOG 在 `1.0` 之前可手写。

首发 `macos-arm64` 限定预览不再要求 ≥10 小时或任何替代时长的 soak；脚本、测试和既有诊断阈值保留为可选、非阻断工具，长时稳定性必须如实标为未验证。候选 SLO、原生启动/runtime smoke、核心任务与 Keychain、数据完整性、进程崩溃恢复、升级/回退/备份及上述 required CI 和受保护 PR 均不豁免；可选诊断发现这些必过项失败时仍阻断。四目标正式发行的长稳门禁继续按 [PRD §10.6](docs/PRD-v1.md) 执行。

本次不改变 T1；未来若改变签名策略，仍须先通过单独的契约 PR。

## 9. 远端

`origin` → https://github.com/lorek666/tianshu-agent

**公开仓库 = 每个字都会被看到。** 推之前问自己一句：这段内容三个月后还愿意让它挂在互联网上吗？
