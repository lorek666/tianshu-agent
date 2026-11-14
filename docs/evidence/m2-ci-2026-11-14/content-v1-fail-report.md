# M2 测试侧诊断候选独立内容验收

**结论：FAIL_CONTENT_CANDIDATE_PATH_ALIAS_OPEN。** 候选存在一项实际路径别名缺陷，不能签无限定 PASS。canonical 路径下的有界机制、异常/事务语义与容量探针已通过；这些证据不消除首轮失败，也不代表原 CI 根因、Windows 或 CI 准入结论。

## 对象与范围

- 基线：`b450c339d546ae2007ed9754e70de0108c4b86c4`。
- 冻结源：`/tmp/tianshu-g3-20261113/m2-diagnostics-root-frozen-csroc57j/source`。
- manifest SHA256：`d56715ae710c9b899b30dc9ec8e3817b51b64c0d0808125cf4805e434c1883f8`。
- 候选 `tests/test_m2_acceptance.py` SHA256：`9eb5ced6d9040aa593c3387bd29cd606faf0e115232017aa73dc6bdc1ee3eadc`。
- 独立可写副本：本报告上级目录的 `source`；1112 个源文件在复制前、复制后及运行后均逐项核对完整文件集、大小与 SHA256，无额外/缺失/改写。
- 对 b450 比较：唯一非 docs 改动为目标测试；另有12个 docs 差异，仅作不透明输入哈希，未读作者结论。src/scripts/schema/UI/CI/锁文件零改动。

所有运行显式设置副本 PYTHONPATH，并守卫产品与测试模块 canonical `__file__` 来自副本；没有 editable 安装回 live。使用现有 Python 3.12.13 macOS arm64 解释器，不代表 Windows 验证。未写 live/产品/测试源/提交，未运行 full/CI/API/付费，未将 root 的 full 结果作为本验收证据，也未读作者报告。

## OPEN 阻断：合法源码路径别名导致 caller 丢失

首轮目标集原件 `candidate-targeted-tests.log`：**1 failed / 10 passed / 29 deselected**，其中原60轮测试只运行一次且通过。失败是新增 `test_worker_diagnostics_real_transaction_caller_and_nested_reuse`，在候选1213行访问 `attempts[0]["caller"]["file"]` 时，caller 为 None。

这不是源回流或不同文件：`alias-input-identity.json` 核实 `/tmp` 与 `/private/tmp` 是同一副本/同一真实文件，canonical 文件守卫通过。候选 `SRC` 使用 resolve 后路径，但 `caller()`（694–707行）用未 canonicalize 的 `code.co_filename` 作 lexical `relative_to(SRC)`，合法的 `/tmp/...` 导入因此全部不匹配，真实 caller 丢失。`frames()`（673–691行）有同类比较，会将这类别名下的文件标识归为 `<external>`。

仅将验收 harness 的导入路径与 pytest 参数统一为 canonical 路径后，**只重跑该失败单节点**，得到 `1 passed / 39 deselected`（`canonical-single-node.log`）。没有重跑60轮，没有修改候选。canonical 成功明确了触发条件，不能洗掉合法别名输入下新增测试实红及关键 caller 丢失。此项保留 **P2 / OPEN_BLOCKING_UNQUALIFIED_PASS**，后续修复须另给精确候选。

## 六门的有界证据

1. **原契约/生产零变：静态通过。** 独立 AST 比较原 normal_finish 整个 body 完全相同，保留原6个 assert、range(60)、两处1ms心跳和暂停路径。生产与时限配置零差异。详见 `after-readback.json`。

2. **成功静默/捕获/beat：canonical 范围通过，受上项路径限制。** 原测试及新增静默测试在首轮通过；独立无 hold 真实 Worker 正控成功、无 worker.errors，stdout/stderr均为0。真实锁异常在 Worker.run_once 转成字符串前由 drive 捕获，码为 SQLITE_BUSY/5。独立探针确认原 beat 的前三次 busy重试、第四次错误对象保留、成功后重置计数、非busy立即停止、False静默结束；未加重试。

