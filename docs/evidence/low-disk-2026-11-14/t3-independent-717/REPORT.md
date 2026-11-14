# T3 独立规格与质量验收

日期：2026-11-14（Asia/Shanghai）。独立 reviewer：`/root/review_admission_contract`。

**规格结论：PASS_T3_FROZEN_ENTRYPOINT_SCOPE。质量结论：PASS_T3_FROZEN_NARROW_DELTA。当前范围确认的行为阻断：0。**

结论仅绑定下述 717 项完整冻结及精确继承的 T1/T2 证据，不是最终提交、全量门、原生 CI、合并或用户设备支持结论。root 后续事实文档/证据元数据的小 delta 不在本冻结内，须另作最终对象绑定。本 reviewer 没有审读作者报告正文或自查结论，也没有将作者测试数作为独立证据。

## 1. 精确身份与操作边界

- 冻结源：`/tmp/tianshu-g3-20261113/low-disk-t3-root-mwb0fwy0/candidate-complete`。
- root 源映射：其父目录 `source-hashes.json`，717 项，SHA256 `7acc68435efe4e926dcf303b36f0a89ae0ec478d17c3f5e8a4c80e3a05939db6`。
- 本报告目录：`/tmp/low-disk-t3-independent-20261114`；所有产品运行在本目录 `work/` 副本。初始、全部变异复原后及封存前的 717 项实际源字节都与上述映射相同。`source-before.json`、`source-after.json` 与 `inputs/source-hashes.json` 的 SHA256 均为上述值。
- 相对已审 T2 的 543 项映射：524 项未变、17 项修改、176 项新增、2 项删除。除 CLI/control 外，原有 71 个运行模块、133 个测试/fixture 文件、7 个 schema 共 211 项全部原字节不变。新增项包括定向测试、dist 资产及证据材料；不把映射条目数误称纯源码数量。
- reviewer checkout 为干净 detached `cefa1e375974d96e2ec25e199f6605f6dcf2239b`；历史 HEAD 不是包含 dirty 候选的产品提交身份。未写该 checkout、产品 live、refs、旧冻结/证据目录，未 commit/push，未 spawn。
- 本 lane 没有执行 full/lock/全仓 ruff/完整 UI/install/build/API/付费/soak。没有运行作者原定向套件，也没有重跑 T1/T2 全量。

需求副本、root 约束转述、root 冻结元数据与原补丁保存在 `inputs/`，精确文件身份见 `inputs.json`。`preparation-PREPARATION.md` 只是先前 PREPARED_ONLY 历史准备稿；本报告才记录正式授权后的结果。

## 2. 独立运行方法与环境

实际环境为 macOS 26.6.2 arm64、Python 3.12.13、Node v24.22.2。React/ReactDOM 19.3.1、Vitest 3.2.7、jsdom 26.1.0。详细版本、解释器与 reviewer checkout 在 `raw/environment.json`。

Python 只在 `disk_admission.shutil.disk_usage` 最薄叶子注入可用空间/异常，并以实际 `ScriptedModel` 子类记录调用再执行其实现。真实 CLI parser/main、两条创建路径、prepare/创建/事件层均保留。IPC 使用真实 `daemon.serve`、认证/hello、`ipc.Client`、ControlPlane 及实际 driver，不以 EchoServer 代替产品业务。离线模型实际执行 `read_text` 与第二轮对话，没有真实模型 key 或付费调用。子进程模型调用、磁盘探测、命令、stdout/stderr 与数据库材料保存在 `runs/i-*/`。

UI 用单文件 jsdom + React `act/createRoot` 挂载真实 App、NewTaskDialog、TaskDetail；只替换 HostClient factory 边界，捕获实际 `host.rpc` method、完整 params 与顺序。它证明 App 交互接线，后端证明另由真实 CLI/IPC 提供。没有 TestingLibrary/react-test-renderer，没有用 selector/helper/demo 代替 App DOM。

