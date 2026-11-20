# 限定预览、恢复与 GC 整合门禁（2026-11-20）

> 本文保留至 `d76ad9a` 的本地整合阶段事实；其后原 PR ready CI 真实失败，已另作最小修复。
> 最新冻结、失败原件及新全量证据见[后续 CI 红与修复记录](2026-11-20-integration-ci-repair.md)。
> 下文“未 push/尚待 CI/仅 Markdown”等均限定于当时阶段，不将原本地绿改标为托管 CI 通过。

## 合批与冻结对象

本次按 CONTRIBUTING §2 的同一发布门禁合批，在独占工作树与分支中本地整合；没有直接改 main，
没有 push、建 PR/tag、远端 merge、workflow dispatch 或公开发行。

- 主干基线：`e075c9cd2328e1c3846a9a15f6dad6aca47bcffb`。
- 独立首发范围政策：`3b035a93c69ce1676163d3da911c365a3ed8f10e`；取消首次 `macos-arm64`
  固定 soak 时长的政策：`d4ef379bc785de5d52379d2c15d5f291dff3924f`。均保留为祖先，
  不设替代时长；长时稳定性未验证，四目标正式发行要求不变。
- 恢复 B/C 与 W15：merge `9bde699027f53bfc7719aafc302c15a466aea7a9`；它已包含
  `aa1aed9237b21983c06737cec205a18e79bf75c9`，没有重复另合 GC。
- preview 实现来自 `7e9cdaf4871b200b3ff4fda9102fca03aad5f0c4`、
  `8c2efcda44b90d77ea9f87a9dc40c670d816893c` 与原证据
  `b346cb976712e88cff136e6a6322edf442b495b9`；cherry-pick 后分别为 `316198d`、`72cdee7`、`6f009e4`。