3. **事务边界/活跃元数据：canonical 范围通过，接受 reuse 标签。** 观测 attempt/acquired/body-return/exit 的实际 context 边界，另有 root 已裁定允许的 `reuse`，仅在真实嵌套进入时出现，不增加 active 写者；不代表新增 BEGIN。A/B BEGIN失败记录 enter阶段及对应真实caller；transaction错误瞬间快照含同进程 heartbeat 的已进入context/native_in_transaction=true。drive外层捕获时 heartbeat 已按原 stop/join 退出，active为空，符合原语义。未宣称精确COMMIT起点、外部进程或OS锁owner。

4. **原异常/事务/采集失败：有界探针通过。** 真实 body 异常与COMMIT故障均保留原对象身份；嵌套返回原被委托的tx对象；成功提交、body回滚后数据及in_transaction与预期一致。COMMIT故障的实际命令序列为 BEGIN IMMEDIATE→COMMIT→ROLLBACK，原异常不被清理覆盖。坏事件记录器不改变成功提交；frames采集失败不遮盖原body异常。候选的closed-connection及note失败用例亦在首轮通过。人工COMMIT/采集故障只证明观察器语义，不作CI根因证据。

5. **有界/敏感内容：所测范围通过。** 独立容量探针得到ring=128、errors=8、超限frames丢弃计数26；附注8800字符/8800 UTF-8字节，保留关键异常及caller，低于24000字符/每行1200字符上限。A/B实测分别12283/13217字符，字节数分别相同。候选不遍历cause/context，链存储上限0；cause自环不会新增遍历。异常消息canary未输出；候选自带的注册秘密/容量测试在首轮通过。此结论来自白名单检查与这些探针，不宣称对任意自定义元数据的普遍无秘密证明，也未把字符上限当字节上限。

6. **两个真实heartbeat BEGIN可区分：canonical有界机制通过。** 新独立探针各在一个隔离进程内运行真实 main＋heartbeat两线程/两连接，无外部写者。保留原busy_timeout=5000、1ms心跳和实际生产路径，只用屏障延长真实heartbeat事务：A在lease BEGIN，B在lease成功后的running BEGIN。两者均queued、相同五事件和真实SQLITE_BUSY/5；诊断分别给出 `leases.py:370 acquire` 与 `tasks.py:1300 transition`，drive frames分别包含/不含 `_acquire_leases`。无hold正控成功。关闭且仅关闭drive捕获的单点红负控实际exit1，被oracle以 `real swallowed drive capture missing` 拒绝。人工hold只证明这两处的诊断区分能力，原CI根因仍 NOT_ESTABLISHED。

## 审阅侧更正与原件保留

- 封存预备清单原先强制外部持锁子进程的文字已由root澄清覆盖：本轮使用单worker进程的真实main＋heartbeat两连接，不将其说成两个竞争写进程。
- `reuse` 为accepted deviation，等价于nested context-enter专用标签；不是封闭契约枚举或新增锁阶段。不要求新增cause/context遍历，不追加字节上限。
- 自写probe v1错误地要求drive捕获时仍有heartbeat active。原机制先join再向外抛错，因此两份v1在审阅断言处失败；其stdout/stderr与脚本完整保留。正确oracle读取transaction错误时的active_at_error。
- v2在进入产品前因复用已有数据目录触发FileExistsError，未执行机制；原件保留。v3只换新隔离数据目录，按正确oracle将A/B各运行一次，均exit0，约5.65秒。未改候选，也没有盲目重复跑样本。

新的独立探针脚本与输出均在本包；旧b450调查probe-v2/probe-alternative只作构造参考，来源hash见 `construction-references.json`，本轮未执行旧probe或读取其作者结论。首轮实红、canonical单节点绿、审阅oracle错误及预检失败均保留，不能将它们合并伪装成一轮全绿。

`facts.json` 为机器可读结论，`frozen-input-manifest.json` 与 `after-readback.json` 绑定完整输入；精确封存集合、大小、hash、模式见 `index.json`。本报告只覆盖9eb5ced6内容，不覆盖未来候选、最终Git对象、PR/CI准入、Windows或原CI根因。