所有 Python 命令设置 `PYTHONDONTWRITEBYTECODE=1`，pytest 禁用 cacheprovider 并清空项目 addopts。Vite 缓存在本输出 `runs/vite-cache`。只复用了现有 node_modules 的依赖库，产品导入均指向本工作副本。运行完成后记录并移除了两个依赖 symlink；重现前可按 `raw/dependency-links.json` 重建。没有对外部依赖 chmod。Node 20.19 没有实际执行；这里只核其 engine 静态兼容，不能冒称该版本验证通过。

## 3. 正控与独立覆盖

结果按实际命令分列，不虚构一次统一的 56P 命令：

- 首轮 Python：**50 passed，22.74s，exit 0**。原命令与源/探针 hash 在 `raw/python-first.command.json`，raw 在 `raw/python-first.log`；当时探针原字节另存 `raw/python-first.test_entry_independent.py`。
- paused 补充：**2 passed、50 deselected，0.96s，exit 0**。见 `raw/paused-supplement.{command.json,log,junit.xml}`。
- 完整独立 App DOM：**4 passed、0 skipped，exit 0**。见 `raw/ui-first.{command.json,log}`。
- 本次独立 unique 为 **52 Python + 4 App**。不叠加 T1/T2 或作者通过数。
- 变异复原正控另列：Python **5 passed、47 deselected，exit 0**；App **2 passed、2 个名称过滤未执行（Vitest 显示 skipped），exit 0**。见 `raw/restored-controls.*`、`raw/ui-restored-controls.*`；不再计入 unique。

实际覆盖如下：

1. 真 CLI `run start` 的默认 effects，对 `MIN−1/MIN/MIN+1/0/bool/负数/探测异常` 七值逐一验证。两个创建入口均显式 effects/pure_only；兼容入口另有默认低盘拒绝。低/未知 effects 在新 Run 创建前拒绝，pure_only 在低盘可实际读取和对话。阈值等于 `2147483648` 只证明空间这一条件通过。
2. 真 stdio MCP peer 启动标记：新 effects 低盘在新 peer 启动前拒绝；pure_only 两入口不为本 Run 新启动 peer；正常 effects 正控实际观测到了新 peer。不能由此推出隔离或既有常驻进程不存在。
3. readonly workspace 与 effects/pure_only、writable workspace 与 pure_only 的交叉事实不互改。Run 策略写在真实 `run.start` payload，事件版本为 v2；pure 清单逐项使用真实 registry 的 ToolSpec 校验。重开实际事件库读回同一持久策略，pure 每 Run manifest 为零。
4. resume/status/pause/cancel × 两种显式 mode 均 exit 2，原事件、Run/Job/资格快照不变。补充真 paused Run 的双向反模式 flag，均拒绝；去掉 flag 后，同一原 Run 在低盘继续实际 pure 读取与两轮模型，原 Run ID 和 `run.start` 不变。
5. 真 IPC 缺省策略同样覆盖七值。允许者返回 created，实际 driver 随后完成；低/未知保留现有 `internal` code 且 rule 原因可区分，没有另造 RPC 错误契约。
6. 真 IPC 的 14 类坏 policy 均 `bad_params`：null、两个 bool、字符串、数组、空对象、缺任一键、bool/float/未来版本、bool/未知 mode、额外字段。另传 `mode:effects` 不能给坏 execution_policy 兜底。两种合法显式策略均经过实际创建与持久层。
7. App 新建默认 effects 与 readonly 目录独立，`task.create` 无 policy，后续 `run.start` 精确携带 V1；新建显式 pure 的低盘失败在 dialog 关闭后保留选择；同 Task 重试不降级、不自动重试，另一 Task 使用自身 default，切回原 Task 仍保留；只有显式改模式才改变请求。既有 TaskDetail 路径同样覆盖默认、显式 pure、unknown 失败重试与跨 Task 隔离。

