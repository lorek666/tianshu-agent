# 天枢 Agent 系统 debug 总账

本轮日期：2026-11-13。项目：`lorek666/tianshu-agent`。
基准完整 SHA：`ccff93e79cfeb93e47e5a35012fd92fae366e941`。基准从 fetch 后的 `origin/main` 确认，原主目录干净、打开PR为0；在独立工作树执行，历史工作树和其他内容不覆盖。

## 路径与证据覆盖

CLI / 浏览器 `ui/dist` → Host（HTTP/SSE、token与来源校验）→ IPC → Daemon控制面/队列 → Worker/AgentLoop → provider适配器 → manifest/schema准入 → Dispatcher审批/执行边界 → File/Exec/Network/MCP/Memory/Subtask执行器 → EventLog、投影、Receipt → 恢复/回放。

| 域 | 检查入口与关键不变量 | 实现 / 可复现证据 | 本轮结果与缺失证据 |
|---|---|---|---|
| A 授权 | 执行侧摘要/票据/claim/lease二次验证；一次批准；路径规范化 | permissions/dispatcher/guard；test_permissions、test_dispatch（含真子进程并发消费）、test_leases、test_job_claims、test_takeover | 已有真实并发/注入时钟路径通过；未发现新的已确认授权绕过。路径限制、同uid控制面不是物理沙箱；Windows跨用户ACL仍未验证。 |
| B 数据恢复 | append-only、同事务投影、intent/result/Receipt、三态、重复恢复 | events/recovery/snapshot/authority；test_transactions、test_failclosed、test_crash_consistency、test_recovery、test_exec_recovery、test_recovery_atomicity、test_undo_recovery | AB-01/02两个P1确认并修复；历史孤立unknown、人工unblock保留、MCP对象/authority补偿有证据。没有证明所有损坏库与调度交错都已覆盖。 |
| C 生命周期 | 真IPC认证/重复实例/停止；断线不空转；PIPE和MCP期限 | ipc/daemon/host/exec/mcp；test_ipc、test_daemon、test_slow_subscriber、test_integration_m5、test_host_cleanup、test_exec、test_mcp_transport_timeout | C-001/003为P1，C-002/004为P2；C-005为CI诊断器P2，C-006为测试夹具P3，C-007为Host元数据所有权/失败语义P2；其修后Windows实验仍在定位失败。Windows慢测按实际耗时、阶段与栈区分，不能把所有耗时归为同一根因。逃逸孙进程、物理隔离、Windows用户设备未补交。 |
| D 外部输入 | 真实manifest/schema、封闭结束原因、协议错误、MCP身份/发送边界、网络外发/上限 | model/tool_registry/toolschema/mcp/net；test_model_contract、test_g1_admission、test_mcp_registry/client/dispatch/crash/object_block、test_network_fetch/outbound_allow | DE-01/02与C-003联动，DE-03无界redirect正文已修；未穷举provider异常形状、公网DNS/TLS与DNS rebinding。受控响应不证明任意“OpenAI-compatible”端点兼容。 |
| E 记忆/上下文/预算/子任务 | 作用域/确认/忘记/擦除；来源/版本；哈希/任务可见；预占/结算；父子fence/取消/恢复 | memory*/contents/context/summaries/budget/delegation；test_memory*、test_content*、test_summary*、test_budget_ledger、test_budget_review、test_g2_delegation* | DE-01/02及独立复审B1/B2/B3确认预算缺陷；三态/未知不退款保留。已有记忆/内容/子任务相关测试未确认新缺陷；hash embedding与脚本评测不代表语义或真实模型效果。 |
| F 用户闭环 | 真实页面状态与真实文件/事件一致；断线重连有界；关UI继续 | ui/src/api/components、Host/Control；Vitest、真loopback响应、真实浏览器、真进程M5集成 | F-01/02 P2修复。实操创建、批准、否决、取消、重开、结果、记忆检索；没有把演示装配冒充三进程，也没用伪造截图。未穷举所有重复点击/乱序与桌面系统组合。 |
| G 仓库交付 | 文档/实现一致；四required；版本绑定；隔离与同步 | AGENTS、README、KNOWLEDGE、CONTRIBUTING、GitHub保护/CI、lock/ruff/UI dist | G-01 P3纠正旧门禁/Windows/摘要/数据根叙述。未改PRD/T1/依赖/Python，不提交运行数据或密钥。合并状态与复审证据须按下面具体版本阅读。 |