- 实际主干已同步至受保护政策合并 `2791220a20c3b24f94b004d55b8522090d7706d7`；fresh fetch
  后正常 merge，因政策原提交已是祖先，本次同步只新增合并祖先，Git tree 逐字节不变。
  该精确政策 main 自身的 push CI [run 44666660163](https://github.com/lorek666/tianshu-agent/actions/runs/44666660163)
  attempt 1 已完成，四 required 齐备且全部 job success；本次已只读回核 head/event/branch/jobs。
  此绿仅覆盖政策 main，不替代尚待提交的本组合实现 CI。
- 新测试隔离修复：`a7ebd5f61a35965bd5f96220d87acc3fd4ee897f`；其后仅测试侧修正
  Windows venv launcher/base interpreter 绑定：`9487ad5`。
- 最终非 Markdown 代码/测试冻结：`2ce373df7ffc700ff763860f388c78f9e4da24f6`；
  Git tree：`044e8bb6e66cbdf7d372abac2c0fce6d4c2072c4`。
- 非 Markdown SHA256：`266266273557e0e7a53c2ddb6331e893272197b461479c2d2e0c0d6197c46ca9`。
  算法为 `git ls-tree -rz --full-tree HEAD` 的记录剔除路径末尾 `.md` 后，保留原序、NUL 分隔及
  尾部 NUL，再计算 SHA256。后续仅 Markdown 回填，不重开代码树。

恢复 merge 的非文档自动重叠仅 `_cli_ops.py` 与 `test_m2_acceptance.py`；保留主干
`8efdb6248364bcf8894788b30513908a5718bd3c` 的默认 worker 分批 drain 与相关回归。
相对恢复分支的 `src/` 差异仅该主干 drain 修复；preview 的 workflow、assembler 与关联测试
相对 preview 原对象逐字节未变。`ci.yml`、生产 verifier/runtime smoke、依赖锁及 UI/Rust 源码
未因本次整合变更。文档冲突保留首发范围、已合主干事实、恢复/GC 的 POSIX durability 边界及新 soak 政策，
未将旧 #198 的远端状态或旧本机绿色当成本次整合对象的结论。

## 新发现：两层 PYTHONPATH 重设会丢失测试隔离

`verify_release._runtime_smoke()` 与 `release_smoke.run_smoke()` 分别把 `PYTHONPATH` 改成 bundle/app。
维护锁与 registry 的生产根不接受测试环境变量；仅设置临时数据根或测试根，不能证明随后真实子进程
加载了 `tests/_startup/sitecustomize.py`。旧作者全量日志的退出码和原始字节仍保留，但旧“隔离环境”
措辞不能作为这些 case 的安全隔离证据；不据新结果倒推旧执行未访问生产根，也不读取生产现场取证。

本次只改测试：`test_release_smoke.py`、`test_release_verify.py` 与新增
`tests/_startup/release_smoke_test_launcher.py`。后者不会打包进产品：

1. 显式 fixture 为源码 smoke 与 verifier 的真实源码拷贝 fixture 建立本用例临时锁、registry 和
   authority 根；父进程实际函数返回值与环境绑定一致，处理 registry 两个环境变量的优先级。
2. 只替换受测模块自身的 `subprocess` 引用，不改全局 `subprocess.Popen`。先核原始完整 argv、
   `-P`、Host `--no-supervise`、cwd 与原 `PYTHONPATH`，不替坏生产命令修复路径；通过后只改
   测试入口和测试 `PYTHONPATH`。
3. 子 launcher 不补救导入：先核 `sitecustomize` 确实已从该 checkout 的测试目录加载，再核实际
   app/package/module 来源与锁、registry、authority 的真实返回根。失配在真实 CLI 前拒绝。
4. verifier 的外层测试角色在同 PID 调用未改 `release_smoke.main()`；其 Host/Daemon 仍由真实
   生产逻辑创建，在 guard 之后同 PID `runpy` 执行真实 `tianshu.__main__`。临时 bundle 的坏
   Host/Daemon 不会被仓库 src 偷换。
5. 每个已启动角色的实际 PID、包来源与实际根凭据，必须匹配父 `Popen.pid` 与运行结果；原有发行身份、
   token、state、退出、故障回收和 caller-cwd shadow 断言保留。缺凭据、错 PID 不可当成功。

无 CLI 负控覆盖父环境/实际根/argv/cwd 漂移、子 startup 缺失、环境绑定漂移、环境正确但实际 getter
错误，以及凭据缺失/错 PID。第二读者会话 `695897fa-5eb0-4284-aaf6-9c0ba35c2fd6` 独立核对
静态字节并运行无 CLI 负控，放行后才运行真实源码集成 case。

第二读者随后用无产品 CLI 的合成 launcher 证实：venv 包装进程 PID 可不同于解释器的凭据 PID。
生产内层 smoke 已用 `_runtime_python_executable()` 处理此 Windows uv 情形；新测试外层 fixture
起初仍取 `sys.executable`。修复仅让 fixture 一次选真实 base interpreter，并让 `_bundle_python`
与 guard 原始 argv 的可执行文件绑定同值；不静默替换错误命令、不放宽 `-P` 或严格 PID。
新增纯 spy 探针先红后绿：模拟存在的 wrapper 与真实 base，核生产 verifier 实际组装的 cmd0；
再使 cmd0 漂移为 wrapper，必须在真实 `Popen` 前拒绝。该合成探针不是 Windows 原生实测。

**这只是受 guard 的源码/源码拷贝集成 smoke，不是未改命令的原生发行包或用户设备 smoke。**
生产 verifier、runtime smoke、CLI、用户级锁/registry 与 HOME 语义均不变；完整原生验收仍留给
独立 runner 或干净标准用户测试 OS 账户，不 fake HOME、不把 fixture 当发行资产。

## 本次本地证据与失败保留

日志存于该独占工作树 ignored `.omc/artifacts/`，测试数据使用 pytest 默认的系统临时根；
独立凭据 service 与每用例锁/registry/authority 注入保持生效。

- 首条定向命令误传不存在的 `tests/test_cli.py`，exit 4、未执行测试，原日志保留。
- 将 basetemp 放入仓库 `.omc/artifacts/` 时，祖先名被现行材料域检测识别而拒绝写入；移至仓库内
  其他路径后，根外副本诊断用例又按实际 repo-relative 路径拒绝 `<external>` 断言。
  两轮失败日志保留，没有删断言或修改产品以变绿。恢复 pytest 默认临时根后，发行/恢复/GC/CLI
  定向回归 exit 0。现行祖先材料域识别边界不因此扩大或宣称已修复。
- 上述定向未包含真实 runtime smoke；guard 静态和无 CLI 负控通过后才运行受控真实源码集成，
  其原始日志 `smoke-isolation-real-source.log` 为 exit 0。
- `a7ebd5f` 的全量 `final-full-pytest.log` 为 exit 1，唯一失败是
  `test_os_lock_identity_ignores_mutable_environment`。原日志 SHA256
  `18715f9a97234dca6ed9b76ad5c2ad7ad95fb0d6d615801a560275591116bb92` 保留不改。
  父命令预载了仅供测试子进程的 startup，导致收集期冻结的“生产 getter”已是测试 lambda；
  原身份断言正确拒绝该测试命令污染。本用例只计算路径，不创建生产锁根或取得生产锁。
  修正父命令为 `PYTHONPATH=src` 并清除外部测试锁/registry/payload 变量；原 `conftest`
  仍逐例提供子 startup 和实际临时根，没有修改生产或放宽断言。原失败用例定向通过。
- `9487ad5` 的 launcher fixture 修复有 `launcher-portability-red.log` 的启动前红证，
  修后无 CLI/原 OS 身份检查日志 `launcher-portability-green-and-env.log` 与 release smoke、
  verifier、maintenance 全受影响域 `portability-affected-domain.log` 均为 exit 0。
- `2ce373d` 的最终全量已自然完成，exit 0。日志 `final-full-pytest-portability.log` 的 SHA256 为
  `0ca29560cdb2d43858d3e0f4c13c132968d2409d4ba05b0dbacfd575a6efa092`。
  执行前后 HEAD、Git tree 与非 Markdown 指纹一致；本机实际耗时与逐项条件跳过以该原始日志为准，
  不把本机条件跳过的平台/摘卷/浸泡/冻结评测改写成已运行。
- `uv lock --check`、全仓 Ruff、`actionlint .github/workflows/release.yml` 与 `git diff --check`
  通过。UI 与 `desktop/src-tauri` 相对主干无非 Markdown 变化，本次未重跑它们的专属构建检查，
  不声称新 UI/Rust 或 Windows 原生验证。

最终全量在仓库根使用以下父命令；不设置自选 basetemp，不预载子进程 startup：

```sh
env -u TIANSHU_TEST_LOCK_ROOT -u PYTEST_TIANSHU_REGISTRY_HOME \
  -u TIANSHU_TEST_REGISTRY_HOME -u PYTEST_TIANSHU_SMOKE_ISOLATION \
  TIANSHU_DATA_ROOT="$PWD/.omc/integration-isolation/data" \
  TIANSHU_AUTHORITY_ROOT="$PWD/.omc/integration-isolation/authority" \
  PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 \
  TIANSHU_CREDENTIAL_BACKEND=env PYTEST_ADDOPTS='' \
  uv run --frozen pytest -o addopts='' -q -rs \
  >.omc/artifacts/final-full-pytest-portability.log 2>&1
```

## 独立复审绑定与范围

第二读者会话 `695897fa-5eb0-4284-aaf6-9c0ba35c2fd6` 的最终 `review.md` SHA256 为
`6690f8bcd3536938f54baf5f1a4942fc6c2bf119827bca0f0f5d75eb8794c8b1`，独立绑定上述
`2ce373d` / Git tree / 非 Markdown 指纹。结论为 **PASS，仅限明示复审范围**：预览组装、
上传白名单、发行身份、latest-exact-main CI 与 draft 边界；双层测试隔离和 launcher P2 修复；
集成重叠保全及具名的高风险恢复/GC 回归。不是对整个大型恢复实现的新一轮逐行复审。

新树上的独立执行重新覆盖 release/getter 域、关键 restore/GC 回归、实际 assembler/default
差分/workflow shell 与假外部 gh transport、双模式无产品 CLI guard 负控、严格 PID 的
合成 launcher，以及真实源码集成各角色凭据。实际回读绑定父 PID、实际临时根与 repo/copy
包来源；旧 `a7` 的 REQUEST_CHANGES、旧全量红和历史隔离修正原件均不改标签。
该 scope PASS 不替代本次最终全量、required CI、未改原生命令、设备验收或公开发行批准。

## 保留的材料路径兼容限制

祖先标准材料域识别目前会沿父目录名匹配 `artifacts`、`blobs`、`memory_blobs`。非标准数据库或
异根 data_root 所用的私有 materials 布局再叠加这些保留祖先名时，可能被误认为另一标准材料域，按所有者不匹配
fail-closed 拒写。第二读者与整合者保留此未修兼容限制；默认 `events.db` / AgentOS 布局不受
该已复现组合影响，故不阻断首次 Apple Silicon 限定预览。将 pytest 数据恢复到默认临时根仅
消除了这轮测试布局碰撞，不是产品修复，不扩大为任意 `--db` / materials 路径支持声明。
未修问题与安全公开的对照复现跟踪于 [#204](https://github.com/lorek666/tianshu-agent/issues/204)。

后续文档回填和主干同步必须用 diff 与同一非 Markdown 指纹证明代码未变。云端 required CI、
真实预览 workflow、下载/索引字节、候选 SLO、Keychain、核心任务、升级/回退/备份恢复与用户设备
证据仍须各自绑定最终 main；本地结果不构成公开发行或远端保护门禁通过。
