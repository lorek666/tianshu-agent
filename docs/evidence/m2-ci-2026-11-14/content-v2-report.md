# M2 diagnostics v2 窄 delta 独立内容验收

结论：**PASS_CONTENT_V2_NARROW_DELTA_WITH_INHERITED_BOUNDED_EVIDENCE**。候选 `766a3496526594009638739bf093fd190d76d4adcd16c61acdf405504a0a4161` 在本次真实 POSIX symlink 输入下，已关闭旧 `PATH_ALIAS_CALLER_LOSS` 阻断。此结论由本次窄行为证据和精确哈希绑定的旧有界证据构成；Windows junction 本地尚未执行，不构成 Windows、CI 或 PR 批准。原 CI 根因仍为 **NOT_ESTABLISHED**。

## 身份与范围

- 新冻结：`/tmp/tianshu-g3-20261113/m2-diagnostics-root-v2-frozen-9_zzhznc/source`；manifest SHA256 `eb856c2981c40af0c841ee15fe1b7b291277630fee9847fbc618ea095798a919`。
- 独立副本：`/tmp/tianshu-g3-20261113/m2-diagnostics-independent-v2-delta-zqbcetec/source`。新、旧冻结及副本的完整文件集合、大小、SHA256 均独立核验。1112 文件中仅 `tests/test_m2_acceptance.py` 变化，其余1111逐字节哈希一致；副本运行前后均与新冻结完整一致。
- 精确 diff 与提供的 `canonical-delta.patch` 一致：`frames()`、`caller()` 各增加一次 `Path(...).resolve()`，新增一个两参数真实 symlink/junction 回归。无其他定义变化。移除新增回归和两次 resolve 后，整个测试模块 AST 与旧候选完全一致。
- 原 `test_normal_finish_and_pause_do_not_report_heartbeat_failure` 函数源码、完整 AST 及6个 assert 均不变；原60轮、1ms、暂停路径及其余语义按同源继承，本轮没有重跑60轮或 full。
- docs 仅用于不透明哈希核对，没有读取作者结论。未写 live、产品或测试源码；没有提交、API、付费或 CI 操作。

## 本次行为证据

运行器使用现有 Python 3.12.13，显式将产品与测试导入指向独立副本；每个 canonical `__file__` 均受导入守卫校验。保留 `/tmp` 导入拼写，使 `co_filename` 与已 resolve 的 `/private/tmp` SRC 保持原触发差异。

1. `run_narrow_pytest.py green`：**3 passed, 39 deselected，exit0**。涵盖原真实 transaction caller/nested reuse 节点和新增 original-src/external-copy 两参数节点。
2. `run_narrow_pytest.py negative-both`：仅在内存恢复旧两方法，源文件不改；**2 failed, 1 passed, 39 deselected，exit1**。原 caller 节点重现 `caller=None` TypeError；新增 original-src 在 caller 非空断言失败；external-copy 通过。这是预期负控红，不是新候选失败。
3. `run_narrow_pytest.py negative-frames`：仅在内存恢复旧 frames；**1 failed, 1 passed, 40 deselected，exit1**。original-src 在 frame 文件集合断言失败，external-copy 通过，证明 frames 回归也能检测未 canonicalize 的旧实现。
4. `independent_alias_probe.py`：**exit0**。独立装配真实 symlink、真实 `LeaseStore.acquire` / `EventLog.transaction`、真实 LeaseConflict，没有伪造 code filename 或异常堆栈。

根内真实 alias 与原 leases.py `samefile=True`、SHA相同，`co_filename` 确实是 alias 路径；caller 恢复为 `src/tianshu/leases.py:370 acquire`，acquire/_acquire_locked 两帧分别为相对路径的371/421行。根外同字节副本 `samefile=False`、SHA仍相同，canonical 路径确在根外；caller保持 null，两帧均 `<external>`。两类均真实产生 attempt/acquired/exit，返回后 active为空、原生事务已退出。附注及结构化观察数据均不含路径 canary、alias绝对路径、target绝对路径、source绝对路径或临时目录绝对路径。附注分别2065/1718字符及同数字节，仍在原字符上限内；这不是普遍无秘密证明。

## 旧 FAIL 身份与继承

旧候选 `9eb5ced6d9040aa593c3387bd29cd606faf0e115232017aa73dc6bdc1ee3eadc` 的正式结论仍是 **FAIL_CONTENT_CANDIDATE_PATH_ALIAS_OPEN**；没有把旧报告改成 PASS。旧包 `/tmp/tianshu-g3-20261113/m2-diagnostics-independent-content-36pnh8sh/report-bundle` 的54文件、801913字节、53 payload+self、所有哈希、0444/0555模式及无symlink再次核验。旧 REPORT/facts/index 原字节副本置于 `prior-v1-identity/`。

- 旧 REPORT SHA256：`e38efbc0695e84a5a31e71fbc41896389de60f8e12100c46efc8b2c15ac6c5b7`。
- 旧 facts SHA256：`a8964fcb2066ddb57cd7205ad3ffdeeb69f34b27521a231e5369d486725684e5`。
- 旧 index SHA256：`fb959a9b7fcffccf78abd3eba0be4af13bf8db0d40acbf0fc3901decf41b091d`。

按1111文件不变及模块AST精确对账，继承旧包已取得的有界语义证据：静默成功/drive捕获/beat重试语义；事务异常对象、nested reuse、COMMIT/ROLLBACK、观察器故障不遮盖原行为；元数据/环形队列/帧/字符上限及canary；真实main+heartbeat两线程两连接在lease与running事务BEGIN处的区分能力；仅关闭drive捕获的红负控。它们是**旧运行证据的同源继承，不是本轮重跑**。两处resolve相关的路径分类与隐私由本轮新证据覆盖。

旧 accepted deviations 仍生效：reuse仅表示真实nested context-enter，不能当新BEGIN或额外writer；真实机制是单进程main+heartbeat两连接，隔离子进程不是外部锁写者；不遍历cause/context的存储上限为0；字符上限不等于字节上限。旧人工hold仅证明观察器区分能力，不证明原生CI根因。

## 审阅脚本错误与未验证边界

第一次静态预检使用 `ast.get_source_segment` 取得方法正文时漏了 `@staticmethod`，在审阅assert中止。候选当时尚未运行；此记录是审阅预检错误，不是候选行为红。改为保留装饰器的AST-only归一化后通过，错误记录完整保留在 `preflight-v1-failure.json`，未删除或伪装为绿。

本轮未执行 Windows junction 的 `cmd.exe /d /c mklink /J` 分支；只能静态确认该分支存在，交由原生CI验证。本轮不消费主代理并行 full 的结果，不声称 full/Windows 已绿。授权 delta 范围内没有未决内容问题；原CI根因、平台原生执行和完整CI门禁仍由各自证据判定。

## 复现与证据

脚本、命令、Python版本、cwd、PYTHONPATH、精确exit与耗时在 `run-results.json` 及导入守卫中；每次stdout/stderr分别保留。独立探针的同文件判定、哈希、canonical路径、真实caller/帧和附注在 `independent-alias-probe.json` 与两份notes中。输入对账、完整前后哈希、精确patch及继承清单均随包提供。`index.json` 枚举除自身外全部payload的精确大小/哈希/模式，自身身份由独立封存回执给出。