## 确认问题与原实现负控

| 级别 | 编号 | 可观察缺陷 | 修复/回归记录 |
|---|---|---|---|
| P1 | AB-01 | 命令已产生真实副作用，崩溃恢复却判未生效 | [恢复记录](DEBUG-RECOVERY-2026-11-13.md)，13项命令恢复用例 |
| P1 | AB-02 | 恢复自身二次崩溃留下unknown却丢失阻断 | [恢复记录](DEBUG-RECOVERY-2026-11-13.md)，10项原子性/旧数据补偿用例 |
| P1 | C-001 | 孙进程持PIPE写端导致有界执行永久等待读锁 | [I/O记录](DEBUG-IO-2026-11-13.md)，原2失败与默认期限真进程探针 |
| P1 | C-003 | MCP写入背压逃出deadline，无法返回unknown结算 | [I/O记录](DEBUG-IO-2026-11-13.md)，原3失败/1健康大请求正控 |
| P1 | DE-01 | 部分usage释放未知token预占 | [预算记录](DEBUG-BUDGET-2026-11-13.md)，两adapter普通/摘要8组合 |
| P1 | DE-02 | 负usage回补预算；账本接受非法数量 | [预算记录](DEBUG-BUDGET-2026-11-13.md)，两adapter与五维账本32回归 |
| P1 | B1 | 显式金额限额未接入模型调用，已知金额未进入账本，缺价调用错误结清 | [预算记录](DEBUG-BUDGET-2026-11-13.md)，普通/摘要/重试发送前预占、祖先金额限额与未知费用对账 |
| P1 | B2 | 历史负settled在升级后仍增加可用余额 | [预算记录](DEBUG-BUDGET-2026-11-13.md)，旧API生成负数库，新实现拒读/拒准入且四表逐行不变 |
| P2 | C-002 | 畸形Host请求裸异常断线，初修仍漏深层JSON解析错误 | [I/O记录](DEBUG-IO-2026-11-13.md)，原6真HTTP回归失败；第二读者深度探针原红，补充400后同TCP下一请求及认证顺序 |
| P2 | C-004 | 慢订阅连接关闭与短写竞态使sender抛未处理TypeError | [I/O记录](DEBUG-IO-2026-11-13.md)，真IPC客户端子进程与实际短写屏障；仅已关闭底层连接的错误转换为ConnectionClosed，开放连接/编码错误负控仍传播 |
| P2 | C-005 | Windows的原生定时栈诊断自身触发访问冲突，不能可靠诊断慢测 | [I/O记录](DEBUG-IO-2026-11-13.md)，同Python3.12.16/Server2025纯标准库反例；改Python线程定时采样，保留fatal handler、60秒时点与25分钟job上限 |
| P2 | B3 | 摘要协议失败留下未知预占，但run.end漏计未知调用并声称预算完整约束 | [预算记录](DEBUG-BUDGET-2026-11-13.md)，摘要错误回落、未知计数与账本对账 |
| P2 | DE-03 | redirect正文无界读取且不计入正文用量 | [I/O记录](DEBUG-IO-2026-11-13.md)，原262146字节→仅读最终2字节 |
| P2 | F-01 | HTTP正文停滞令UI永久busy | [I/O记录](DEBUG-IO-2026-11-13.md)，真loopback HTTP原失败 |
| P2 | F-02 | SSE收到头就清失败数，自动重连无界 | [I/O记录](DEBUG-IO-2026-11-13.md)，fake timers确定性原失败 |
| P2 | C-007 | Host元数据双删、旧快照/晚发布误删改新owner，拒读/损坏误当缺失；原Windows正常停止裸WinError5 | [I/O记录](DEBUG-IO-2026-11-13.md)，20项真进程回归；两轮候选被独立反例退回，最终dd49内容已审，修后Windows实验失败仍待核 |
| P3 | G-01 | 现行入口文档与代码、required配置相矛盾 | AGENTS/README/F1/exec修正，M5历史记录保留并加后续说明 |
| P3 | C-006 | MCP对象阻断测试不关闭客户端，14进程和28读线程留至atexit，污染后续用例与诊断 | [I/O记录](DEBUG-IO-2026-11-13.md)，原业务测试绿但资源探针红；13行finalizer覆盖正常、测试失败和构栈失败，独立观察器在自身清理前验证零残留 |

