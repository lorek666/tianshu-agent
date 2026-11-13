# 数据准入 T1 · 本地候选验收

本片基线为 `30b973dfe21ce71f40ccfd7e84412de33d23877a`，契约为独立提交 `ccade5edbe47a66c0a48ad09e0667ee7393c148b` 的 [数据准入契约](data-admission-contract.md)。实施计划 SHA256 为 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`。作者为 `executor_data_cli`；本文区分作者本机验证与第二读者有限复核；原生 CI 与发布另行验收。

T1 将只读检查接入每个真实 `EventLog` 连接：共享维护准入取得后，SQLite 打开空主库前排除孤立旁文件和历史材料；打开真实连接后核对实际文件、事件版本旁表、已登记 payload/记忆版本和迁移进度，通过才创建 payload 目录、设置 WAL/FULL 与执行 DDL。既有损坏事件库的拒绝逻辑保留，错误路径关闭连接并释放维护准入。

正常 v0 不改 `user_version`，缺单行 payload 版本仍按既有 v0 映射；缺整个旁表拒绝，关闭后重开也拒绝。内存库不读取工作目录进度。自定义 DB 读取自身新 sidecar，不借另一个根的 v2 库或标准 DB 的旧进度放行。新格式2状态有严格字段、数值、路径、摘要算法与连续步骤校验；编码函数仅返回字节，不执行迁移或落盘。

实际 v2 普通打开要求至少一份位置/格式正确且严格合法的 completed。标准 `events.db` 旧位置的 format1 completed 只保留普通连接兼容；新 sidecar 只接受 format2，旧位置只接受 format1。双 completed 分别验证同一实际库、完整步骤与 v2 机制对象，不要求历史时间/旧备份路径相同。任何 in_progress、矛盾/损坏记录、未来版本均在事件 DDL 和 payload 目录之前拒绝。completed 的日常打开不重算历史业务基线，后续合法写入不会被旧摘要锁死。

本片没有修改迁移协调器或 `data_cli`，没有交付绑定备份的迁移、迁移 CLI、restore apply、恢复隔离、容量保护或 GC；不能称 G3 完成。

## 原红与新绿

- 首次原实现测试日志：`/tmp/t1-admission/red-admission.log` 为 41 failed / 12 passed；增加状态解析与机制检查后，`red-all.log` 为 97 failed / 12 passed。其中包含新增模块/API 缺失，不能全算行为负控。
- 114 项测试曾用未改变的基线 Git `src` 重演，并冻结测试、历史 fixture、基线源码及导入位置于 `/tmp/t1-admission/red-original/`：102 failed / 12 passed，其中 47 项实际“未拒绝”、1 项真实 CLI 退出码断言、53 项新 API 模块缺失、1 项新模块文件缺失。增加空目录骨架回归后的 126 项另在 `/tmp/t1-admission/red-final-126/` 重演：108 failed / 18 passed，0.75s；其中 53 项实际“未拒绝”、1 项真实 CLI 断言、53 项新模块缺失、1 项新模块文件缺失。两个目录均明确标注为后续重演，不冒充最初 RED 原件，也未包含下述独立复审后增强的重复字段测试。
- 对机制隐藏生成列、DESC 索引和非 BINARY 比较规则，新增真实 SQL 负控先取得 3 项未拒绝失败，再用完整列/索引元数据检查修复，原件为 `red-shape.log`。
- 首轮完整套件保留原环境：3 failed / 2527 passed / 8 skipped，426.24s（`full-first.log`）。其中新增 `.read_text` 违反既有字节 I/O 门禁；另把 `SnapshotStore` 先创建的空 `artifacts/blobs`、`artifacts/snapshots` 目录误判为历史。新 12 项空目录树/嵌套文件回归与原 Windows 门禁先得 7 failed / 6 passed，再将进度读取改为字节解码，并递归区分空目录骨架和材料；零字节文件、链接及不可判定条目仍拒绝，旧恢复测试不改。
- 首轮价格测试失败来自本机端点配置：精确基线的两个原节点在原环境为 1 failed / 1 passed，过滤配置后 2 passed；候选修复前过滤后仅恢复节点失败。原件为 `full-failure-controls.log`，导入位置为 `control-source-paths.log`。价格产品与旧测试均未修改。
- 完整套件运行前的关联定向底层命令：`uv run --frozen pytest -o addopts='' -q tests/test_data_state.py tests/test_data_admission.py tests/test_events.py tests/test_maintenance.py tests/test_memory_store.py tests/test_windows_compat.py tests/test_failclosed.py` → 299 passed，3.01s。由既有 `run_clean.py` 在子进程局部去除 `TIANSHU_*`、以 `BASE_URL` 或 `API_KEY` 结尾的环境项；不打印配置值。覆盖固定旧 SQL/旧状态、未来/畸形版本、双状态/错误位置、v2 无记录、孤立 WAL/journal、旁表重开、实际自定义 DB 的普通 CLI/重开拒绝、live WAL 读取、后续业务写入、锁释放、空骨架兼容及 AST/独立子进程导入边界。
- 完整套件在上述子进程局部配置隔离下，底层命令 `uv run --frozen pytest` → **2542 passed / 8 skipped，424.60s，exit 0**，原件 `full-final-clean.log`。完整测试期间源码、126 项新测试和 fixture 九文件与 `freeze-source.json` 均无漂移；对应测试源码保存为 `test_data_state-at-green-full.py`，完整结果和原始摘要绑定于 `full-final-clean-boundary.json`。
- 独立第二读者指出，原重复字段测试同时缺必填字段，无法单独证明 hook 生效。随后只增强 `tests/test_data_state.py`：完整合法状态落盘正控、顶层和嵌套重复字段两项负控；新测试总数为 127。临时完整源码副本移除唯一 `object_pairs_hook` 后，两项均 `DID NOT RAISE`（`red-duplicate-hook-mutant.log`）；当前产品关联定向 **300 passed，4.46s**。三个产品文件与 fixture 相对已绿全量均不变，仅该测试 SHA256 从 `41a7f00d24f38b07199fb705d39c686301281b9a9a9f493f7bddb199188028ec` 改为 `e5826e76e6738b716344f773148552554b14b816f5b5e44b508becbe1f3e748c`；最终清单为 `freeze-source-final.json`。这项测试增强未包含在旧全量中，后续整合最新 main 的新组合须重新全量，不能沿用本轮结果。
- `uv lock --check` 与 `uv run --frozen ruff check .` 均 exit 0；作者未提交或推送。

历史夹具的来源见 [fixture 说明](../tests/fixtures/data_admission/README.md)。最初 CLI 探针曾写错 `replay` 参数，被 argparse 拒绝；该输出不作行为证据。正式测试已改用真实 `--run-id`，基线重演与候选均执行同一入口。

## 独立复核与边界

最终源/测试/文档哈希及命令原件由父会话接收，另由第二读者复核。作者未提交、推送或自批。T2/T3 仍待单独授权与 RED/GREEN；当前纯状态类型中为它们保留的绑定字段不证明完整一致点算法已实现。维护准入仍是协作锁，不能抵抗旧客户端、raw SQLite 或同 uid 恶意写入；本机结果不证明 Windows/ARM64 或安装体验。

## main 组合首轮失败与夹具适配（历史过程）

父会话保全作者交接后从30b快进9129095，原样恢复最终九项已审内容；
共享代码包含main #133的实际恢复修复。lock/ruff通过，首轮新组合全量为
**4 failed / 2562 passed / 8 skipped，432.50s**，264个非文档对象无漂移。
四项均为上游新增 `test_recovery_atomicity` 的corrupt参数：夹具先在空根写坏
snapshot manifest，再首次创建EventLog，因此按新契约被孤立历史材料检查拒绝，
尚未进入其声称的恢复路径。此次失败不能作为新组合通过记录。

适配范围仅为该测试的夹具顺序：先初始化真实事件库和intent，再在启动恢复子进程前
写入坏manifest；保留真实硬退77、unknown结果与本地/对象阻断的原子性、再次恢复
唯一性等全部断言。产品准入与恢复代码不因这次夹具调整放宽。
原始失败及有限集成预核保存在[证据目录](evidence/data-admission-t1-2026-11-13/README.md)。
修后结果、独立复核和正式提交绑定继续单独记录。

夹具适配的修前同四参数亲跑 **4 failed / 4 deselected**，修后同一测试
**8 passed**，恢复与T1关联 **346 passed，23.40s**。
`tests/test_recovery_atomicity.py` 摘要为
`736d77c60f8f1d655867dab8bb7b0a7e85623613c182e8ca8d0402d6b46e7573`；
原九项已审内容无漂移，变动只有夹具顺序与坏字节正控。
修后完整套件已启动；以上定向结果不替代完整组合通过。

独立第二读者以912、原九项与最终夹具构造隔离副本，亲跑四个corrupt参数
**4 passed / 4 deselected**；只换回真实历史非原子recovery实现后，同参数
**2 failed / 2 passed / 4 deselected**，失败在原结果/阻断一致断言，真实DB确有
unknown result而缺workspace block。AST确认仅setup移动与新增字节断言，
原13条断言、child字串、参数化与硬退77均不变，结论为
`PASS_FIXTURE_INTEGRATION_ONLY`，未决0；不是修后全量、CI或正式提交批准。
见[独立夹具记录](evidence/data-admission-t1-2026-11-13/recovery-fixture-independent-review.md)。

## 修后完整组合（本机通过，原生CI待核）

9129095加最终九项T1内容及上述736d77c6夹具的实际组合：
`uv lock --check`、`uv run --frozen ruff check .` 与
`uv run --frozen pytest` 全部exit0；完整结果为
**2566 passed / 8 skipped，435.78s**（包装器436.08s）。
264个冻结非文档文件运行前后无漂移；九项独立内容与最终恢复fixture逐hash相符。
底层测试仍为Python3.12；仅对子进程去除本机provider端点配置，未输出任何配置值。
运行日志和冻结清单见本目录的 `t1-main133-fixed-*` 归档原件。

本组合补测最终127项及main #133的恢复集成，不能倒写首轮四红或旧2542全量。
UI源码、测试和dist未变，继承已核对象，不另跑未变UI。
最终提交SHA与内容继承须另行绑定；T1本机和有限内容通过不代表新原生CI、
T2/T3、恢复隔离或第一批全部通过。
