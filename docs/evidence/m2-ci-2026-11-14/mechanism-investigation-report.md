# b450 Windows M2 queued/SQLite 锁错误：有界隔离调查

## 结论及边界

**原 CI 实际抛错点、持锁者和触发条件仍为 NOT_ESTABLISHED。** 发现并通过确定性屏障验证了一个产品失败机制：真实心跳事务持有 SQLite 写锁超过现有主连接锁等待后，主驱动产生原生 SQLITE_BUSY(5)，Worker 将异常降成字符串，随后把尚 queued 的 job park 为 waiting。未修复产品或测试；未变重试、超时、原断言；未推送或修改 live。

同一 CI 指纹可以从**两个不同主驱动事务边界**得到：租约 acquire 的 BEGIN，或 resumed 状态 transition 的 BEGIN。因此五事件及错误字符串不足以唯一归因。人为持锁超过5000ms是充分条件实验，**不是 Windows 原红根因证明**，也不证明 Windows 特有或 flaky。与 issue145/147 的共同根因未调查/未建立。

## 原件身份与原红

- 源：git对象 `b450c339d546ae2007ed9754e70de0108c4b86c4` 经 git archive 导出，完整1101个归档常规文件逐一 SHA256 与执行后的隔离副本相同，详细 `source-hashes.json`。
- source.tar SHA256：`4d8924f3ce2f0dd7d149b06b6cf129c05132cdd41fe0c26ab44e0feeaa1ef88a`。
- CI 原log：`/tmp/tianshu-g3-20261113/pr148-main-b450-native-ci/job-132363808565.log`，SHA256 `bdb430929caa1d37ae0da90371ea3c1b0155e87b9b478ef11336a71a979b2528`；原件未覆盖。
- run43567298082 attempt1 / job132363808565，log1817–1824行：原测试line726、iteration55、`worker.errors=['OperationalError: database is locked']`、queued != succeeded、run.end=[]、五事件 `run.start/job.enqueued/run.state/job.claimed/job.parked`。没有底层 traceback、sqlite扩展错误码、锁持有者或事务阶段。
- 调查目录 `/tmp/tianshu-g3-20261113/m2-heartbeat-rootcause-j9ba0w7y`；仅在此处创建源副本、探针、数据与报告。

## 源码事实

1. `Worker.run_once`在claim_next之后进入try（worker.py244–252），`_drive`普通异常在264–265仅保存类型和str，完整traceback随之丢失；finally267不论驱动异常均调用 `_settle`。
2. `_drive`先 `_side_channel`（413），再启动真实HeartbeatLoop（414–420）；`EventLog`与LeaseStore/JobStore构造可能产生SQLite写入（worker.py199–202、events.py367–372、leases.py328、jobs.py246）。因此原指纹不能仅凭后台线程存在就认定错误来自后台beat。
3. `AgentLoop.resume_run`在进入运行态前读claim、workspaces和leases，先 acquire缺失租约（loop.py872→764→leases.py370），再写running状态（loop.py873→tasks.py1300）。二者使用 `EventLog.transaction` 的 `BEGIN IMMEDIATE`（events.py412），原 busy_timeout 是5000ms（371），本探针未改。
4. 心跳走独立连接上的 `renew_claim`，先claims.heartbeat再leases.renew；两次独立事务（jobs.py201–215）。后台HeartbeatLoop对busy/lock最多进行三次重试后记录error（worker.py151–165），主驱动交易没有这一层重试。
5. `_settle`读到Run仍queued，非终态/非fenced且requeue=False，落入worker.py454的park；jobs.py665–685原子置job waiting并写job.parked(reason=queued)。这解释日志中的park是驱动异常后的**结果**，不是证明先发生了暂停/心跳失效。
6. `_drive`在420抛出后，with会先stop/join心跳，但421–432的beat汇总和loop.error报告被跳过。因此 `worker.beats=0` 不证明心跳没运行，未出现“心跳失效”前缀也不能证明后台始终无异常。

## 确定性实验（不是平台复现）

环境：Python3.12.13 / macOS26.6.2 arm64 / SQLite3.50.4。使用未改的b450产品源；`python -S`及显式隔离src路径和canonical文件位置断言，避免其他可编辑安装/插件介入。使用真实ScriptedModel写create_report再end_turn，与原用例动作同形；单个run，而非原60轮测试。