基准已确认共18项：8 P1、8 P2、2 P3，无已确认P0。候选整改引入的monitor问题归入C-007过程，不另充基准缺陷数。没有把缺实现的新功能、已声明限制或单纯可疑源码列为已确认bug。

第二读者独立找到Host深层JSON与B1/B2/B3，初始绿色不能作为这些边界已修的证据。预算后续 `51d46e06f230d50b8d7ec27eb5ce3d3c763c2a0a` 的二审又以两provider证实：摘要失败后若普通调用在发送前因金额不足被拒，终态仍从旧cursor漏计未知费用，因此该SHA的B3只能判部分修。5000位整数的Host输入候选经真实HTTP验证已由原ValueError边界返回400，已撤销。初始Ubuntu CI中的sender关闭竞态警告也在基准main存在，后经真进程屏障复现确认为C-004；没有守护进程退出或数据损坏证据，不将影响等级夸大，也不把绿色退出码等同没有后台异常。

## 验证版本和原始证据

- 基准本机：Python **2150 passed/7 skipped，375.82s**；lock/ruff exit0；UI76通过、类型/构建/产物一致性exit0。
- 冻结组合工作区：Python **2226 passed/7 skipped，374.22s**；lock/ruff exit0；UI80通过、类型/构建exit0，二次build哈希一致。这个结果是三批修复组合快照，日志HEAD仍为原基准；不冒充拆分后各SHA的CI。
- 本机：macOS26.6.2 arm64、项目Python3.12.13、Node24.22.2、pnpm11.20.0。测试子进程隔离相关provider与TIANSHU变量，设置临时data/authority根；Python3.14仅运行外部日志包装器，不是产品测试解释器。
- 基准main [CI run 43298476414](https://github.com/lorek666/tianshu-agent/actions/runs/43298476414) 四项success；Windows **2127 passed/30 skipped，1167.21s**，该次Server2025。
- 本地原始证据根 `/tmp/tianshu-debug-20261113`：`baseline-*`、`ab-*`、`c-*`、`de-*`、`f-*`、`integrated-*`；同名JSON记录命令、环境、SHA、开始结束时间与退出码，日志未提交仓库。C/D/E/AB作者报告及coverage矩阵存子目录。无效夹具尝试、lint/typecheck失败原样保留，并与真实缺陷负控区分。
- UI真实截图 `ui-cancelled.jpg`、`ui-result.jpg`、`ui-memory.jpg` 与 `ui-witness.log`：临时脚本模型/数据，不能声称真实模型评测。服务结束后端点已确认关闭。

## 交付批次

| PR | 初始独立复审SHA | 范围 |
|---|---|---|
| [#131](https://github.com/lorek666/tianshu-agent/pull/131) | `4479088ecb2d161755d7ba3a89e46119c94a8b8e` | I/O、UI连接及事实文档 |
| [#132](https://github.com/lorek666/tianshu-agent/pull/132) | `e89a95d4c7e8f7196b38c51148e294d4c72bed9a` | 模型usage与预算 |
| [#133](https://github.com/lorek666/tianshu-agent/pull/133) | `ab7706ba2a0ee837776f7c68b618f803b6f58258` | 执行边界、恢复原子性与总账 |

复审由新的独立上下文在detached工作树执行；作者不为自己批准。当前状态、后续同步SHA、精确CI链接和未决项以各PR的版本绑定记录为准；任何代码变更须重新审验受影响范围。

初始三SHA的四项required均已通过：[I/O run 43345340119](https://github.com/lorek666/tianshu-agent/actions/runs/43345340119)、[预算 run 43345346574](https://github.com/lorek666/tianshu-agent/actions/runs/43345346574)、[恢复 run 43345352145](https://github.com/lorek666/tianshu-agent/actions/runs/43345352145)。这仅是初始提交证据，不替代后续整改SHA的CI与复审。

Windows初始I/O套件为2140 passed / 30 skipped、844.37s；预算批2180 / 30、911.48s；恢复批2203 / 30、1090.74s。I/O的两个isolation setup为9.78s/9.71s，对比基准120.34s/120.26s，不再等待孙进程120秒退出。整套时间还含真实故障注入、脚本评测子进程与runner波动，不能将三批总时长差异都归因于同一个修复。没有提高25分钟上限或添加重试。基准Ubuntu为2149 passed / 8 skipped / 2 warnings（263.43s）；两个warning与I/O初始CI均为slow_subscriber sender的同一None handle异常，未谎称本轮新引入或已经消除。

## 历史整改与门禁快照

- I/O 独立第二读者 `verify_io` 绑定 `a0cb461702e538b22192eacc9b27fffc030a34ac`，原竞态探针与 3 项正式回归通过；父版本负控 1 failed / 2 passed，相关 68 项以线程异常警告升级错误方式通过。已审 Host/exec/MCP/net/UI 与 dist 对象继承核对通过。本机全量 2169 passed / 7 skipped（377.77s）。
- 预算独立第二读者 `verify_budget` 绑定 `21cd9b253e11ba71fcd8ad8fa610ab5646b42076`：初始 40 项及 B1/B2/B3 均已修，冻结 21 项组合探针通过，原两项失败在未改断言下转绿；关闭重开/replay、旧负账本四表不变和 claim/cursor 检查通过。整合最终 I/O 后提交 `a3ac288c7ce403bbcb020c99057a50e8e224202c`，本机全量 2243 passed / 7 skipped（387.42s），lock/ruff exit 0；该提交的云端结果见下述精确run记录。
- 恢复生产代码在 `ab7706ba2a0ee837776f7c68b618f803b6f58258` 经 `verify_recovery` 独立验收；按其建议，正式测试同时拦截两种 authority 首次登记，并核对 MCP before_authority 的 dispatch error。强化回归 10 passed，旧实现负控 2 failed / 2 passed / 6 deselected。合入最终 I/O 与预算后的完整组合本机全量 2266 passed / 7 skipped（390.29s），lock/ruff exit 0；243 个代码/测试/配置文件在运行前后指纹一致，第二读者已将强化测试、六组独立探针及对象继承绑定 `7dfed61db80a0124f3b934feb7f07356b2e25553` 验收通过；不把这份代码批准当成后续提交的CI批准。
- I/O `a0cb461…` 的 [run 43349177427](https://github.com/lorek666/tianshu-agent/actions/runs/43349177427) 为 UI/Ubuntu/macOS success、Windows failure，失败原件完整保留。Windows在60秒原生诊断输出时access violation/exit139；实际CPython3.12.16 x86_64、Windows Server2025、pytest9.2.0。后续隔离实验 `92545fa418478afd1be6886f70ba5f5470e24c0b` 的 [run 43353423204](https://github.com/lorek666/tianshu-agent/actions/runs/43353423204) 在同环境、无产品代码参与时复现原生采样0xC0000005；Python采样与无采样控制正常。它确认原生定时机制风险，不证明原业务失败的全部C指令因果；实验分支不合入main，实验完成也不是required CI通过。
- 预算整合 `a3ac288c7ce403bbcb020c99057a50e8e224202c` 的 [run 43351470704](https://github.com/lorek666/tianshu-agent/actions/runs/43351470704) 四绿：Ubuntu2242/8（235.76s）、macOS2243/7（602.87s）、Windows2219/31（870.64s）。恢复整合 `7dfed61db80a0124f3b934feb7f07356b2e25553` 的 [run 43351471256](https://github.com/lorek666/tianshu-agent/actions/runs/43351471256) 四绿：Ubuntu2265/8（278.07s）、macOS2266/7（628.85s）、Windows2242/31（1363.31s）。两份旧绿色不抵消I/O失败，也不代替后续诊断器增量的当前提交检查。
- Windows后两次总耗时差492.67秒；新增23项恢复测试的日志墙钟包络约18.10秒，共同eval慢项增加约205.89秒，原100次故障注入增加15.43秒。MCP测试在慢eval之后开始，因此C-006残留不能解释更早eval耗时；现有close能在0.295秒清空残留，未证实生产close泄漏。没有提高超时、增加重试或削减测试来取绿，剩余耗时波动没有足够证据归为单一根因。
- C-005新诊断器的独立内容预验由第二读者亲跑：19个真实场景（setup/call/teardown各实际60秒、GIL两侧65秒、fatal双侧真实abort、失败/中断/诊断异常、正常及同fd阻塞收尾）、10个配置边界与5项阶段扰动均符合预期。采样脚本SHA256为 `22c25d059f87cac109cc7d64374ecf43e7941555c7e75d315a8f1388ea79b111`；正式测试为 `b8fa248960a173faf30079db3972525e0bd931d86048c8a2def3dcd8802ba166`。新候选曾发现同fd覆盖缺口、20ms测试时序误红/误绿及有限超大timeout后台异常假绿，均有独立反例并整改；未发布的候选缺陷不冒充基准产品新增缺陷。这里是内容hash预验，正式提交仍须绑定SHA与四项CI。

## 诊断整改后的整合验证（2026-11-13）

C-005/C-006正式I/O提交为 `7111cbb43cd71db9f25a1348d91e7b39a0d97dee`。第二读者已核验精确8文件Git blob、交接哈希及280份原始证据并正式批准；报告 `verify-io-final-7111cbb-report.md` SHA256为 `60c90cab41ae45b54cc89ae60bc1cfded7f8a4cb237f60de8f9285b9219708b6`。上述19场景/10配置/5扰动和C-006的90项/两失败路径可继承，上述7111的C-005/C-006精确八文件范围无部分修/未修阻断；不含随后C-007，不构成整批I/O合并批准。I/O本机最终2200 passed / 7 skipped、390.82s，lock/ruff exit0；[历史7111 run43359223697](https://github.com/lorek666/tianshu-agent/actions/runs/43359223697) 已完成：Ubuntu2199/8、macOS2200/7、UI80成功；Windows1 failed/2175 passed/31 skipped/1107.46s，唯一失败Host停止unlink WinError5；31项诊断回归及三个60秒栈后用例通过但不能抵消整轮失败，不能由本机或原生隔离实验代替。

| 整合内容 | 本机结果 | 内容绑定 |
|---|---|---|
| 预算a3 + 已审I/O7111 | 2274 passed / 7 skipped，394.72s；lock/ruff exit0 | 322个跟踪文件在全量前后hash完全相同 |
| 恢复7df + 已审I/O7111 | 2297 passed / 7 skipped，396.59s；lock/ruff exit0 | 326个跟踪文件在全量前后hash完全相同 |

两轮仅测试子命令隔离相关环境并使用不同临时数据根，未修改用户全局配置。生产src、UI、schema、依赖相对各自已审a3/7df完全相同；新增测试/包装器按7111对象继承。结果后的变更只有证据文档回填。提交前日志仍记录原HEAD；`*-freeze.json` / `*-stable.json` 记录组合内容，该两组合后来已分别绑定本地556与4eb完整提交，并由第二读者 `verify-post-diagnostic-integration-report.md` 完成对象继承；尚未push且不含C-007，不是当前CI或合并批准。各PR正文记录最终完整SHA、当前CI与合并状态；本表不把未完成的门禁写成通过。

## C-007与最新整合状态（2026-11-13）

I/O正式提交 `dd49f21530899d2d643fba031a7fe46ea69ebbde` 已由独立第二读者完整SHA验收，报告 `verify-c007-dd49-formal-review.md` SHA256 `3e9778149d3fbbb0941436a8b33388511315a7deae24188c38430d2997b12d16`。源码68ea/测试3c68及245冻结文件精确匹配Git blob，本机2219 passed/8 skipped、407.48s，lock/ruff0；321跟踪文件Git/index/工作区一致，13核心及280历史原件hash不变。独立亲跑57/1及真实monitor、双启动窗口、stop新owner保护、supervise=True拒删/原异常；之前7个元数据交错以精确内容继承。未将d6ee的2217/8或a624的2218/8绿色沿用：前者真复现两个Host，后者新排序使未启动monitor被join、日志未关但退出0，均被退回。

[dd49 run43367761924](https://github.com/lorek666/tianshu-agent/actions/runs/43367761924) 已完成：Ubuntu2218/9、324.56s，macOS2219/8、630.64s，UI80通过；Windows **6 failed / 2189 passed / 32 skipped、1149.02s**。第二读者保全完整Windows日志，SHA256为 `15e8218f32705e09234b98fe4cbf97733d4ec67fada31baace03e5da673b4d0d`，实际checkout `fdf52734c489715adba95f56bd120715e8d6f820` 与dd49同tree。隔离修后Windows [run43368502126](https://github.com/lorek666/tianshu-agent/actions/runs/43368502126) 绑定 `cdc191f6a08f23373162580061946ce2bcdcf2ff`，2 failed / 18 passed / 1 skipped、34.46s；真实CRT拒读已产生HostError/PermissionError errno13但winerror丢失，测试硬求32误红；monitor探针主动关闭尚未服务的HTTP连接造成后台异常。实验的launcher/worker身份和持锁worker硬退出证据也不完整，须修正采集协议，不能删stderr断言或用collector0替代验收。

正式六红中的另外五项，第二读者在精确dd49测试/Host对象上以 `PYTHONIOENCODING=cp1252`、`PYTHONUTF8=0` 真实复现为5 failed：stale-stop两项EOF、晚发布winner缺失、第二Host错误ready、monitor EOF；保全的子stderr指向Host中文print的UnicodeEncodeError。仅在测试子进程调用生产CLI已有的 `cli._force_utf8_streams()`，不改src/断言/环境，同五项转为5 passed。原测试直接调用Host而绕过CLI初始化，且失败时丢弃stderr；正式helper将对齐真实入口并保全失败证据。报告 `verify-c007-dd49-ci-codepage-review.md` SHA256为 `c9aef076f05037b0f35a76fe5cf97774ce5cafdcf014b2eda531336e5861b30c`，37份原件台账SHA256为 `4d89a387d574a8dfb05ddbe79fb3a07c954301179d4fb5ec01264995ea713294`。本机monitor EOF不同于Windows10054，原CI丢失stderr无法找回，不把机制对照冒充原现场逐字归因；最终修正仍待Windows runner验收。

原版Windows实验run43363720150真双删10组有3组WinError5，普通读32、只读5、share0拒读假缺失；人工share-delete候选本轮无错，保留反证，不反推历史CI所有句柄交错。d6ee/a624反例、无效import覆盖负控及中断的无退出码日志均原样保留；测试/探针整改不另充基准缺陷数。

预算本地 `609c1f8cebd6e4bb10dad115bcad3d6038bb43bf` 已吸收dd49，本机2293 passed/8 skipped、420.63s，lock/ruff0且323文件稳定；独立关键预算正式组201项、冻结探针85项及旧负库fail-closed通过，正式对象复审报告 `verify-integration-budget-609-formal.md` SHA256为 `06101f5c0763c88dde186ae7a3a2567a79cc3d3a547ee59d87b7046d3dbc34a2`。恢复批基于4eb吸收609，只有Host源码及回归继承、证据文档合并，本机2316 passed/8 skipped、424.20s，lock/ruff0；327跟踪文件在全量前后hash不变。第二读者已亲跑151项及六组独立崩溃/恢复探针并通过内容预验，正式提交绑定仍待。上述结果均对应尚未修正Windows取证测试的组合，不沿用为下一测试版本的门禁。三个PR均未合，旧a3/7df四绿仍只属于旧提交。

用户要求全部修复稳定后集中跑CI：中间候选留在本地，先完成回归、独立复审和最终内容冻结，再集中推送。已运行的CI仍保留完整结果，不重跑旧版本取绿。三个PR各自当前SHA的四项required检查不省略；顺序合并后的strict同步或最终CI揭示的新缺陷，只补必要受影响版本。

最终正式测试 `dc274a06197b1b3fc67bbbad817d330e0242838b65c0976a1654c38a5d695048` 的独立内容预验通过：cp1252五项5 passed，精确a624仍因真实join/log_open失败；四种helper正常/异常路径保留原异常对象及完整stderr。报告SHA256 `0988a65c488bad22a6361b6c8751a6e177b6f60732fc1b4d67a8f7a4f4e69ae1`；Windows失败收尾的诊断专用新probe另验，不混入该批准。

三批最终工作区于05:26:11Z并行执行本地门禁，lock/ruff/full均exit0：I/O **2219 passed / 8 skipped、437.57s**，预算 **2293 / 8、439.61s**，恢复 **2316 / 8、442.55s**。248/249/251个非文档跟踪文件前后hash全部一致；冻结清单SHA256为 `84aa4946e608df310b1062637b76197afe9f5603756e2d006cebaf2981ba40eb`，原件 `*-final-batched-*` 及 `final-batched-local-nondocument-stable.json`。三个运行的日志HEAD分别仍为dd49、609、251，正式提交后须核Git/index/工作区与冻结内容映射；后续只回填文档，不把本机通过冒充当前云端通过。并行耗时不与旧串行时间直接作性能比较。

## 明确保留的边界

- 事件库与authority跨库不原子，重启补登记窗口仍存在；命令unknown不等于已核对全部外部副作用。
- 同uid控制面、workspace路径、能力声明不是物理沙箱；没有补Job Object、ConPTY或Windows跨用户ACL。
- 历史负预算不自动迁移；未知费用不自动退款，估算模式不冒充硬上界。
- 记忆forget不等于全部历史副本擦除；hash embedding没有语义能力；摘要/委派脚本模型只验证执行链。
- Windows10/11/ARM64、mac-x64用户设备、安装/升级/回退、真实模型UI、公网链路未验证，不从runner扩大支持承诺。
- 未穷举恶意MCP孙进程继承输出端、所有并发取消交错、所有损坏旧库或provider输入。事件版本缺失与人为吞内层异常等疑点缺可靠生产复现，未报确认缺陷。
- Python定时诊断无法保证在C扩展长期独占GIL时出栈；fatal handler与外部job期限不是该诊断缺口的等价替代。诊断专用fd阻塞时返回非零并保留活跃writer持有的fd到CLI进程退出，不声称该错误路径已经无线程/句柄残留。

本轮完成的每条负控能区分已知坏实现与修复实现；它们不能证明整个系统不存在其他bug。

本轮冻结清单的 `non_document_files` 精确含义是排除 `docs/` 目录，并非排除全部Markdown；AGENTS、README等根目录文件也在248/249/251条冻结对象中。正式版本绑定按精确路径集合和hash核对，不靠该标签推断范围。
