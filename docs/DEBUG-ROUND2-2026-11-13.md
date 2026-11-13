# 第二轮系统 debug：修复与复审记录

本轮处理第二轮只读审计中的19项确认缺陷：6 P1、11 P2、2 P3，未确认P0。
原审计绑定 `69119f009096df74fc8e370d5d1c6d71cad3f290`；修复前重新 fetch，实际基线为
`cefa1e375974d96e2ec25e199f6605f6dcf2239b`。额外 DATA05（迁移备份错绑）已经由新 main 修复，
既有 foreign/stale 两参数测试通过，本轮销账，没有重复修改迁移实现。

修复在隔离工作树 `codex/debug-round2` 完成，三位作者按文件所有权并行；作者与第二读者上下文分离。
随后 main 仅新增工程教训文档，安全快进到 `c1bad0a8c06b7ba335958c97855f974589c76deb`，
保留其他会话全部内容，产品/测试对象未变。未改 PRD、工具 schema、依赖、Python 版本、CI 超时或分支门禁。
当前正式 head、PR、四项 required checks、合并与 main 自身 CI 由对应 PR 的最终记录绑定，不以本机候选预先宣称通过。

## 1. 真实基线与证据位置

本机 macOS 26.6.2 arm64、CPython 3.12.13。从 Git archive 固定基线，522个跟踪文件运行前后逐字相同。
所有测试子命令隔离 `TIANSHU_*`、provider配置与代理环境变量；仅记录变量名，不打印值，不改用户全局配置。
使用临时数据根、合成凭据、真实子进程及 loopback 服务，没有真实模型费用或用户数据写入。

