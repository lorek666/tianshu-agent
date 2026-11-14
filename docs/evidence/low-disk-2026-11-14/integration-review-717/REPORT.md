# 低磁盘整分支独立集成复审：717 固定候选

日期：2026-11-14（Asia/Shanghai）  
第二读者：`/root/review_low_disk_integration`  
**结论：`NOT_APPROVED_PUBLIC_EVIDENCE_CLOSURE_REQUIRED`。** 本范围未发现新增产品行为阻断；**公开证据封装阻断 1 项（INT-E1，未修）**。不预先给 PASS，不批准整功能交付。711/717 身份说明与基线 CLI 预算缺口分别列为非阻断事实项。全量、lock、全仓 ruff、完整 UI、正式提交及原生 CI 不在本轮执行范围，仍待 root 核验。

## 1. 输入、身份与操作边界

唯一审查候选为 `/tmp/tianshu-g3-20261113/low-disk-t3-root-mwb0fwy0/candidate-complete`，完整 717 文件清单 SHA256：

`7acc68435efe4e926dcf303b36f0a89ae0ec478d17c3f5e8a4c80e3a05939db6`。

基线 Git 对象是 `a0223aa269efcdf70df4c4d8c43e5629b4fa0215`；该 HEAD 不能代表未提交候选。候选 717/717 文件实际 hash 匹配、无额外文件。独立保留的 `source/` 也是相同 717 文件，产品导入仅来自本目录的只读副本。候选不是 Git checkout；启动时的 `git status` 因无 `.git` 返回 128，未据此转读 live 产品源码。仅为核实预算观察，后按 root 明确指令读取上述固定 Git 对象的 `cli.py`、`loop.py`，保存在 `inputs/git-a022/`；没有读取 live 产品源码。

已先读指定 task-1 Global Constraints、三份 brief、task-3-context，并保存精确输入；progress 只读首行及 ruling/parked/deferred 行。计划身份为 `/tmp/tianshu-g3-20261113/low-disk-implementation-plan-v2-20261114-feqkue8h/PLAN.md`。权威规格取冻结契约、PRD §5/§6，流程取冻结 CONTRIBUTING。KNOWLEDGE 与 LOW-DISK 中作者历史陈述仅用于文档对账，不作为独立通过证据；未读作者自查报告。

继承绑定 SHA256 为 `3726f2fdd46e70addf113b48ec9b65f70b5a1c4f90e2ccd0aaf92859cf40dba7`，其中 8 个原始身份全部匹配。另逐 hash 验证原 T1 index 的 **579** 个文件和原 T2 index 的 **1403** 个文件，零差异；候选公开保留的两轮 report/facts/index 共六件与原件逐字节相同。原 T1 15/13/3 项 raw、T2 最终 80 passed 与变异审阅原件的选定副本保存在 `inputs/inherited-selected/`，其余不改旧档案。

本轮不重跑 T1/T2 套件，也不复制另一 reviewer 的 CLI/IPC/App 行为矩阵或变异。没有 full、lock、全仓 ruff、完整 UI、API、refs 写入、commit、push、付费模型、soak 或产品修改。

## 2. INT-E1：公开索引缺三份静态源副本——P2，未修，阻断证据批准

精确源锚：`source/docs/evidence/low-disk-2026-11-14/artifact-index.json:833–847`；说明来源为 `source/docs/LOW-DISK-2026-11-14.md:36–41`。

公开索引自身 SHA256 为 `42a9fb2d0d622d9883d4181f1c472d359121c9224de260b8d4cebb5d31cc65dc`，声明 173 项，但以下三项不在完整 717 候选内：

- `t3/visual-static-demo/dist/assets/index-CIZ6Bgii.js`，预期 SHA256 `fe672593496fdb8228021e3457107e032569e903206361c1338f0050e010e533`。
- `t3/visual-static-demo/dist/assets/index-XPu6LbkB.css`，预期 SHA256 `799a089898524ac56f7d6845cccd07ed971161ce87f070237ec69f295e664225`。
- `t3/visual-static-demo/dist/index.html`，预期 SHA256 `52420ffb1ff112f4c19458e9c45e12effb3ceed2d553e565f6c4c137322c632e`。

其余 170 项 hash 匹配；证据目录实有 171 普通文件（包括 index），不是索引暗示的 174 件。三份对应内容在产品 `ui/dist` 中存在且 hash 相同，故不是已证明的产品产物丢失，也不推翻原视觉截图；它是公开可提交证据集合的缺件，不能称当前索引闭合。

复现命令：

```sh
python3 -B /tmp/low-disk-whole-branch-review-20261114/probes/integration_audit.py /tmp/tianshu-g3-20261113/low-disk-t3-root-mwb0fwy0/candidate-complete
```

