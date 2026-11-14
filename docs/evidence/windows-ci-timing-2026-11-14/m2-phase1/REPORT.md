# M2 c2cf Phase1：已定位 BEGIN 失败，根因待下一观测

版本：c2cf1c71c65f46dea161e41b140df99d137a5686；tree 35bc297ce3125be4fd8db6fc4087196507a94045。Windows shard0 原日志 job132390772492，由父代理给定 run43576167008 attempt1；日志自身核到该 c2cf checkout（L37/81/90/111）。选定源码逐字节对照隔离 bare 中精确 c2cf blob，见 inputs.json。原件完整 hash 与有限原行摘录也在包内。

本轮只做源码与原日志分析、诊断和屏障探针设计；未执行产品/探针/测试，未改 live、共享隔离 source、refs、重试、timeout、门禁或产品行为。本报告不构成修复或根因签字。

## 1. 原件已证明的最窄事实

用例在第13轮（从0计）失败；worker.errors=[]。run 事件已经到 execution.started，然后 job.finished/run.end/run.finish，run.end 为 failed，错误为“主循环异常：OperationalError: database is locked”。这与旧 b450 queued、五事件且无 run.end 的形态不同；不能自动同因。

固定异常是主线程 connection1、tx1413、nested=false，在 EventLog.append_many(events.py:508) 进入最外层 EventLog.transaction 时抛 SQLITE_BUSY(code5)，原始最后源码帧是 events.py:412 的真实 BEGIN IMMEDIATE。源码 busy_timeout=5000（events.py:371）。这定位到“写事务还没进入”，不能从本日志得出该 BEGIN 实际等待了多少。

当时 active_at_error 记录了 heartbeat connection1373 的 LeaseStore.renew tx1474。其 acquired 与 body-return 都为693843000000；异常采样为693875000000；exit 为693906000000（原件L1951）。相减得到32ms/63ms只表示观测时钟差。active_at_error 是保存的 acquired 项复制，native_in_transaction=true 也是 acquired 时的采样，未在异常瞬间重采连接/OS锁；body-return不等于COMMIT开始，exit在原生事务退出后才记录，还会包含调度/观测延迟。不能把这个63ms的观察context说成单个5秒锁，也不能把32ms说成主 BEGIN 的等待长度。原日志未记录 monotonic 时钟分辨率。

tx1413的attempt已不可见：ring容量128、最终仅打印32条；tx_dropped=5660。共用sequence分配connection/thread alias及tx，1474-1413不能当事务数。collection_failed=0表示收集器未报错，不补足已淘汰的等待窗。34.68s为整个失败test的call时长（前13轮已运行），不能充作单次busy等待。

## 2. 真调用链与竞争假设

后台：Worker._drive → _SideChannel（新EventLog连接）→ HeartbeatLoop._run → renew_claim。每次先读claim核身份，再 JobStore.heartbeat（jobs.py:206/495，独立BEGIN/COMMIT），再live_paths读取，然后 LeaseStore.renew（jobs.py:215、leases.py:621，另一独立BEGIN/COMMIT）。顺序是heartbeat→renew，不是renew→heartbeat。同一beat的两个写事务之间没有interval等待；成功beat之后才 Event.wait(interval)，第一拍立即运行。主线程 AgentLoop.heartbeat_once 也能调用相同renew_claim；不能看到renew就一概归后台。各caller/thread/connection需一起看。

本用例显式1ms；默认 HEARTBEAT_MS=5000ms。1ms不是实际周期保证，两次写入和读路径的耗时另计。源码足以提出“短事务连续重获写锁/原生busy轮询与线程调度竞争”假设：主线程busy期间可能多次错过写锁空档。它不证明 SQLite FIFO/饥饿机制、不证明本次发生，也不能直接外推默认5s。另须保留单次较长事务、未观测连接/外部进程、OS/IO调度等候选。本轮不改变任何等待或共享锁策略。

动作路径是 AgentLoop._advance 内的proposal驱动 → Dispatcher.dispatch → TaskStore.begin_execution（写execution.started后才yield执行permit）→ executor → Dispatcher._commit_result → append_many。文件正常结果与Receipt在4263一次提交；执行阶段其他Exception会走 _fail_unknown，在4006尝试tool.result+recovery.blocked。两者在当前caller诊断都只表现为events.py:508。_record_result(2838)也是源码候选，但正常已落结果时append=false；派发前拒绝或守卫拒绝路径与当前事件前缀更不相符。现有采样没有上层栈和batch类型，不能唯一签正常_commit_result或_fail_unknown，更不能签CI文件已写/未写。