首轮已有的 **27 个拒绝案例**另做只读持久材料核对（不是新增 pytest 通过数）：8 CLI、5 IPC low/unknown、14 IPC bad。全部 `runs/jobs/leases/approvals/grants/dispatch_requests/budget_reservations` 为 0，无 `run.start/model.intent/approval.requested/tool.intent/execution.started`，无模型日志或每 Run manifest。数据库均以 sqlite URI `mode=ro` 打开。见 `raw/retained-rejection-audit.json`。这些案例可已有 Task/初始化/审计事实，绝不声称整库零写。

## 4. 负控、复原与观察器修正

本副本逐项实施 **11 个最小变异**，每项恰好产生 **1 个 call 阶段行为失败**，合计 11 红；零 setup/import/collection kill。每项保留 `mutations/<label>/change.patch`、`record.json`、runner stdout/stderr，完整命令/raw 在 `raw/mutant-<label>.*`。聚合为 `raw/mutation-results.json`。

变异分别为：CLI start 传参、CLI compat 传参、CLI 非创建 flag guard、RPC prepare policy、RPC persist policy、RPC manifest policy、RPC truthiness 默认化、App 创建 policy、App 详情 policy、dialog 关闭后重试 seed、taskId 隔离。前七项经真 CLI/IPC 观察失败，后四项经真实 App DOM 的实际 RPC 参数/状态断言失败。

每次 `finally` 恢复原字节并比较全部 717 项。全部变异结束及复原正控后，完整源映射仍相同。不是用语法、导入或测试框架错误冒充行为红。

观察器问题均保留原件并明确分类：

- UI 负控最初 metadata 错写 skipped=0；raw 明确是 1 failed + 3 skipped，其中三个由 `-t` 过滤，非 authored skip。已修正四个 record 与聚合为 `filtered_unexecuted=3/authored_skips=0`，保留原 metadata 于 `raw/metadata-before-filter-correction/`，执行时原 runner 于 `raw/run_mutations-before-filter-correction.py`，说明见 `raw/filter-metadata-correction.json`。该修正不增加 kill，也不是产品失败。
- 封存观察器首次把仅运行模块 71 项误套成源/测试/schema 合计 211 项，断言失败；随后按明确集合重核 71+133+7=211。错误与修正见 `raw/finalize-observer-first-error.json`。这是后验材料观察器错误，不影响上述产品测试数，不当作负控证据。

## 5. 质量与文档对账

窄范围独立审读确认：CLI 仅添加严格选择、非创建 flag 拒绝和两条创建传参；ControlPlane 严格 `.of`、缺省 effects，prepare/manifest/start_run 同一策略；App 以 taskId 保存最小 transient 选择，不改写 Task/workspace 权限。继承内核没有新增授权、租约、并发写、事件或计费旁路。本 lane 不以能力声明代替授权，不改变原策略或三态。

pyproject 与 uv.lock 不变；运行库没有扩张；UI 只增加 jsdom devDependency 及其 lock。demo 静态一致性仅作辅助，不计后端证据。新增/修改的 src/tests/ui/src 代码新增行未发现 TODO、NotImplemented、skip/only 或 continue-on-error 占位，具体文件集合与 scan 在 `raw/source-and-quality-audit.json`。README 易漂移的 49/47 计数已删除。

非行为文档时点提醒：本冻结契约顶层明确区分历史契约阶段和当前交付，但 §5.1 末仍有“当前可写清单仍为75项”的历史句，宜进一步标明契约阶段时点。已交 root，root 承接最终事实文档小 delta；本报告没有把其后 live 修订冒称已包含在717项中，也没有放宽契约正文。最终独立通过状态、公开证据引用/计数与最终提交必须按新对象另绑定。

## 6. LD 映射与精确继承

继承绑定 `inputs/inherited-evidence-binding.json` 的 SHA256 为 `3726f2fdd46e70addf113b48ec9b65f70b5a1c4f90e2ccd0aaf92859cf40dba7`。其中八项文件在结束时再次逐 hash/size 相符，见 `raw/inherited-input-recheck.json`。