| 基线检查 | 实际结果 |
|---|---|
| `uv lock --check`、`uv run --frozen ruff check .` | exit0 |
| `uv run --frozen pytest` | 2672 passed / 8 skipped，502.83s，exit0 |
| UI frozen install / typecheck / test / build | exit0；80 tests；3个重建dist文件与提交逐字相同 |
| [main run43428459428](https://github.com/lorek666/tianshu-agent/actions/runs/43428459428) | cefa1e3，Ubuntu2667/13、macOS2672/8、Windows2643/37、UI80，四required success |

原始命令、cwd、环境变量名、退出码、耗时、stdout/stderr和冻结hash保留在工作树被忽略的
`.omc/artifacts/round2-fixes-20261113/`（下称 E）。`baseline-ci-logs.zip` 为有效原生日志；
空的 `baseline-ci.log` 不算证据。原只读审计原件未改。仓库保留可执行回归与本索引，不提交本地数据库、密钥或海量机器生成日志。

## 2. 执行路径与风险覆盖

实际路径：CLI / 浏览器 → Host HTTP桥 → 认证IPC → Daemon driver / Worker → 两provider / MCP适配 →
ToolSpec与schema准入 → Dispatcher / execution_guard → 文件、网络、记忆或子任务执行 → intent/result/Receipt → 投影、恢复、replay。

| 入口与关键不变量 | 本轮实现与可执行证据 | 仍缺的证据或边界 |
|---|---|---|
| 审批恢复继续使用同一持久预算 | daemon AgentLoop装配；daemon_budget四场景、独立双审批IPC探针 | 未宣称所有真实provider计费档位硬上界 |
| 执行前最终授权、父版本与一次消费 | dispatcher/permissions/memory_exec；真子进程屏障、真实GET与spawn | 同uid协作守卫不是物理沙箱 |
| 外部输入不发明成功或授权 | model/MCP真实适配器、真实manifest、真实stdio peer | MCP非文本内容仍依既有unsupported_content口径 |
| 有界deadline、帧与生命周期 | net总期限、IPC分平台读取；真子进程半帧/超限/close | DNS阻塞、认证握手期限及Windows用户设备未据本机证明 |
| 材料与事件解释恢复状态 | backup/data_cli原生parser、全事件版本、runs全集对账 | approvals/leases完整事件重建、恢复apply、隔离、GC未交付 |
| 摘要只能由保存历史解释 | replay/summaries；历史正文/来源/归属/严格数值与不变性 | 不评价真实模型摘要效果 |
| 页面提示对应已知结果，新视图不被旧回包覆盖 | MemoryController七时序回归、真实Host/Daemon/dist浏览器验证 | 本轮聚焦记忆闭环，未重做所有桌面用户旅程 |

## 3. 确认缺陷、最小修复与回归

以下各项均在cefa基线重新复现；“已修”为独立内容复审结论，平台与正式提交门禁另见§5。
测试路径均在 `tests/`；各文件可用 `uv run --frozen pytest -o addopts="" -q PATH` 单独复跑。
破坏性反例只改测试临时材料或投影，不改真实用户数据。

| 编号 / 严重度 | 原触发、违反的不变量与实际行为 | 修复与回归证据 |
|---|---|---|
| AUTH02 / P1 | after_intent 后撤销、fence或cancel先赢，network/subtask提前分流仍执行；spawn还能继承新版未批准目录。违反执行侧授权及版本绑定。 | 最终guard中重核请求/claim/Run/审批/grant。网络短事务提交executing后释放锁再发包；spawn复核、父版本、配额和实体创建同事务。`test_round2_auth_execution.py`；独立GET阻塞、一次消费、3处硬退出77与恢复幂等。 |
| AUTH01 / P1 | depth1只读child写user记忆candidate或进入嵌套审批，实际改变持久状态。 | Dispatcher与MemoryExecutor独立核真实child身份，拒绝写及嵌套审批。`test_round2_auth_memory.py`；根写、child读正控保留。 |
| ROOT01 / P1 | daemon等待审批后resume装配漏BudgetLedger，重启或不重启都漏记账、绕request_limit。 | resume传同一driver预算。`test_round2_daemon_budget.py`四场景；限额不发第二请求但已写Receipt保留applied；无限额两调用均持久记账。 |
| INPUT02 / P1 | 两provider真实HTTP跨源redirect将合成key带到第二源。 | 模型transport按scheme/host/有效端口拒绝跨源，拒绝前关闭响应；显式端口0不当默认端口。`test_round2_inputs_model_mcp.py`，301/302/303/307/308×两provider第二源零命中；同源302正控。 |
| INPUT01 / P1 | MCP畸形result/content/block/text/isError被当空success、applied并出Receipt。 | 非法结构走既有protocol/unknown、无Receipt、对象阻断。真实stdio peer与后续新任务真实拒发；`test_round2_inputs_model_mcp.py`及fixture。合法空content与合法执行错误正控保留。 |
| DATA01 / P1 | 删除snapshot/undo后Receipt写前正文变清理候选，甚至丢正文仍可发布备份。 | Receipt blob_ref/evidence独立推导材料引用，受控文件Receipt须一致snapshot；缺证据拒绝发布/verify/cleanup。`test_round2_data_integrity.py`；删manifest声明不能藏缺材料。 |
| INPUT03 / P2 | 慢滴响应反复触发read而不超单次socket timeout，超过整次deadline仍运行。 | 同一绝对deadline中断实际socket，finally取消并join定时器；跨跳不重置。`test_round2_inputs_net.py`慢头/体、静默、取消与无遗留Timer。 |
| INPUT04 / P2 | provider缺失、None、数组或布尔arguments/input被`or {}`洗白。 | 区分缺失/非法wire形状与合法空object；OpenAI非object/半截JSON保留原串由执行侧拒绝。两provider同参数化，真实loop零工具执行。 |
| INPUT05 / P2 | IPC poll只见半帧，随后recv阻塞越deadline。 | POSIX缓存4/12字节头和正文，统一期限；RPC超时断开防串响应，event/server可续读。Windows独立原生消息读取。`test_round2_inputs_ipc.py`双timeout→完整帧→下一帧。 |
| INPUT06 / P2 | next_event漏8MiB限制，超限消息先分配或长等正文。 | 三接收入口均分配前检查，超限关闭不可复用；AF_PIPE pending close取消并回收。真子进程屏障，超限合法JSON排除JSON报错伪证据。 |
| INPUT07 / P2 | query-only redirect把`/dir/page?first=1`变成根路径查询。 | urljoin保留路径后再规范化/同源检查。`test_round2_inputs_net.py`真实HTTP请求记录及相对/根路径正控。 |
| DATA02 / P2 | snapshot version999/changes[{}]、undo缺items/伪completed/错run_id被宽松备份读取器接受。 | 复用SnapshotStore原生parser与UndoJournal.from_dict；snapshot只变static签名、函数体不变。坏材料拒绝，v1/v2/v3和合法completed保留。 |
| DATA03 / P2 | 非tool事件未来payload版本仍被restore-plan声称complete。 | 全事件先精确版本+真实event_schema验证，再汇总tool未决项；覆盖run.state/receipt/task.created及tool正控。 |
| DATA04 / P2 | DELETE runs后预演complete=true，遗漏活Run。 | 从run.start/state/finish/fenced归纳全集身份和最终状态，缺行/多行/矛盾拒绝。合法running/succeeded/fenced及独立cancelled/failed/noop正控。 |
| UI01 / P2 | 写已成功而刷新失败，或写响应断线，提示把不确定结果说成没执行。 | 分开写入与刷新结果；已确认成功保留notice，写断线说无法确认。`memoryController-races.test.ts`及真实浏览器截图。 |
| UI02 / P2 | 旧scope/list/search/写后刷新回包覆盖新视图。 | 共用viewGeneration，过期成功/失败不覆盖新选择；保留busy收尾与操作结果。七项确定时序回归、真实HTTP及浏览器晚到列表。 |
| SUM01 / P2 | 来源ref/digest/kind/ordinal、材料/记录归属损坏，正文仍在就replay ok。 | 按本Run保存历史重建build_material，逐项比对来源、身份、文本摘要；不用当前索引/模型。二审另发现int洗白REAL，已补version/bytes/ordinal严格存储整数。`test_round2_summary_provenance.py`17反例；原事件全文不变。 |
| DATA06 / P3 | format_version 1.9/true/"1"强转合法，Infinity产生非结构化异常。 | 五清单数值严格非bool非负int，CLI结构化拒绝；备份hash/mtime不变。 |
| DATA07 / P3 | DB与清单都写未来版本999时verify仍ok，和plan不一致。 | 独立verify及发布拒绝未来/负数真实DB版本；不改变当前支持版本含义。 |

## 4. 独立复审与负控

原件位于E相应目录，含完整argv/cwd/exit/日志/冻结对象。正式提交后还需逐Git对象绑定；
不同批次有重叠，不把以下数字相加冒充唯一测试数。

| 第二读者 / 范围 | 亲跑与原实现负控 | 内容结论 / 报告SHA256 |
|---|---|---|
| `/root/review_round2_auth` / AUTH | 35+242通过；6独立真进程/HTTP/crashpoint；换回cefa授权源7项业务失败 | 已修，`c22ad2c32d6e266d946083c442b532d19687de9d5135b46e5ad8acb858d0cf99` |
| `/root/review_round2_inputs` / INPUT + AST门禁 | 73新+235旧+28独立通过；cefa59failed/14passed；端口变异4真红；门禁32通过 | 本机已修，AF_PIPE待runner，`5c56271edad7ed1866ec507b641c3d2a3eda92543e868aab0cf84f733eca134c` |
| `/root/review_round2_root` / ROOT/UI/SUM v2 | 预算双审批IPC，UI87+7独立，摘要81+数值4+历史正控；cefa预算/摘要18红、扩展摘要17红、UI7红 | 已修，报告原件`review-root/REVIEW.md`及review-binding.json绑定10文件 |
| `/root` / DATA（实现作者为另一上下文） | 147回归+8独立场景+缺runs表；同源cefa34failed/5passed（其中3个TypeError只计接口差异） | 已修，`86287d860a94064639c1a28bc3a6e738b72f56e370338d6c8abaae52c863ac89` |

复审的失败尝试全部保留：SUMMARY最初REAL截断仍通过；预算观察器误等run.state终态；
负控父子CLI混源；错误测试路径/no tests；测试夹具未建schema；AST fixture误改函数名等。
这些工具/夹具错误不记产品缺陷或有效负控。修正后才采信真实业务失败与转绿。

首次最终组合全量：**1 failed / 2836 passed / 8 skipped，551.76s**，冻结277个非docs对象无漂移。
失败为旧AST门禁把新HTTP `opener.open()`误当文件`Path.open()`。保留文件字节门禁，
只识别紧邻`urllib.request.build_opener`赋值后的HTTP with调用；8个正式正负控及第二读者13种
真实文本I/O负控证明同名Path、重赋值、作用域、with第二项和body内文件访问仍拒绝。
未改产品来迎合误报，也未跳过/压制门禁。随后仅该测试文件改变，最终完整门禁：
`uv lock --check`、`uv run --frozen ruff check .`、`uv run --frozen pytest`均exit0，
**2845 passed / 8 skipped，500.10s**；277个非docs冻结文件前后零漂移。
UI frozen install/typecheck/87 tests/build均exit0，3个dist文件与已审冻结字节相同。
原件为E/logs/final-v2-*、final-ui-*及final-v2-integrity.json。

## 5. 本机、页面与云端边界

本轮真实浏览器使用ego-browser技能、真实构建dist、真实Host/Daemon及临时根。
测试Host仅对指定memory.list注入503或事件屏障，未替换业务写入/查询结果或伪造HTML。
截图`ui-browser/write-refresh-failure.png`显示成功notice与刷新失败同时存在；后台实际两次显式remember各一条。
`late-user-response.png`显示旧user列表释放后仍保留新task+query的空检索视图。
Task作用域按现行契约仍包含user，不把user条目出现在task视图误判越权；首次空列表假设已撤销。
重开Host及页面后两条记忆仍在。测试脚本首个Host被公共runner的25分钟上限结束，残余Daemon由重开Host接管；
最后正常stop_host，核自有PID已退出。原timeout/陈旧port观察器错误保留，不当产品故障或成功生命周期证据。

Windows基线1273.00s接近25分钟上限：原生durations显示eval真实task scope86.14s、100次随机crash66.75s、
CLI compare60.40s、scripted baseline43.40s；不是把这些等待一概称生产挂死。
本轮不增加job上限或重试。最终runner的durations/线程栈须另核，不能用cefa绿色替代新代码。

保留边界：模型同源POST307/308在基线与候选均为urllib http_status错误，只有同源302允许已验证；
DNS解析/部分连接系统调用不能由响应socket定时器强制中断；IPC认证握手期限未在本轮修复。
同uid运行、workspace路径及能力声明均不是物理沙箱。Windows AF_PIPE实际执行以当前CI为准，
runner通过也不代表Windows10/11或ARM64设备、安装/升级、所有桌面流程实测。

本轮未发现额外P0，不等于证明不存在P0或其他bug；本轮仅对表中可复现不变量与指定输入范围给出证据。