主循环generic Exception在loop.py:1057-1059被捕获并调用_finish；它可正常返回到Worker，因此worker.errors空可由源码解释。run.end的none_applied来自尚未吸收tool.result的progress，不能反向证明execution.started后实际文件完全未动；这里保持结果未知的证据边界。

## 3. 最小下一观测：test-only，不再靠ring复原开始

A. 每个transaction wrapper将enter起点保存在局部token，不让ring淘汰：紧邻调用original context __enter__前记录enter_start_ns，捕获enter异常后记录error_at_ns和enter_elapsed_ns。elapsed是“wrapper等待进入”的墙上单调时长，包含Python/调度/收集开销，不命名为纯SQLite busy时长。现有attempt时刻也可保留，但说明它在caller/alias等收集之后和原生enter之间的边界。acquired后再留acquired_at_ns，normal/exception exit保留exit_at_ns，区分outer/nested及enter/body/exit；所有数据只用int/str，不保留frame/log/thread强引用。

B. 每个有限pending外层enter记录同进程其他outer writer的窗口汇总：pending登记时记已有active的ID/原acquired时刻，之后在acquired/exit观察时更新计数。分组键采用有限caller（file/function/line）+ thread/connection alias；单独统计attempt、acquired、exited，以及本窗口新入后完整观察到的context最大acquired→exit时长。carry-in、carry-out、成功/失败退出须分开；nested/reuse不当作新锁。设8个pending、有限writer组、overflow/eviction计数，error快照冻结该pending的汇总并随pinned异常优先打印；用同一短锁原子维护，不在锁内调SQLite或持锁执行原事务。

B只回答“同进程观察到多少writer context在pending窗口出入、是否有多次短context循环、是否有跨整个窗口未退出的context”；多次短context不等于它们占满所有锁空档，最大observed时长不等于原生锁精确寿命，也不排除未采writer/OS饥饿。active_at_error只作最后一刻context旁证。若用全局累计count的差值代替per-pending更新，须保证group稳定/有overflow且carry-in/out另列，不能借connection alias淘汰把同一窗口切碎后假算完整。

C. 最多收最近4个项目源码上层frame（file/line/function，不读locals/参数）用于append_many调用链；若必要只加batch的封闭type列表或count，不收payload、文件内容、模型正文、环境密钥或原始路径。优先用“跳过events.py本层后的上层src frame”即可区别_commit_result/_fail_unknown/_record_result；现有最末exception帧无法替代这个调用处栈。每item/total上限仍保留，关键elapsed/counters/type在优先字段，超限明示。

D. 一次性的测试元信息记录Python/SQLite版本、time.get_clock_info('monotonic')的implementation/resolution、用例interval、初始化时busy_timeout设置。不要在失败时对仍busy连接额外查PRAGMA；无需打印SQL/正文。失败原件保留，重跑属于新版本新attempt，不覆盖原失败。

## 4. 之后若另授真实屏障探针（本轮未执行）

最窄先做单持锁充分条件对照：独立连接运行真实renew_claim，在其真实LeaseStore.renew事务已acquired后，用Event/Barrier暂停事务体；主线程先等acquired再进入真实append_many/BEGIN。长持锁分支由主BEGIN自然抛原生SQLITE_BUSY后才释放holder（Event.wait有失败上限、finally必释放），不手工抛SQLite异常、不改5000ms、不sleep。它只能证明该类单持锁足以造成进入失败，不能证明CI就是此情形。

短持锁对照在主即将调用原生BEGIN的屏障后释放holder，期望成功；纯Python屏障只证明抵达调用点，不能证明主线程已经在原生busy handler内等待，故不能把这个对照写成精确“已等待后释放”。若要证明原生pending，需额外原生可观测边界，不能仅用thread.is_alive或延时替代。

连续短写假设应先靠B获得失败原窗证据，再决定是否加原生事务交接探针。让主线程在Python屏障外等，而让写者跑一串短事务，不是原生busy竞争证据。单连接/同进程线程探针也不能冒充跨进程证明；若要排查外部writer，另用真实子进程、身份及屏障。

验收下一诊断：在新精确SHA原生失败中，pinned错误能在ring大量淘汰后仍给出enter_start/elapsed、窗口writer统计/边界/overflow、append_many上层caller，且成功静默/异常原样传播/patch恢复。只对诊断新增信息验收；原红是否复现、锁策略是否应改、默认5s用户影响仍另裁决。
