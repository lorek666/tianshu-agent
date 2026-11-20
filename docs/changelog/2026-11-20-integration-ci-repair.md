# 整合候选首轮原生 CI 红与最小修复（2026-11-20）

## 历史与当前对象分开绑定

这是 [前轮本地整合门禁](2026-11-20-preview-release-integration.md) 的后续，不覆盖或重标原日志。
原本地冻结通过后，协调者正常 FF 更新原 PR [#198](https://github.com/lorek666/tianshu-agent/pull/198)，
并标记 ready。其 head 为 `d76ad9a90dedfc274d7359e25f1f9bec25116747`，base 为受保护政策主干
`2791220a20c3b24f94b004d55b8522090d7706d7`。

真实 ready CI [run 44670311820](https://github.com/lorek666/tianshu-agent/actions/runs/44670311820)
attempt 1 **失败**：Windows 两片共有下列五个具名失败，macOS 真实触发25分钟作业上限；
不是排队失败，也不是仅 `gh run watch` 网络错误。协调者下载原始日志及 Windows 分片报告，
校验 artifact digest、run/attempt 与实际 checkout；Windows 两片报告中的 `github_sha` 和
实际 checkout 均为合成 merge `a102511c41d306fa45a83906cbf623535d308a08`。
Ubuntu、UI 与原生 Windows Rust 当轮成功，不替代失败的 Python/汇总。先前 draft-push 的
skipped run `44670249503` 也不算绿色。PR 随后恢复 draft；本轮未盲重跑、加时或削减套件。

新本地代码/测试冻结：

- commit：`7b4d80bf3030219790f35694279b0055830f2846`。
- Git tree：`ea3249dd3b9b9bdd91f3e14dd62cadcb7362a507`。
- 非 Markdown SHA256：`bed11199e35e7431e30fe4769c8ae36b77761930bd78478adef8c7d5112a662c`。
- 相对 d76 的完整九文件 patch SHA256：
  `292636cb67b2200a756b557c7ad2fb69edc54f555e15b76f855b6430536c1a78`。
- 主干在提交及全量前分别只读核对，仍为上述 `2791220`，已是祖先；没有无意义新增 merge。

非 Markdown 指纹算法保持不变：取 `git ls-tree -rz --full-tree HEAD` 的原始记录，剔除路径以
`.md` 结尾的记录，保留原顺序，以 NUL 连接并带尾 NUL，再计算 SHA256。本次全量后的文档回填
仅改变 Markdown，必须以 diff 和相同指纹证明未重开代码树。

## 五个具名失败及修复边界

| 原失败节点 | 证据区分与最小变更 |
|---|---|
| `test_backup::test_restore_reconciliation_reports_pending_work` | Windows 按既定 #200 能力边界正确报 `durability_unavailable`，原正向用例越过该边界。两平台均先只读检查保存备份中的 pending intent、approval、非终态 run、live lease；不可写平台严格检查 facade 的真实异常 cause/category、无 batch/phase，以及目标、暂存、备份和 registry 零变更。POSIX 原完整成功恢复及报告断言保留。参数化只能关闭能力，不能强开 Windows 写入；不新增 blanket skip。 |
| `test_w15_safe_gc::test_approve_reclaims_only_safe_targets_truncates_wal_and_preserves_replay_and_backup` | 只读备份检查可能留下 SQLite WAL/SHM；无目录同步能力的静态预检正确优先拒绝 companions，夹具并非干净离线。备份验证后仅经正常 `EventLog` RW open/close 恢复该前提，并断言 WAL/SHM/journal 全不存在；原二次 GC 成功、幂等与零删除断言不变。 |
| `test_w15_safe_gc::test_run_command_stdout_and_stderr_spill_blobs_protected_and_verified` | 同一夹具前提导致原期望 `reference_unknown` 实得优先级更高的 `incompatible`。同样只用正常 SQLite 生命周期收尾；全部篡改/缺失流引用负控仍精确要求 `reference_unknown`、退2及零删除，不调整产品错误优先级、不手工 unlink companions。 |
| `test_release_draft::test_asset_list_emits_exact_indexed_files_and_index` | 原 Git Bash 的真实 shell 无 stderr 退1。实际 workflow shell 与真实 jq 的受控 LF/CRLF 传输实验区分出相同失败机制；仅 Windows 测试 helper 为原生 jq 请求 `--binary`，匹配生产 Ubuntu shell 的 LF 传输。真实 JSON CR/LF 名、隐藏额外文件、重复/不安全名、目录和精确索引集合负控保留；不 strip 名称、不手写 JSON 验证器。新原生 CI 仍须验证同环境结果。 |
| `test_restore_bc_review_regressions::test_legacy_material_scan_fails_closed_on_nested_entry_added_after_descent` | 这是产品扫描缺口，不是平台装饰器遗漏：下降后真实新增孙项，父检查仅靠 child metadata，延迟/未变化的目录 stat 可隐藏其内容。保留原递归身份、普通目录、链接/junction、缺失和直接子项检查；逐次调用累积已观察目录，最后做一次扁平真实内容复核，检查前后状态及全部子项闭包，任何材料/新增目录/删除/替换/不可读均 fail-closed。 |

树扫描每个目录保留原两次列举并增加一次最终列举，已保存状态及总观察成本为 O(N)，不是在每层
重复递归整棵子树。真实 late-file 配合冻结 child stat 的区分红控、final-sweep 故障负控与深链
列举上界均保留。原 child-identity race 的注入点仅随递归实现移到私有 helper，严格断言不减。

上述 GC 局部能力路由与 jq 换行投影是机制证据，不是 Windows 原生实测。GC 的原机制正控曾用
外置正常 close 注入；最后对已写入源码的夹具验证明确关闭外置注入，不能把两层证据混称。
生产 GC、受管恢复写入能力政策、`release.yml`、资产白名单、source profile、依赖与 Windows
诊断均未改变；唯一产品源码变更是上述旧材料空树扫描。

## macOS 预算冲突：拆完整职责，不加时减测

原原生作业先做约200秒 Rust 工作，再运行已耗约1298秒、仍未结束的 Python 套件，最终超过
25分钟上限。该时序来自原始日志，不是将本机全量耗时投影成 runner 结果。

- 原 `test` 矩阵只保留 Ubuntu，原 Python 命令路径不变。
- 新 `macos_python` 在 `macos-latest` 完整执行 Python install、lock、sync、Ruff、SBOM 及
  `uv run --frozen pytest -o addopts="" -q -rs`，保持25分钟上限。
- 新 `desktop_macos` 在原生 macOS 完整执行原 Rust fmt、test `--locked`、clippy
  `--locked --all-targets -- -D warnings`，保持25分钟上限。
- 两原生作业均显式 checkout `${{ github.sha }}`，保持原 push/ready-non-draft 门禁。
- `macos_summary` 在 Ubuntu 保留 required 名 `test (macos-latest) · py3.12`，自身亦为25分钟；
  `always()` 与原 draft gate 同时存在，显式依赖两原生作业，严格要求二者均为 `success`。
  真实 Bash 执行 success/failure/cancelled/skipped 的全部组合；取消、跳过或失败均不能报绿。
- exact-checkout 丢失/改为 main 的负控、动态 draft gate 与 release 原 all-jobs-success 过滤
  对新增具名 Mac 作业的 failure/cancelled/skipped 负控均保留。四个 required 名不变。

这只消除 Python/Rust 共用单个作业预算，不保证 Python 在新原生 runner 上必然及时完成。
新托管 CI 仍须提供实际时限证据；不提高 timeout，不新增 Python 分片、不减少完整套件。
Windows 双 runner、原包装器/fatal handler/60秒诊断及其能力边界均不动。拓扑见
[CONTRIBUTING §5/§6](../../CONTRIBUTING.md)。

## 本地红绿、唯一新全量与静态结果

日志在作者独占工作树的 ignored `.omc/artifacts/` 中，计数与逐项跳过均以原始日志为准：

- `ci-repair-nested-macos-red.log`：旧实现/旧拓扑的定向红，SHA256
  `f12230db98080da37cf041eefbf8f77e714a20878782c00df40c068ccffd2cba`。
- `ci-repair-nested-macos-green.log`：最初命令的 `-k legacy_material` 仅选中树扫描，未覆盖 CI；
  随后单独运行 CI 测试并追加同日志。保留真实顺序，不将第一次命令冒称两域都已覆盖。
- `ci-repair-affected-domain.log`：backup、restore review/durability、W15、draft、release/CI
  workflow 的完整受影响文件 exit0，SHA256
  `3bfdde843379e5baef82452858a6625be1dc9417c7824575ab20227d2da6b171`。
- `ci-repair-final-macos-summary.log`：最后补齐汇总25分钟上限后的 CI 域 exit0，SHA256
  `f38d7cc8808936083068919e16367270336befe78504d20d7a29053a3639b3a2`。
- `ci-repair-gc-capability-green.log`：已修源码夹具、无外置 close 注入的局部能力投影 exit0，SHA256
  `950f0e25669ce93b46c1649fe41d2c97c75673537ae4fe111c04e9f8b8870d69`。
- 冻结 `7b4d80b` 后只启动一次新最终全量，2026-11-20 自然完成 exit0；原始日志
  `final-full-pytest-ci-repair.log` SHA256
  `fa843f94f3639b81bf1239d16dfeb23ecf907e4026a222a072e4e24a3c5805ea`。
  实际本机耗时904.25秒；执行前后 HEAD/tree/非 Markdown 指纹一致、工作树干净。
  本机条件跳过的 Windows、摘卷、冻结评测、soak 等未改写成已运行；这不是原生托管 CI 结果。
- `uv lock --check`、全仓 `uv run --frozen ruff check .`、对 CI/release 两工作流的 actionlint
  及 `git diff --check` 通过。文档最终提交再核相同非 Markdown 指纹和相对链接。
  UI/`desktop/src-tauri` 无源码变化，不声称本轮另做 UI/Rust 构建或新设备验证。

最终全量沿用已审父命令，不使用自选 basetemp、不在父收集期预载子 startup：

```sh
env -u TIANSHU_TEST_LOCK_ROOT -u PYTEST_TIANSHU_REGISTRY_HOME \
  -u TIANSHU_TEST_REGISTRY_HOME -u PYTEST_TIANSHU_SMOKE_ISOLATION \
  TIANSHU_DATA_ROOT="$PWD/.omc/integration-isolation/data" \
  TIANSHU_AUTHORITY_ROOT="$PWD/.omc/integration-isolation/authority" \
  PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 \
  TIANSHU_CREDENTIAL_BACKEND=env PYTEST_ADDOPTS='' \
  uv run --frozen pytest -o addopts='' -q -rs \
  >.omc/artifacts/final-full-pytest-ci-repair.log 2>&1
```

## 独立复审与尚待远端门禁

第二读者 `695897fa-5eb0-4284-aaf6-9c0ba35c2fd6` 的
`ci44670311820-actual-repair-review.md` SHA256 为
`de9a48b35a59c4f35767aef2a0ce446353723c048432d03b9bb6f306ffb4935d`。
其 **PASS 只覆盖上述九文件修复增量**，先逐字节审 patch，再独立确认 commit/tree/non-Markdown
指纹及 clean 状态。独立 no-CLI 执行覆盖 CI、actual draft shell、具名 Mac all-jobs 负控与新旧
树扫描边界；作者其他红绿日志由其回读 hash 校验，不冒称其独立执行。该报告不将后来新全量、
真实 Windows/Mac CI 或用户设备验收并入 scope PASS，也不重新声称逐行复审整个大型恢复实现。

代码冻结后只做本 Markdown 证据回填。协调者仍须将最终文档提交正常 FF 更新原 draft PR，
ready 后取得最终 head/base/合成 merge SHA 自己的 required CI，再按分支保护合并；旧红、政策
main 的旧绿和本机全量都不能替代。未创建 tag、dispatch 预览、公开发布或增加替代 soak 时长。

旧材料扫描仍只是有界协作式观察，不是原子文件系统快照或同 uid 物理隔离。自定义材料路径的
保留名祖先误识别 [#204](https://github.com/lorek666/tianshu-agent/issues/204) **仍未修**，本次
不扩大默认 AgentOS/events.db 以外的兼容声明。Windows 受管恢复目录持久化屏障依旧不可用；
source profile/测试投影不替代未改原生命令、安装包/真机、SLO、Keychain、核心任务、升级/回退/
备份恢复等各自最终 main 证据。长时稳定性仍未验证，取消首发强制 soak 不豁免其余必过项。