- **无hold正控**：`probe.py no-hold`，state=succeeded、job=done、errors=[]、产生receipt/run.end/run.finish，0.050s。
- **真实心跳持锁→租约BEGIN失败**：`probe-v2.py hold-heartbeat`，包装真实EventLog.transaction，心跳线程真正进入BEGIN并记录native_in_transaction=True后设置屏障；主线程只在屏障后进真实engine；直到原生主驱动SQLite异常出现才释放。没有手工raise OperationalError，没有改busy_timeout，没有sleep猜时序。约5.2s后SQLITE_BUSY(5)；完整caller链 `Worker._drive:420 → AgentLoop.resume_run:872 → _acquire_leases:764 → LeaseStore.acquire:370 → EventLog.transaction:412`。结果精确同五事件、queued、errors字符串、run.end=[]，job=waiting/park reason=queued，未写目标文件。
- **同一锁机制→另一事务边界失败**：`probe-alternative.py hold-transition`，待真实租约获取后，在main调用running transition前屏障等待下一次真实heartbeat事务持锁；完整caller改为`resume_run:873 → TaskStore.transition:1300 → EventLog.transaction:412`。约5.26s，仍为SQLITE_BUSY(5)、完全相同的五事件和queued/park指纹。这是原log不能唯一定位的直接证据。
- 架构是产品自身的一个worker进程/真实主线程+heartbeat线程/真实两条SQLite连接，**不声称“两进程并发”实验**。没有把同进程连接冒充另一进程。未运行原60轮或50/100次盲跑；两次heartbeat-acquire探针中的第二次仅补全原有caller记录，前一份原件保留。
- 两次最初探针在源文件守卫处因macOS `/tmp`与`/private/tmp` canonical比较失败，发生于任何产品env创建前，原log独立保留；修的是隔离探针路径断言，非产品。不是产品失败证据。

## 最小下一步证据采集建议（仅建议，未实施）

仅现有用例范围装一个测试侧、进程内有界诊断器；原assert、60轮次数、heartbeat间隔、TTL、busy_timeout与返回语义均保持。成功路径不输出；只在原测试异常时add_note有限摘要。为捕捉已经被Worker吞掉的异常，**不能等最终assert失败才开始采集**，需提前布置内存记录但只失败输出：

1. 测试侧包装真实 `Worker._drive`：遇异常时记录repo-relative file/line/function traceback帧、异常类型、sqlite_errorcode/sqlite_errorname、线程名/id、run/job/claim标识；随即原样raise。这样在run_once265压成字符串前保留实际边界；不创建假异常、不吞异常。
2. 测试侧包装真实 `_SideChannel.beat`：异常同样保存结构化帧/码并原样raise，让原HeartbeatLoop重试/停止；记录loop.error、stopped_promptly等时不能假定原 `_drive`异常后会进入其421–432。
3. 测试侧包装真实 `EventLog.transaction`，只为目标db的少量连接维护有界ring：connection代号、thread、单调时刻、调用帧、enter-attempt / real-enter-acquired / body-return / exit-ok / exception、`conn.in_transaction`与嵌套深度。外层真实contextmanager成功enter才标acquired；异常traceback行412/415/423区分BEGIN/body/COMMIT。异常发生时附当时仍acquired的本进程连接/线程及持有时长。**只记录元数据，不用SQL trace callback，不记录SQL文本/参数、locals、正文、异常对象repr或环境变量**；事件输出、总字符和环条数都应有上限。
4. 失败时读已经存在的目标run事件与job/lease当前投影，保留seq、type及短状态/owner字段，明确current不是历史持锁者。标注每个诊断采集失败而不覆盖原异常。
5. SQLite不提供“谁持有此写锁”的产品级权威表。上述ring只能确认**已被装配且被采样到的本进程事务持有者**；若无匹配持有者，报告unknown，不能从job/lease owner推断SQLite写锁owner。若要覆盖其他进程，必须给实际跨进程路径另加相关性证据；不能把本机屏障锁拥有者冒充CI锁拥有者。

下一轮若只加失败后note而无吞失前的有限记录，仍无法唯一定位。是否实施上述测试侧观测需另行授权；当前没有产品修复授权。

## 产物

`facts.json` / `commands.jsonl` / `source-hashes.json`；原始`hold-heartbeat.log/json`、`v2-hold-heartbeat.log/json`、`alternative-hold-transition.log/json`、`no-hold.log/json`；三个探针源码及source.tar均保留。探针exit0表示其断言证实预期失败机制，不等于产品通过。原main CI仍红阻断；本机正控和机制实验不销账原红。
