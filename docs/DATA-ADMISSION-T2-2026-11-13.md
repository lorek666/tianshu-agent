# 数据准入 T2 · 迁移一致点绑定本地候选

T2 为显式离线迁移增加完整一致点绑定与严格续做协议；本机定向验证通过，独立实现复审、最终组合全量与原生 CI 尚待验收。本文不是 T2 独立批准或 G3 完成记录。

基线为 `585e3474b7f9f03bb118308470fac1f500c3a02e`，工作分支为 `codex/g3-data-admission`。权威输入为 [数据准入契约](data-admission-contract.md) 和 SHA256 为 `464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875` 的实施计划 Task2、§1.4、§2。本片仅改 `data_digest.py`、`migrate.py`、两份迁移测试与事实文档；T1、backup、maintenance 和 data_cli 保持基线内容，其他 debug 模块未改。

## 实际行为

`migrate()` 只接受标准根内、无用户 symlink/junction、`st_nlink == 1` 的普通 `events.db`，取得独占维护准入后复查布局。macOS 固定的 `/var`、`/tmp`、`/etc` 系统别名须确实解析到对应 `/private/` 路径；这不放行用户根或根内链接。打开空主库前复用 T1 的历史旁文件检查；全程使用同一条真实活库连接完成检查、摘要、每步事务和最终复查。

业务摘要枚举完整持久 schema 与所有持久表：保留普通表、未知扩展表、`sqlite_sequence`、FTS 虚表及其 shadow 表、index/trigger/view 定义和列元数据；类型区分 NULL、TEXT、INTEGER、IEEE754 FLOAT64、BLOB。包含可寻址 rowid，保留重复行；generated 列读取实际值，虚表 hidden 查询控制列只保留元信息。无法完整读取的形状拒绝。仅从业务摘要排除经形状核验的 `migration_log` 和 `idx_migration_log_applied`，未知附着对象继续纳入；实际版本与完整 typed audit 由机制摘要另核。

材料完整枚举 `artifacts/`、`blobs/`、`memory_blobs/`，包含隐藏和 `.tmp` 文件。活根、保存备份实物、manifest 除 `events.db` 的声明须双向相等；不复用旧备份遍历器的省略规则，不扩备份格式。

首次绑定只接受无任何进度的真实 v0/v1，要求保存库真实版本等于活库起点，完整机制、业务与材料均相等。新进度仅写 format2 的 `events.db.migration.json`，绑定实际 DB、原备份 canonical 路径、manifest 原始字节、算法与完整 SQL 计划。旧关联 `migration.json` 仍由共享严格解析器验证，不转换、不覆盖、不授续做权。

续做只接受已登记 `current_step` 的提交前/后窗口；不按 DB 数字补造步骤。每步 DDL、`user_version` 与 audit INSERT 同事务，继承 audit 值必须保留；失败不写 completed。四个既有 failpoint 保留真实子进程退出码 77。

完成后的重复调用分两路，均不写 DB 或进度：原备份路径只能验证原绑定，坏备份、manifest 替换或后续业务/材料变化直接拒绝；不同路径的新 v2 备份则核当前完整机制、业务与材料，不依赖旧备份存在、不改绑、不刷新时间。实际 v2 缺 format2 进度或仅有 legacy completed 时，迁移拒绝；普通 EventLog 的有限 legacy 兼容与日常不核历史业务基线的行为保留。

## RED、修正与本机验证

原件与哈希见 [证据目录](evidence/data-admission-t2-2026-11-13/README.md)。测试通过范围绑定 `final-local-results.json` 的四个源/测试摘要；后续实质变更须重验，不能继承此处数字。

- 初次 RED 为 **36 failed / 14 passed，1.71s**，含真实旧迁移未拒绝与新增 digest/API 缺失；不能统称 36 项行为反例。后来在隔离的精确基线源码上重演异根、旧事件、投影、五类额外材料、真实 FTS optimize、真实版本与完整 audit 起点共 **11 项**，均在期望拒绝处 `DID NOT RAISE AdmissionRefused`。该重演有源码/测试哈希，明确不是最初 RED，也不是独立复审。
- 首次实现为 **7 failed / 43 passed，2.26s**。其中“跳步”夹具其实是合法 after-step1 提交窗口，修正为 DB1 却无 done/current，并另以全部可达位置正控保留合法窗口；旧测试改为强类型 format2，另一计划负控改为真实中断产生的完整状态。未来 target99 拒绝仍以独立参数保留。未知真实版本通过同连接先核计划，保留旧公开薄异常名。
- 补查迁移入口的空主库历史旁文件 pre-open 缺口，先取得 **3 failed**，仅在迁移连接入口复用既有 T1 检查后 **3 passed**。这证明在真实空 DB 与历史 WAL/journal/shm 文件前不会调用 SQLite 打开；不把探针解释为已证明任意损坏 WAL 的恢复。
- 最终迁移组：`uv run --frozen pytest -o addopts='' -q tests/test_data_binding.py tests/test_migration.py` → **90 passed，4.71s**。覆盖 from0/from1 全部合法和非法位置组合、真实多 segment FTS optimize 后虚拟行相同而 shadow 确变、字节相同保存库替换、6 次进度发布失败、DDL/version/audit 同事务回滚、同连接与持锁检查、hardlink/symlink 及锁后布局复查、完成 A/B 的逻辑/进度字节不变。
- 最终关联组：`uv run --frozen pytest -o addopts='' -q tests/test_backup.py tests/test_backup_materials.py tests/test_data_state.py tests/test_data_admission.py` → **208 passed，2.41s**。
- 额外产品字节 I/O 门禁 **1 passed**；`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check` 均 exit 0。四个候选源/测试运行前后哈希相同；438 个非授权基线文件无漂移。

两组测试使用既有 `run_clean.py` 在子进程局部去除 `TIANSHU_*` 和以 `BASE_URL`/`API_KEY` 结尾的配置，不打印值，不改用户或全局环境。本轮未跑全量、UI、Windows/ARM64 或原生 CI，未 commit/push。Windows 链接构造不支持时测试会注明真实 skip 原因；本机没有发生该 skip，不据此声称 Windows 设备已验。

## 待独立验收与边界

第二读者须按最终冻结源码执行计划具名变异负控：额外活材料、完整业务表/FTS shadow、原 manifest/计划/真实机制起点、严格 current 窗口、原路径不得降级新备份分支、完整 audit/定义和 no-op 不落盘。作者自查和上述本机定向不构成批准。

T3 的迁移 CLI 未接；restore apply、恢复隔离、容量保护与 GC 仍未交付。维护准入是合作锁，不能对抗 raw SQLite 或同 uid 恶意写入。逻辑/材料与 manifest 字节完全相同的文件替换视作同一个恢复点，不宣称 inode/UUID/generation 身份保证。当前两步只改变迁移机制，不能以此摘要协议证明未来任意业务表迁移可续做。