实际最终输出 `PUBLIC_EVIDENCE_INDEX_INCOMPLETE`、exit **1**；原始详细对象在 `logs/public-artifact-index-mismatch.json`。首版审计在同一缺件断言 exit1，原脚本与输出另存 `integration_audit_first_failure.py`、`integration-audit-first-failure.*`，没有把首红覆盖为绿。中途一版观察器误把中文全角冒号写成英文冒号，字符串定位 ValueError；单列 `integration-audit-observer-colon.*`，不计产品缺陷或负控。

root 已接收事实并计划以最窄 `.gitignore` 例外将原三份字节纳入公开集合；截至本 717 报告尚无新完整冻结输入，因此仍是**未修**。销账须另核新增三份实际字节、旧索引不改、完整新候选身份及相对本报告的精确 delta。无须因此重跑产品，但不能先给 PASS。

## 3. 已修义务的继承与跨片接线：限定内容通过，未新增行为背书

**T1 C1/C2/Q1：已修结论被保留。** T2 至 T3 的 211 个 `src/tianshu/`、`schemas/`、`tests/` 路径（排除两个新入口）逐字节相同，包括控制 Run 事务原子性、run.start v2 严格写读语义和 takeover 守卫夹具。T3 对两个入口的逐行审读未改变 `Dispatcher.open_control_run`、TaskStore 的最终检查或执行守卫。原 T1 的 15/13/3 项 raw 与原索引核对一致；不把它们重新计为本轮新测试。

**授权、租约、晚检查与并发写：既有修复被保留，未见新入口旁路。** `control.py:433–468` 严格解析策略，先查真实 Task，再 `prepare_run`，再把同一对象传入 `start_run`，最后入队。`cli.py:541–569,574–610` 的两条创建入口均将可信参数转换为策略并调用原 AgentLoop；非创建 action 携显式 mode 在 `_authority_stack` 前返回2。`tasks.py:1016–1044` 仍在原事务中重读绑定、检查 active Run、做最终当前空间门并统一插入。`dispatcher.py:819–827,1010–1067` 仍是原身份/目标→资格/空间→批准/grant 消费→intent 的顺序；`1221–1239` 的停止例外仍在 pure_only 非 pure 资格门之后。`tasks.py:1228–1254` 的 guard、current、租约、真实目标空间和 started 顺序未改。原真双进程、审批与实际边界证据仅按精确未变源继承；本轮 AST 顺序检查不是新的并发行为证明。

**事件 append-only、版本与 unknown：既有结论被保留。** T3 未改 EventLog、event_schema、TaskStore 插入/重读、恢复、File/MemoryExecutor、network/MCP 发送边界或 intent/result/Receipt 代码。真实导入的45种事件只有 run.start 当前版本为2，其余为1；真实 rules/schema 集合仍精确相同，共78项。`tasks.py:1124–1130` 仍由统一层覆盖 payload 同名策略，`1047–1057` 仍只对合法历史0/1返回None。`loop.py:938–950` 不以缺 manifest 洗白已有 intent；旧结果/未结 intent 的恢复分支未被 T3 重写。原 T2 的硬退、unknown、Receipt 与负控原件身份未变；不把继承扩大为物理断电、真实整卷 ENOSPC 或新的重放保证。

**纯读清单、默认 effects 与模式≠权限：接线相符。** 实际 registry 有23项，pure仅7项；`control._exposed_tool_ids(PURE_ONLY)` 与真实 ToolSpec及 `tool_manifest()` 的 pure 交集相同：list_files/read_text/read_content/memory.search/memory.get/subtask.status/subtask.result。清单过滤不会授予作用域或写权限，下游仍核真实身份。UI `App.tsx:507–551,569–608,908–919` 的 task.create 只发 goal/workspaces，策略只传 run.start；失败重试用 taskId 关联临时状态，不承诺重载持久化。NewTaskDialog 与 TaskDetail 的独立选择器不改绑定的 readonly/writable。真实 DOM/RPC 参数与重试行为属于另一 reviewer，本报告不替其给 PASS。

**错误处理：未引入新错误契约或放行。** RPC坏对象明确转 `bad_params`；其余沿原 ControlError 路径。UI errors.ts 仅展示已有 internal 中带磁盘 rule_id 前缀的原因，没有修改授权、降级模式或自动重试。CLI默认 None 经 helper映为EFFECTS；只有 argparse封闭值 pure_only 选纯读，不按 goal/workspace猜模式。

独立结构命令为 Python3.12 `-B probes/structural_wiring.py`，实际 exit0，详见 `logs/structural-wiring.command.json/.stdout.txt/.json`。新增产品差异未见 TODO/skip/only/NotImplementedError/continue-on-error/type-ignore；新增两测试文件只做静态扫描未执行。Python运行依赖与 uv.lock 字节继承T2，UI只新增获准 jsdom devDependency；没有据此声称 lock 或构建已通过。

