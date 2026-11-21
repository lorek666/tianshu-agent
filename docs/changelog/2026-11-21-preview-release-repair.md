# 0.1.1 组合修复：模型用量精确版本与发行身份

日期：2026-11-21。状态：**本地实现、限定独立整合预审及精确 `1c63e82` 的授权 source 全量、静态门禁与即刻复制留证已完成；PR210 首轮 Windows collection RED 保留，新 required CI、产品 App 原生验收和真实模型复评仍待完成**。这不是发行或真实评测通过声明。

## 原因与范围

旧基线 `a0732d9881247998df24db7af9f1c7989c1ae7a4` 已按保护规则合并；原 [ready RED 44670311820](https://github.com/lorek666/tianshu-agent/actions/runs/44670311820) 保留。修后 head `314e987ea5c8d1179caff6a4bcdf488c055f577f` 的 [ready CI 44678467012](https://github.com/lorek666/tianshu-agent/actions/runs/44678467012) 与 a073 的 [main CI 44681321379](https://github.com/lorek666/tianshu-agent/actions/runs/44681321379) 均为各自首次 attempt 成功，不是重跑旧失败。精确 a073 的 [release workflow 44731411389](https://github.com/lorek666/tianshu-agent/actions/runs/44731411389) 已生成未发布的 `0.1.0` draft/归档；静态闭环与 staged runtime smoke 通过不抵消归档 App 的原生 codesign 资源封套失败。旧 spctl exit1 也不构成有效 Gatekeeper 策略结论，GUI/安装/Keychain 等原生验收未完成。最终 App 资源封套修复见[独立记录](2026-11-20-outer-bundle-seal.md)，本次没有修补或覆盖旧发行对象。

C1 合法缓存用量不能通过 C3 v1 封闭 usage，真实模型响应后会在结果提交处失败；只改第一事件仍会在回合事件再次失败。[独立契约 PR #206](https://github.com/lorek666/tianshu-agent/pull/206) 已先合入主干 `53424e3e91f0d97732be8ff262baedb57313fe9a`，实施与契约没有混在同一 PR。

- 仅 `model.result`、`run.model_turn` current 升为精确 v2，旁表记录 2；全局默认仍 1，原 run.start/end v2 不变。
- 完整深拷贝旧 v1；全部原 v0 各自解释不变，尤其 turn v0 usage 仍为 open object|null。新 v2 可选缓存键为 integer|null、minimum 0，usage 仍封闭，input/output 仍必填。
- 新缓存字段负整数值数值 `-1.0` 被拒，合法 JSON integer `2.0` 仍接受；不改全局 validator、旧版本或 input/output 语义。
- 两个 JSON current 与历史 v1 同步；legacy 镜像不变。不删缓存、不增加任意 extras、不补历史默认值、不 UPDATE/DELETE 旧事件，不自动重放已发模型调用。
- pricing/budget/summary/授权/loop 未重写；非零缓存仍金额未知，已知 tokens/requests 与未知 micros 分维度处理。内存“已知费用和”为零不能冒充总费用免费。

## 单一新发行身份

旧 `0.1.0` 对象保持原样，组合候选用 `0.1.1`。仅修改 `pyproject.toml`、包 `__version__`、`control.DAEMON_VERSION`、uv lock 自家 editable package、Cargo package、自家 Cargo lock entry、Tauri 顶层版本。锁文件的第三方内容逐结构比较不变；无依赖新增、协议升版或全局替换。独立 UI 私有 package/lock/src/dist 不动。

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

## 仍待门禁与边界

新 required CI、产品 App build/发行实跑、原生用户验收与真实模型复评仍待完成，不能继承其他 SHA 的绿色结论。原真实首组 unsafe、失败、未知金额及未运行组不改判。旧 `0.1.0` 的原生 RED 不因本地封套结构探针或 source 全量绿变成 PASS；原生启动、Keychain、核心任务、SLO、安装、升级/回退、备份恢复等原门禁保持。新合入的 [Responses 续接契约](../openai-responses-contract.md)仍为待实现目标，本轮没有交付或宣称 Responses 运行时支持。
