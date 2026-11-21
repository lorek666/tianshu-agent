# 0.1.1 组合修复：模型用量精确版本与发行身份

日期：2026-11-21。状态：**组合修复已由 PR210 正常合入 `e87a38cbf62876a0e25451d60401f5fdb11f6e0b`（tree `6a49ca05d3512d7202c8544dde47d23f42796de1`）；新 ready/main CI 与 `0.1.1` 预览发行实跑已完成，draft 尚未公开。新归档 App 副本 codesign strict 验证通过、spctl 策略拒绝；真实源码 scripted control 退出90，仍暂停，产品原生用户验收仍未完成、真实模型复评未获通过**。各轮 source/UI 绿只归属其精确冻结；PR210 首轮 Windows collection RED 与既有真实评测结论保留，不因后续 CI 或结构验证改判。

## 原因与范围

旧基线 `a0732d9881247998df24db7af9f1c7989c1ae7a4` 已按保护规则合并；原 [ready RED 44670311820](https://github.com/lorek666/tianshu-agent/actions/runs/44670311820) 保留。修后 head `314e987ea5c8d1179caff6a4bcdf488c055f577f` 的 [ready CI 44678467012](https://github.com/lorek666/tianshu-agent/actions/runs/44678467012) 与 a073 的 [main CI 44681321379](https://github.com/lorek666/tianshu-agent/actions/runs/44681321379) 均为各自首次 attempt 成功，不是重跑旧失败。精确 a073 的 [release workflow 44731411389](https://github.com/lorek666/tianshu-agent/actions/runs/44731411389) 已生成未发布的 `0.1.0` draft/归档；静态闭环与 staged runtime smoke 通过不抵消归档 App 的原生 codesign 资源封套失败。旧 spctl exit1 也不构成有效 Gatekeeper 策略结论，GUI/安装/Keychain 等原生验收未完成。最终 App 资源封套修复见[独立记录](2026-11-20-outer-bundle-seal.md)，本次没有修补或覆盖旧发行对象。

C1 合法缓存用量不能通过 C3 v1 封闭 usage，真实模型响应后会在结果提交处失败；只改第一事件仍会在回合事件再次失败。[独立契约 PR #206](https://github.com/lorek666/tianshu-agent/pull/206) 已先合入主干 `53424e3e91f0d97732be8ff262baedb57313fe9a`，实施与契约没有混在同一 PR。

- 仅 `model.result`、`run.model_turn` current 升为精确 v2，旁表记录 2；全局默认仍 1，原 run.start/end v2 不变。
- 完整深拷贝旧 v1；全部原 v0 各自解释不变，尤其 turn v0 usage 仍为 open object|null。新 v2 可选缓存键为 integer|null、minimum 0，usage 仍封闭，input/output 仍必填。
- 新缓存字段负整数值数值 `-1.0` 被拒，合法 JSON integer `2.0` 仍接受；不改全局 validator、旧版本或 input/output 语义。
- 两个 JSON current 与历史 v1 同步；legacy 镜像不变。不删缓存、不增加任意 extras、不补历史默认值、不 UPDATE/DELETE 旧事件，不自动重放已发模型调用。
- 原模型用量修复本身未重写 pricing/budget/summary/授权/loop；后续主干 Responses 整合另有调用、恢复与成本接线变化，见下文。非零缓存仍金额未知，已知 tokens/requests 与未知 micros 分维度处理。内存“已知费用和”为零不能冒充总费用免费。

## 单一新发行身份

旧 `0.1.0` 对象保持原样，组合候选用 `0.1.1`。原版本身份修复仅修改 `pyproject.toml`、包 `__version__`、`control.DAEMON_VERSION`、uv lock 自家 editable package、Cargo package、自家 Cargo lock entry、Tauri 顶层版本。锁文件的第三方内容逐结构比较不变；无依赖新增、协议升版或全局替换。该身份修复未动独立 UI 私有 package/lock/src/dist；后续主干 Responses 同步另带 UI src/dist 变化，不能将本段当作新组合的 UI 零变化证明。

新增[实际七处身份绑定测试](../../tests/test_release_version_identity.py)，从真实声明和模块读取，不用手写替身；故意漏改 Daemon 常量会准确失败。仅真实 source-copy smoke 的 positive/negative 前置版本采用当前 `__version__`；`9.9.9` 漂移负例、shadow sentinel、自包含历史 `0.1.0` fixtures 不变。真实拷贝包的 release identity 更新后正常重算 manifest，不绕过 runtime 检查。

## 本地证据与保留的 RED

- 原 a073 的实际两个 adapter→实际 loop 在第一事件拒缓存；独立实际 `commit_model_turn` 在第二事件拒缓存，均是真实失败而非测试启动错误。
- 修复后[真实产品路径回归](../../tests/test_model_usage_v2.py)通过不联网的受控 Transport 保存完整规范化用量及精确版本；验证 result/turn/cursor/版本同事务、first/second 版本行和第二事件故障整体回滚，派发前已持久化，暂停/恢复不重发已发模型回合，重放/重建不加模型调用，未知 micros 占用不退款且幂等。
- 固定原 schema/payload/hash/version；精确旧 a073 完整 schema 源配合未改的 EventLog/data_admission，在重开全库准入处拒新 v2，早于 WAL/schema 初始化。连接、restore access、SQLite 打开已发生，不宣称全文件零接触或所有旧二进制均可安全回退。
- 新测试一度错误要求已开连接逐行读立即复核旁表 type；保留该 RED，仅将测试落在实际重开全库准入拒绝点。已开连接不重验 side.type 是原有边界，未借本修复改 events/data_admission。
- 首轮金额负控错返 int，类型错误不能证明账目 detector；原件保留。改用实际 Cost 零对象后，未知占用断言准确失败。分别回退任一事件、镜像漂移、旧 schema 污染、丢缓存、开放 extras、删除 float minimum、重放多发请求也都被实际断言捕获。
- 新 source-copy 版本门禁通过；`cargo fmt --check`、`cargo test --locked`、`cargo clippy --locked --all-targets -- -D warnings` 单次完整完成。仅普通测试编译，不是产品 App build/启动/安装验收。

早期离线定向命令沿用[贡献流程](../../CONTRIBUTING.md)的 source-only 父进程入口：清空测试锁/registry 注入变量，设工作树私有数据/权威根、`PYTHONPATH="$PWD/src"`、`PYTHONDONTWRITEBYTECODE=1`、`TIANSHU_CREDENTIAL_BACKEND=env`、`PYTEST_ADDOPTS=''`；`uv run --offline --frozen pytest -o addopts='' -q -rs` 后显式选受影响文件。没有自定义 basetemp、父 startup hook 或真实凭据。`uv lock --offline --check`、冻结 Ruff 和 diff 检查也通过。实际日志与各轮 code/test tree 摘要按协调记录保留，不用本文静态测试总数代替日志。

## 限定整合预审与冻结全量

- 独立第二读者完成整合静态审查及精确冻结增量复核，结论为未发现已确认代码阻断。其独立 schema/getter/history/alias/cache 动态探针 53 项实际通过；另 8 个真实 DB 探针在 SQLite 打开前被审阅守卫阻断，一次 FFI 加载尝试被拒，独立变异负控未执行。真实 DB/事务/恢复/重放的动态证据来自作者测试及本轮 source 全量，不能写成全功能独立 DB PASS；这些阻断与未执行边界均保留。
- 原冻结 `2b91bfc79fa7b0888a82cf1d015aa89735c0b034` 的组合全量实际 exit1，旧[精确版本测试](../../tests/test_low_disk_run_creation.py)遗漏了两个已批准的模型 v2 事件。仅补充这两个 current==2 断言及精确排除集，仍严格要求所有其他事件为 v1，保留原 run.start/end、历史 schema 与实际 append/version 断言；未改生产源码或机器 schema。受影响域回归通过，原 full RED 不改判。
- 正常同步主干待实现 Responses 契约后，精确新冻结 `e59415f1243122b9f31f3bc0612c32269d067518`（tree `1db617754594d4a42876c23de97df17f7a418107`）获准执行一次新的 source 全量，实际 exit0。命令为 `uv run --frozen pytest -o addopts='' -q -rs`，沿用上述 source-only 私有根，逐名 presence-only 核空真实模型 key 环境，仅在测试子进程移除无关继承 key；没有 `--offline`、自定义 basetemp 或父启动预载。跳过项目与动态总计以该次原始日志为准。随后 collector、`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 均实际 exit0。
- full 自然结束后立即执行 copy-only collector，留存 tiny-C 原生结构矩阵、两份真实归档及 source/source-copy 的包路径、PID/private roots 收据，缺失记录仍为 false。后续点查发现部分原始 pytest 临时路径已不存在，留存副本重新对照 at-time 收据核 hash/size，归档只读核成员与模式，不扫描其他临时目录、不重造夹具。初版附加后处理错误要求原路径持续存在而 exit1，原错误和诊断保留；获准后仅修正只读索引，不重跑 full 或原生夹具。

前次 `e59415f` source full 原始日志 SHA256 为 `7c499f4b78d48b6f60c7aea566411abae381f9bfabfbccc6872bc001a1553879`；即刻 collector 收据为 `a89fe3e568e8e0cc5101acdef29ad39ab6b4409f380377f4825318160e1b53b2`；留存证据索引为 `1049aa3eef24b799f020241819cd9efcc61a850a9d7d83986945545e922817c7`，按协调记录保存。复制收据只证明当时测试来源与留存内容，不证明当前进程已退出、零孤儿或未修改产品 App 的原生行为。

## PR210 Windows 收集 RED 与最小编码修复

- [PR #210](https://github.com/lorek666/tianshu-agent/pull/210) 首轮 [CI 44867867789](https://github.com/lorek666/tianshu-agent/actions/runs/44867867789) attempt1 的 head 为 `d82a032ce9e44f63bea205316a1c66c0a813737d`、base 为 `71eb57ef1544d126cc929b9172f5463a14999359`。两个 Windows 分片均在 `test_model_usage_v2.py` 的 fixture 默认编码读取处报 `UnicodeDecodeError`，collection 实际 exit2，尚未执行测试 body。这是本轮新增测试的可移植性缺陷，不是生产 schema 失败；原 CI RED 与原始日志保留，未重跑或改判。
- 最小修复 `1c63e8297020dad51f72494a9db3230ffc61ec4f` 仅给 `test_model_usage_v2.py` 与 `test_release_version_identity.py` 的八处文本读取显式补 `encoding="utf-8"`；fixture、schema、产品、CI、版本声明、locale 和守卫不变，也不新增持久测试。修复前后用同一已审驱动各起独立 pytest 进程，完成六次有限对照：fixture collect 的旧/新 exit2/0、schema 纯断言 body 的旧/新 exit1/0、身份纯断言 body 的旧/新 exit1/0。collect 对照均无 runtest，另两组各只执行指定真实 body；ASCII `once.md` 未声称单独动态 RED。驱动只在精确 allowlist 输入的默认读取处采用 CP1252，显式 UTF-8 原传；这是 Mac 窄路径编码模拟，不是 Windows 实测或 hosted CI 通过。
- 最小修复后一次受影响域回归实际 exit0，lock/Ruff/diff 均 exit0；独立第二读者完成 `1c63e82` 两测试编码增量的静态复核。原独立 DB 探针受阻、FFI 拒绝及独立变异未执行的边界不变，不因此改称独立 DB 动态通过。
- 精确新冻结 `1c63e8297020dad51f72494a9db3230ffc61ec4f`（tree `6bd04d16c66f3e27b78c82be3be7c6fc35f9c891`）获准执行唯一一次新的 source full，沿用上述完整 source-only 私有根命令，实际 exit0；随即 collector、lock/Ruff/diff 与外层驱动均 exit0。即刻留存副本逐项核 size/hash，原 at-time 缺失记录仍为 false，不补造临时原件；新全量的后处理未重新打开临时原件。证据归属执行时 clean 的精确冻结，不套用前次 `e59415f` 绿，也不将后续 Markdown 回填工作树当作该次执行身份。

新 `1c63e82` source full 原始日志 SHA256 为 `476d98575825cc4bc41714639704ae27e34d1acf39cdbf1dad89e5430720275e`；即刻 collector 收据为 `b6e0660064415c80d1678f4b04b76b85c2b8a662a2a0aca2b0ba1791b1fc387c`；冻结留存证据索引为 `01d3eecf045c2b3957f3fede6167e0592137665547392f30596994407e628f7f`，按协调记录保存。tiny-C、归档结构及 source/source-copy 收据仍只证明当时测试与留存内容，不是产品 App、当前进程存活性、零孤儿或原生用户验收结论。

## 主干 Responses 实现推进后的新整合

[PR #209](https://github.com/lorek666/tianshu-agent/pull/209) 已在 `f27a1bfe03140e918cdcaa249d3c5c0863638d28` 合入无状态 Responses 运行时，而非仅文档契约。本地从 `c777514959afb9ceec8b0d2be365005ce8ca80e1` 正常合并该主干，形成新冻结 `f88e2bd55f796da78fc5ca7346ee8b77b4e241a7`（tree `cce786edc46b842c30ba687c10196ac135e36679`），保留 `0.1.1` 身份和资源封套修复，并接收 Responses 的模型、上下文、恢复、成本、设置与 UI src/dist。两个模型事件的缓存 v2 与历史 schema 等价，新增 continuation 仅属于 `model.result` v2；不重复扩张版本或污染历史。显式 null 缓存保持未知，回滚结果不发布到已提交内存进度；不能为旧测试假设退回这些生产语义。

新生产/测试/UI 组合已完成自身受影响回归、限定独立整合预审及 `f88e2bd` 提交绑定附录；UI 冻结 install/typecheck/test/build 均 exit0，src/dist 字节与接收的主干一致。精确 clean `f88e2bd` 沿已审 source-only 私有根命令获准执行一次 source 全量，实际 exit0，随即 copy-only collector、lock/Ruff/diff 与外层驱动均 exit0；协调方另核 full 原始结果、辅助 streams/三份 native 矩阵、归档及 source-copy 收据和当前源码字节。原日志 SHA256 为 `c76631831b0191d71fe7c4d9eb6e41fceb6c09c385d23f28d43224ab8a60aa8f`，动态总计和跳过原因以该原件为准；冻结证据索引为 `b93cba3b4e9ddf31929fce6eeadd6987923fa792449bf452b2297eb8b746675e`，按协调记录保存，不复用前述 `e59415f` 或 `1c63e82` 的 source full。

主干 [push CI 44874423859](https://github.com/lorek666/tianshu-agent/actions/runs/44874423859) 的 Windows 分片实际超过 25 分钟上限被取消、严格汇总拒绝，不是主干全绿；末尾输出的测试节点不等于已确认根因。旧 CI/full RED、已知未知费用、独立 DB 守卫阻断与 FFI 拒绝均保留，不因主干代码或有限整合检查改判。Responses 原实施与线上未验证范围见[上游实施记录](2026-11-21-openai-responses.md)。

本地新整合树的真实完整有序收集代入未改的原 partition 函数后，[真实任务文件](../../tests/test_eval_real_tasks.py)已自然分到 Windows 分片 1；保持同文件归属、片内原顺序及互斥全集，因而完全未改 CI。节点分组不是耗时证明，不承诺在原 25 分钟上限内完成，也不补造旧取消任务缺失的报告。

本次 source 全量与即刻捕获沿用默认临时目录和逐例隔离，不预载父 startup、不新增 hook/plugin/CI identity/basetemp。空 key 与 env 后端不等于默认 urllib 代理/重定向零外发，也不消除默认 ControlPlane 构造时的 Keychain/ctypes 模块导入；导入不等于实际取密钥。collector 正向收据不替代 full 退出码、辅助原件矩阵或当前 PID 活性证明；旧独立 DB/FFI 阻断保持，不扫描旧临时根、查询旧 UNKNOWN 组或重造夹具。

## PR210 合并、精确新发行与本机静态观察

- [PR #210](https://github.com/lorek666/tianshu-agent/pull/210) 已按保护规则正常 merge 为 `e87a38cbf62876a0e25451d60401f5fdb11f6e0b`（tree `6a49ca05d3512d7202c8544dde47d23f42796de1`），没有直推或绕过保护。[ready CI 44887615950](https://github.com/lorek666/tianshu-agent/actions/runs/44887615950) attempt1 的 head 为 `e2ae5acd4d0f5ab06d8b619d5753118cd5c2f81a`，实际 PR 合成 checkout 为 `999070a43078d6bfd10e81c7072dedb9ed7b3379`，与 merge 的 tree 相同；随后 e87 自身的 [main push CI 44890059535](https://github.com/lorek666/tianshu-agent/actions/runs/44890059535) attempt1 也九项 job 全部 success。两轮四项 required 齐全，原25分钟上限未改；旧 Windows collection RED 与主干超时不被覆盖。
- 精确 e87 的 [release 44892061206](https://github.com/lorek666/tianshu-agent/actions/runs/44892061206) attempt1 的 preflight、`build macos-arm64`、assemble 三项 job success，生成 `0.1.1` prerelease draft（453596814，尚未公开）。两份 workflow artifact ZIP 与六份 draft asset 各只下载一次，原 validator 完整 CLI 单次 exit0；归档 `Tianshu-Agent-0.1.1-macos-arm64.app.tar.gz` 的 SHA256 为 `f832c8ea2c37b456f77c711e1a1705a98ce272d08918f1b359d27b6bc0b98a98`。这是新发行构建及校验链的证据，不是 GUI/安装或用户任务验收；旧 `0.1.0` 对象未修补、未覆盖。
- 新归档受限提取成功，逐项核对935个节点（863个普通文件、71个显式目录及隐式 App 根）；App 根0700仅为私有提取容器权限，不是归档记录或安装目录模式。2026-11-21 18:57（UTC+08:00），在 macOS 26.6.2（25G83）/arm64 上，对该精确新副本单次执行 `codesign --verify --deep --strict --verbose=4`，实际 exit0，输出 `valid on disk` 与 `satisfies its Designated Requirement`；stderr SHA256 为 `fd4120c9a29b3b7d45f96ec47888c47c8834527a99ccf0f936e7daedb0872b55`。随后单次 `spctl --assess --type execute --verbose=4 --ignore-cache --no-cache` 实际 exit3、`rejected`，stderr SHA256 为 `54b4a138e304c42f6babf3ff35e9c490f755bdd7d10b3db66a374cd723be1f7f`。这次是明确的系统策略拒绝，与旧资产资源封套操作错误分账；没有运行 App、重签或修改系统策略，前后副本字节与模式不变。结构封套修复不提供 Developer ID、公证或 Gatekeeper 放行，也不引入新的付费签名门槛。

## 新复评控制组暂停与仍待门禁

新 harness 的 synthetic suite 单次通过只覆盖假产品夹具，不证明真实源码的磁盘准入可用。2026-11-21 18:43（UTC+08:00）新 scripted control 的真实源码 crash child 记录 `DiskAdmissionError`，parent 记录 `crash_guard_invalid`，worker/controller 均退出90，未通过 control。原记录仅保存异常类，没有当时 rule、path 或 free 值，不能确定是 low 还是 unknown 分支；18:44 的事后可用空间观察为931012608 bytes，只支持低空间推断，不是事发时证明。原2GiB门槛未改，不注入空间、不移根或降门槛；现暂停、不重试、不进入 paid、不读 key。旧真实首组 unsafe、51次失败调用、旧进程组 UNKNOWN、未知费用及未运行组均不改判、不恢复重跑。

GUI/原生启动、安装、Keychain、核心任务、候选 SLO、进程恢复、升级/回退及备份恢复等原验收仍未完成；同账户控制面入口与 TOCTOU/provenance 等安全边界不变，不以第二 OS 账户或任何替代 soak 时长增加首发条件。首次 Apple Silicon 限定预览取消强制≥10小时 soak、无替代时长的原决定保持，长时稳定性仍未验证。上述 CI、发行校验、静态原生观察与 synthetic suite 均不能替代这些验收。[Responses 续接契约](../openai-responses-contract.md)的运行时已由主干实现，但本轮固定真实模型 job 仍走 Chat/OpenAICompatibleModel，不构成 Responses 线上覆盖；Responses 协议的官方与网关真实付费 smoke 仍为 NOT_RUN，不能宣称线上兼容或产品发行通过。