## 4. BASE-B1：CLI预算账本接线缺口——P2，基线既有，非本片阻断，未修

精确源锚：`source/src/tianshu/cli.py:440–456`、`source/src/tianshu/loop.py:674,689–690,1220–1245`。

不是仅凭“T2已有”推断：固定 a022 Git对象、冻结T2、冻结T3三方独立比对确认，三者 `_engine` 都没有传 `budget=stack.budget`；a022 与T2/T3的这段差异只有后来补的 contents。T2/T3 `_engine` 函数 hash 同为 `d6d315fd42487592edc72e05e9173bc97341abae9d8cf1c3ac84424fa518d68d`；AgentLoop构造器、`_ensure_budget_account` 和 `_reserve_attempt` 在三方分别逐字节相同。没有找到 CLI其他装配把该engine的budget再补上。

最小语义探针 `probes/budget_assembly.py` 用真实 `cli._engine`/build_stack，在自有数据根运行一次离线 ScriptedModel pure_only周期：stack.budget存在、engine._budget为None，真实写入 model.intent/model.result并成功结束，而 budget_accounts=0、budget_reservations=0；`_reserve_attempt`直接返回None。另外从封存a022对象只编译原 `_engine` 函数，用同一真实构造器核实其_budget也为None。没有调用真实模型或复制CLI入口矩阵。

实际命令、运行版本3.12.13、源hash、原始事件类型和结果在 `logs/budget-assembly.command.json/.stdout.txt/.stderr.txt`；三方对象/函数hash见 `logs/budget-assembly-three-way-reproduced.json` 与 `probes/three_way_source_audit.py`。该项不是预算“已闭合”的证据，恰恰限制本报告：**其他入口的预算保持证据不得外推 CLI**。root明确决定本片不顺手修预算，而在公开限制/后续工作中记录；按基线既有、T3未扩大覆盖的非阻断缺口列账，不声称已修。

## 5. 文档、身份说明与公开可复现范围

**契约语义兼容：已核对。** T2/T3契约§1–§5与§6六条LD正文逐字节相同；新增部分只把“契约阶段全部NOT_RUN”区分为历史时点，并链接当前分项状态。PRD变化同样是既定实现状态与编号登记对账，未夹带新权限、枚举含义、错误状态或放宽验收。不因保留历史NOT_RUN而错判当前失败，也不因新状态文字而批准尚未跑的门禁。

**DOC-N1：711/717区别——P3，未修，非阻断说明项。** `t3/t3-source-summary.json:6` 为711；该作者map排除6份派生metadata：artifact-index、freeze-postcheck、verify_freeze、t3-delta、t3-source-hashes、t3-source-summary。完整717清单包含它们，全部源map项与完整map一致。当前LOW-DISK:126–128称其为累计源映射但未明说该排除口径；root已决定最终公开事实补充解释，不因此重跑产品。**这6份派生排除与INT-E1的3份缺件是两件不同的事**。

**公开引用：限定核对。** LOW-DISK/契约10个本地Markdown目标存在；公开六份继承身份副本与原件相同。公开继承目录仅有report/facts/index身份副本，原T1/T2独立探针与完整raw并未全部随公开目录发布；本读者是在保留的原始封存目录中核对它们，不能把“稳定身份副本”说成“公开全档案可独立复跑”。T3在库测试可按其实际命令另行复跑；本报告只确认已读命令/原件与源继承，不冒充再次运行。INT-E1仍是当前可复现性明确失败项。

## 6. 封存、限制与交还

所有实际证据和保留输入列入 `hash-index.json`（不含自身），`facts.json`记录范围、发现与报告hash。文件0444、目录0555。source副本与原717候选末次核对仍一致，无pycache/pytest cache新增；最小模型探针数据在本目录 `data/`，不进入候选或产品。旧冻结与旧证据均未改写。

继承 **OBSERVED_LOCK_HELD_DURING_CONTROLLED_LEAF**：本片不改变guard→transaction顺序，也不把受控leaf等待证据升级为原生disk_usage有界、无死锁或SLO。原生跨卷/嵌套挂载、文件系统长阻塞、用户设备/其他OS、真实付费、10h、restore apply、容量/GC、真实整卷ENOSPC和发布仍按原未验证范围保留，不作为新放宽的通过项。

**交还：当前717候选公开证据阻断INT-E1仍未修；不予PASS。** 已审源接线和未变内核未发现新增功能阻断；根任务须在最终公开小delta上独立销账，再分别绑定正式提交、最终组合门禁与原生CI，不能把本报告或另一路T3行为报告直接当作整功能交付。
