# T2 author ledger

Base: 585e3474b7f9f03bb118308470fac1f500c3a02e
Plan SHA256: 464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875
Scope: Task2 only; no T1/backup/maintenance/data_cli edits; no full/commit/push.

- 初次定向 RED：36 failed / 14 passed，1.71s，red-first.log；同时包含真实未拒绝和新模块/API缺失，不能全部算行为证据。
- 首次GREEN尝试：7 failed / 43 passed，2.26s，green-first.log；失败原件保全。
- Ruling: DB1、done=()、current=1属于合法after-step1窗口，错误负控应改为current=None，而不是收紧产品拒绝合法窗口 — 以契约§2为准 — 若错会阻止真实崩溃续做；新增from0/from1全部21种位置组合正负控。
- Ruling: 旧backup字典和applied list改为共享frozen对象和tuple，另一计划用真实中断产生的完整format2再仅改plan摘要；未来target99另保留参数 — 让测试到达声称的判据，拒绝非法格式仍保留 — 若错会把缺字段错误冒充计划绑定。
- Ruling: 未知真实user_version在同一conn先核已注册计划，保留UnsupportedSchemaVersion旧公开薄子类；STATE_FORMAT/STATE_FORMAT_VERSION显式重导出共享常量 — 不复制parser或多开连接 — 若错会破坏公开调用方错误分类。
- Ruling: macOS固定/var、/tmp、/etc别名只有实际解析到/private对应目录才允许；用户根/根内symlink和junction拒绝，锁后复查 — 沿用预检与canonical边界 — 不宣称同uid对抗保证。
- 修正后原组50 passed；补充窗口、进度故障、同事务回滚、持锁同conn与完整布局后86 passed；typed五值测试去除五次同样循环的重复参数，保留五种类型互异断言；异根/旧事件拆成两个独立可复现参数。
- 真实旧源码后续重演：隔离git 585e347 src（70文件hash）与冻结测试，仅选11个行为反例，11 failed / 62 deselected，0.70s，全部DID NOT RAISE AdmissionRefused；不是初次RED、不是独立复审。
- 补查pre-open：真实空主库旁分别放历史WAL/journal/shm，禁止sqlite3.connect，3 failed；仅在migrate._connect复用T1 check_new_file_evidence后3 passed。此探针证明前置顺序，不证明任意损坏WAL的恢复。
- 最终候选：迁移两文件90 passed，4.71s；备份/状态/准入四文件208 passed，2.41s；产品字节I/O门禁1 passed；lock/ruff/diff均exit0。四文件hash运行前后稳定；438个非授权基线文件无漂移。最终绑定在final-local-results.json。
- 候选文档只记本机有限范围；独立实现复审/具名变异、最终组合full、原生CI仍待父会话安排。未授T3，不接CLI，不提交推送，不自批。