- **LD-01 → T1/T2**：继承真实分类、审批消费、执行边界、父作用域、停止控制与负控；T3 只确认入口选择不扩大权限。
- **LD-02 → T1/T3**：继承 T1 创建准入与持久资格；本轮新增真 CLI/IPC 默认/显式策略、七值、创建前拒绝、真实 App 可达路径。
- **LD-03 → T1/T2/T3**：继承直接创建、子 Run、控制 Run、Dispatcher/执行边界；本轮补两创建入口、RPC prepare/persist/manifest 与真实 App 两路径，以及相应接线负控。
- **LD-04 → T1**：继承已审多位置/authority/独立 guard、路径/阈值及原限制。本轮未重做原生不同卷调查，受控 leaf 不等于真实跨卷或嵌套挂载。
- **LD-05 → T2**：继承 effects/真实 v0/v1 × 低/未知 × 六恢复路径、普通/摘要模型、停止/审批与原资格；本轮只补 CLI flag 不得升级和同原 Run 的最小接线回归，没有新增全面空间门。
- **LD-06 → T1/T2**：继承事件版本/schema/replay/rebuild、真实双进程/屏障/硬退与审计证据；本轮仅核新入口持久同一 V1 资格与 v2 run.start。

T1 round1 独立 REPORT：`/tmp/tianshu-g3-20261113/low-disk-t1-independent-round1-20261114/REPORT.md`，SHA256 `a2ec4da045b9a862744198a5cd002910a145458f9dd4a924a615b0839b7688e0`；其 T1 源映射 `ce2a50155505e3915abf90de6be26e40ba6019e0c9f0b495db9c58df5d96e6f4`。继承 round1 的窄 C1/C2、六文件 delta 与原 v1 接管 guard 范围，原 v1/负控原件索引仍按该独立报告继承，不宣称本轮重新执行。

T2 独立目录 `/tmp/low-disk-t2-independent-20261114`：REPORT `b41941026f996d45293d672c180747d2c05735771bcb5235d42aaa1e54bd5e72`，facts `8e7b2c12ade690f81e5d00a7768bc3ce91204115c0a67f4affb993e3cf9c35e1`，index `a5a7c114d417c4753daa41efa60719fbb85fce5e7ef960cf9bf0f4d3a0464ba0`；T2 源映射 `589c6d3a5b7e2a1b2d2c3b47eb3f646aa035ddd03f32cc84e824a0e0b46a4c1a`。其80P、9变异/10行为红留在T2旧冻结范围，不计入本次52+4或11红。

## 7. 限制、后置门与封存

保留 `OBSERVED_LOCK_HELD_DURING_CONTROLLED_LEAF`：只观察到受控 leaf 等待时持锁；没有证明原生 disk_usage 的时间上界、消除死锁或 SLO。OS/文件系统阻塞未获上界。同临时卷 leaf 注入不是原生不同卷/嵌套挂载，也不是 Windows/Linux 用户设备证据。阈值是准入条件，不是容量预留。pure_only 仍可能有必要审计写和模型网络/计费，不承诺零磁盘、零联网或免费。

本 lane 的以下项目均 **NOT_RUN/NOT_GRANTED**：最终 lock/full/全仓 ruff、完整 UI frozen install/typecheck/test/build 与 dist 重建一致性、正式提交/PR、最新 main 精确同步、四 required 原生 CI及Windows两分片/汇总、保护合并与新 main 自身 CI。它们由 root 另行执行，不能用此独立候选结论替代。未做真实付费模型、10h soak、恢复 apply、容量 GC、发行或其他平台设备验证。

封存前记录的所有 child/peer PID 均已退出，work 无额外源文件，无 pycache/pytest cache，见 `raw/process-and-extras-audit.json`。文件设0444、目录0555；`hash-index.json` 覆盖本目录全部文件且排除自身，自身 SHA256另回 root。依赖 symlink 已移除并保留重建说明。封存后本 reviewer 停止写入，等待 root 对最终对象的另行绑定授权。
