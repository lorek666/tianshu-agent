# 低磁盘 T2 独立规格与质量验收

日期：2026-11-14（Asia/Shanghai）  
独立读者：`/root/review_admission_contract`  
**Spec verdict：PASS_T2_FROZEN_CONTENT_ONLY**  
**Quality verdict：PASS_T2_NARROW_DELTA_AND_INDEPENDENT_EVIDENCE**  
本片已确认阻断 **0**；部分修 **0**、未修 **0**。六组规格义务在本报告所列范围内已兑现；四组质量检查通过。它们不是 T3、整功能、全量回归、原生 CI 或发布通过结论。

## 1. 授权、版本与输入

唯一候选是 `/tmp/tianshu-g3-20261113/low-disk-t2-root-ldhq_5tt/candidate-complete`。完整 543 源文件映射 SHA256 为 `589c6d3a5b7e2a1b2d2c3b47eb3f646aa035ddd03f32cc84e824a0e0b46a4c1a`；root facts 为 `99178f32443fb795033e06719ea3026c69e31d85b394e0c0022cfbd953b4b4c9`；相对已审 T1 round1 的 delta 为 `b4a00f9a852b03abe488c43abb552f5bbda8d7584050ca75bb84b041fe5d4adf`。基线 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215` 只是历史 HEAD，不能代替完整冻结身份。

唯一任务需求 `task-2-brief.md` SHA256 为 `1d8f62d6c575aa6b9cb2fb506f7f55f90e5d2f961405b9c34f0140b9ff9d43ae`；沿用 task-1-context Global Constraints。候选中的已合契约 SHA256 为 `2d47b609c3925c9e11622b534438d74855bf6e9c65f7893896e106e661fa0a1d`。输入原件另存 `inputs/`，索引见 `inputs.json`。旧 PREPARED_ONLY 准备稿没有覆盖或改写。

复审前已逐项核 543 映射与 root 完全相同，复制为本目录 `work/`；所有产品导入、运行与变异仅在该工作副本。冻结源和复原副本的后核仍逐项相同，且无额外文件、pycache 或 pytest cache。相对 T1 的 16 项为 15 改 1 新，527 源未变，0 删除。冻结源当前文件均只读；本轮开始没有单独记录冻结源初始 mode 清单，不能据此声称 mode 的前后逐项比较。内容前后逐 hash 是完整的。

reviewer 所在 checkout 一直为干净 detached `cefa1e375974d96e2ec25e199f6605f6dcf2239b`；这不是产品身份。本轮未读产品 live 或作者报告/接线图，未运行作者脚本作为独立证据。遵守仓库必读所读取的候选 KNOWLEDGE 及其 delta 含作者历史状态陈述，未将这些陈述当作通过依据；行为判定来自独立脚本、真实材料和 raw。

## 2. 已修/兑现的规格义务

1. **审批前、批准/grant 消费与 intent 前：已修。** 真实 effects、legacy v0/v1 × 低盘/未知 × modify_text/create_report 共 12 例。批准后降盘，修改类批准保持 approved，自动类不新增 grant；均无新 grant 消费、tool.intent、execution.started，文件保持原样。另有真实低盘 delete_file 在 prepare、审批请求前被拒绝。精确 ToolSpec/schema/action 身份先核；执行器原票据/claim/digest/权威记录核验保留，新增门不把 approved/admitted 当许可。`raw/final-positive.log`、`probes/test_independent.py::test_consume_gate`。
2. **六个实际执行边界：已修。** TaskStore.begin_execution、FileExecutor、记忆守卫、MemoryExecutor、network.fetch、MCP send boundary × 低盘/未知，共 12 例。全部先走合法 prepare/批准和真实 grant 消费、intent，再只在所选边界前切换同一 DiskAdmission 的最薄测量叶子。守卫级拒绝无 execution.started；执行器级可已有 started，但无实际写。实际文件保持 old，记忆无新行/正文，真实 loopback HTTP peer 无命中、真实 stdio MCP peer 无 tools/call。独立 exec 补例核真实 nested cwd 低盘不启动子进程，恢复恰好阈值后相同策略下真实命令写 marker 且 Receipt 可核。`raw/final-positive.log`、`test_execution_boundary`、`test_exec_real_nested_cwd`。源码落点为 dispatcher 的消费前/资源复核与 MCP send、tasks.begin_execution、FileExecutor/MemoryExecutor；不把一个上游拒绝替代六个下游证据。
3. **pure 与已有停止控制：已修。** effects/v0/v1/pure_only × 低盘/未知，共 8 例。真实 registry 的 subtask.status/result 经 Dispatcher 可读；错父和不存在目标都按原作用域拒绝。effects/legacy 对已有子任务 cancel 免空间，pure_only 非 pure cancel 拒为 run_effects_disallowed。核心 pause/cancel 在同组有效；独立低盘 revoke 保持原行为，delete 不因“腾空间”获豁免。erase/undo 没有新增例外分支，按真实非 pure spec 落通用门；本轮未另跑完整 erase/undo 矩阵。`test_pure_stop_identity`、`test_prepare_before_approval_and_delete_no_stop_exemption`。
4. **effects/真实 legacy 的恢复与普通/摘要模型：已修。** resume/reopen/pause/worker/daemon/direct _drive × effects/v0/v1 × 低盘/未知，共 36 例。独立离线 provider 每例真实完成 8 次普通模型及 3 次摘要调用，并核持久摘要行；原 run.start/版本/资格、Run ID、未知预算预占均保留，同 owner epoch 1→1，本 Run 未启动已配置的 MCP。legacy 用真实 legacy_write 历史 payload、明确历史 event_versions fixture 与 rebuild 读 None，未补造新 policy。该 fixture 修改仅用于播种旧版本，不是生产迁移或运行时更新事件。worker/daemon 使用真实驱动方法，未声称覆盖完整守护进程 IPC 或 UI。`raw/resume.log`，最终形状见 `raw/final-positive.log`、`test_resume.py`。
5. **审计失败、unknown、Receipt 与硬退：已修。** 一次合法文件写后核独立 Receipt/旧 manifest；下一动作低盘且拒绝审计 append 注入 errno.ENOSPC，真实异常向上暴露，批准/grant/旧事件前缀/Receipt/manifest 不变，文件停在 first。此证据是指定审计 append 失败，不是把整卷实际写满。MCP 三窗只用既有 TIANSHU_FAILPOINT：before_call、marked、after_call 子进程均原生 exit77；真实 peer 仅 after_call 有调用与执行标记。恢复分别保留 none_applied、unknown、unknown；连续两次恢复事件完全幂等，无伪 Receipt。低盘恢复普通模型后旧结果与 peer 标记不变，无重放；死 owner 按原接管规则 epoch 1→2，不能误称所有重开都不换 epoch。另有 pending MCP 低盘/未知拒绝、不发现/不消费，schema 变化发现后拒旧动作，正常同身份只发一次且 Receipt 可核。`raw/process.log`、`raw/supplement.log`、最终同形 raw；`test_process.py`、`test_pending_mcp_exact_identity`。
6. **真双进程、屏障与原始证据：已修。** 两条独立 parent/child 实进程链分别在审批后和合法消费后的 FileExecutor 前用 stdin/stdout 通信屏障推进。日志有不同 PID、批准/消费快照、命令、退出码和实际文件；不使用 sleep 猜时序，也不用同进程双连接替代跨进程。仅 existing crashpoint 执行 os._exit；独立脚本未另写硬退或 SIGKILL。`test_two_real_processes_barrier` 与 `process_child.py`。

## 3. 独立结果与负控计数

最终独立脚本组合为 **80 passed（11.01s），exit0**：边界/审批/pure/ENOSPC 33、恢复普通+摘要 36、双进程+硬退 5、exec/pending MCP/停止补例 6。唯一计数以 `raw/final-positive.log` 为准，不把前序通过次数相加。

阶段原件分别保留：phase1-repair 33P、resume 36P、process 5P、supplement 6P。前序 create_report 的 6 例曾使用 path 键；最终组合前已改为真实 schema 的 name 键，最终 80P 是该工具合法参数形状的证据。六边界 marker 断言也在最终组合前加强。`raw/final-positive.*.py` 保存最后 80P 使用的完整脚本版本；当前 probe 后续仅删去未调用的辅助函数，并为负控恢复观察加入明确异常记录；runner 增加 source/probe hash 记录，均不更改产品。

**9 个最小变异，10 个 call 阶段行为失败，0 setup/import/collection kill**：

- 删除消费前门：批准/grant 已消费且 intent 已提交，后级仍挡住文件；消费不变断言真红。
- 删除 TaskStore 门：低盘产生 execution.started，后续 FileExecutor 拒绝；边界标记断言真红，不冒称已有文件漏写。
- 删除 FileExecutor 门：真实文件 old→new、applied/Receipt，真红。
- 删除记忆守卫门：低盘产生 execution.started，后续 MemoryExecutor 拒绝；边界标记断言真红。
- 删除 MemoryExecutor 门：真实记忆写 applied，真红。
- 删除共享资源执行复核门：HTTP peer 收到 /proof、MCP peer 收到 tools/call，共两例真红。
- 把所有 Dispatcher 工具套空间门：真实 pure status 被低盘拒绝，真红。
- 提前豁免所有 cancel：pure_only 非 pure cancel 被错误准入，真红。
- 在 _drive 全面套新空间门：尚未普通/摘要模型调用即从产品 call 抛 DiskAdmissionError，真红；不是 setup/import 错误。

每个变异都有单独 patch/raw/前后源 hash，全部原字节复原。`raw/mutations-reviewed.json` 是逐项语义核验，`raw/mutations.json` 是执行记录。**复原控制 10 passed（3.36s）** 单列于 `raw/restored-controls-corrected.log`，不与 80 或 10 红相加。执行器/外发真红来自真实操作标记；守卫 marker、消费和恢复错误分别按其准确行为计。

## 4. 质量判定

- **变更范围与原安全门：通过。** 独立审读 16 项 delta；新增执行资格/空间门保留当前 ToolSpec 身份、父作用域、授权/审批、claim/fence、取消、预算和 intent/result/Receipt 顺序。pending MCP 只对既有绑定请求惰性发现，发现后重新核 schema/version；从已存在结果或未结 intent 先走旧结果/恢复分支，未新增未知重放路径。无新增运行依赖、数据库迁移或事件类型，契约文件原字节继承 T1。
- **封闭集与文档对账：通过。** AST 读取真实 RULE_IDS 与 schema enum，精确集合相同，共 78；三项资格/空间 rule_id 在当前 PRD 清单均存在。类属性 rule_id 是真实产生路径，不以 type ignore 压越界。见 `raw/schema-binding-static.json`。新 delta additions 未含 TODO、skip/only、NotImplemented、continue-on-error 等占位/掩红；未作全仓 lint 结论。
- **继承夹具与恢复质量：通过。** 旧测试变更只补同实例 context、显式 MCP 准备、summary memory 以及新 admitted 形参；旧硬退、unknown、对象阻断等断言没有删除或放松。schema 静态扫描增加固定 rule_id 类属性识别，与真实产生路径一致；不把该扫描本身当行为证据。
- **可追溯性：通过且限制明确。** 命令/cwd/env 名/exit、原文件/SQLite/peer、源 hash 和所有首红均保留；pytest 与所有产品子进程使用 Python 3.12.13，禁 pycache 和 cacheprovider。早期命令 JSON 没逐阶段记录全543 hash，不补造：其内容绑定以运行前完整 source-before、完整冻结副本、变异前后 hash 与最终复原检查支撑。新增精确阶段源/probe hash 从 restored-controls 命令开始存在，必须区分实际测量与后验绑定。Python3 3.14 仅处理文本、AST/hash/报告元数据，没有导入或运行产品。见 `raw/runtime.json`、`source-before.json`、`source-after.json`、`work-restored.json`。

## 5. 观察器错误、未执行与边界

首轮 `phase1.log` 为 **13 failed / 20 passed**：12 例网络白名单误用 http:// 导致建任务失败、1 例错误查询 receipts 表；它们没有到待验产品行为，不算产品红或 kill。修正到 network:// 与 receipt 事件后通过，原脚本 `raw/phase1.probe-v0.py`、修正版 `raw/phase1-repair.probe-v1.py` 保留。复原控制首次 argv 漏写 test_independent.py 路径，`restored-controls.log` exit4/no tests；不是 kill，改正 argv 后 10P。恢复元数据时路径定位曾漏 src/tianshu，属于准备阶段只读错误，旧 PREPARED_ONLY 原件保留。本轮人为中断后已核无遗留测试/peer进程，未盲重试已完成组。

没有自动重跑作者相同字节四文件联合，也没有最终16文件统一全绿结论。作者首联合510P/3F/1skip不是本报告最终通过证据，作者局部修后统计不与独立计数合并。未执行 full、lock、全仓ruff、T3、UI、原生CI、API、commit/push、真实模型/付费、soak；这些后续门禁仍由 root 安排。

继承 T1 **OBSERVED_LOCK_HELD_DURING_CONTROLLED_LEAF**：只证明受控 leaf 等待期间持锁，未证明原生 disk_usage 时限、死锁消除或 SLO；OS/文件系统阻塞仍无已证明上界。本片没有新建 timeout 框架或改变 guard→transaction 顺序。最薄 probe 的同临时卷路径选择与真实 nested cwd 文件操作，不是原生不同卷/嵌套挂载证据；未验证 Windows/Linux 或其他设备。阈值不是容量预留，不能承诺命令/远端 MCP 全部外部写位置永不满盘。纯读/模型仍依赖原审计可写、预算、取消和能力门。

**正式交还：本片规格与质量均在上述冻结/范围内通过，已确认阻断0。报告、facts 和全目录 hash-index 封存后停止，等待 root 亲读核验；本报告不授予后续实施权。**
