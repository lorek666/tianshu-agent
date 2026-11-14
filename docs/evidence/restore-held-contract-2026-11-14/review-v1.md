# B 窄契约独立文字审阅

2026-11-14 · **REVISE_BEFORE_CONTRACT_PR / NOT_APPROVED**

范围：仅审 DRAFT.md 的文字与可实施性，固定代码为 `9413c7ca66af56fce8ac1664e989cf8cd6fc9808`（tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`）。119 行草案 SHA256：`325a537637a5abecc3c9889bc10a24456065d426a782219174182f53688a922d`。未读取作者自查结论，未修改仓库或任何输入。下述时序是契约反例，不是产品实测；全部 B 验收、旧43项/10断点、产品 probe、CI/API/模型/付费均 NOT_RUN。

## 必须修订或裁决

### B-R1 · 未修 · 阻断：abort 在取得互斥前删除 stage

草案 L62、L77、**L92**。P 持最终 namespace exclusive，完成 P4 的 stage 重核，尚未写 intent。A 观察“无 intent”后依 L92 先删 stage；此时 A 尚未取得 namespace/entity exclusive。P 写 durable intent 并继续 rename，A 已破坏待发布内容；若 A 持已打开目录继续清理，rename 后还有误删 final 内容的风险。A 随后取锁 busy 或发现 intent 都不能撤销已发生的删除。无需后一种风险，删除封存 stage 本身已构成反例。

修订要求：任何 stage 写、删、seal、intent 与 rename 必须由同一固定 final namespace/批次操作互斥串行化；abort 在任何清理前先取得该独占、重核完整当前链及可 abort 状态，持续到清理与 durable 终结。明确长清理期间释放 gate 后保留哪一把独占，以及最终追加事实如何遵守 L45 的锁序。不能保留“先清理、后取锁/重核”；仅多加 intent CAS 或事后检查不够。

### B-R2 · 部分修 · 阻断协议空窗：token 原子写的归属未闭合

草案 **L75**、L91–92、L111 已允许无 token 的完全空目录受控重建，但未规定 token 原子写临时文件布局。允许的最短序列是 reserved → mkdir stage → 在 stage 内写/fsync token 临时文件 → rename 成正式 token 前硬退。此时 stage 非空、无有效 token；现文只能保全，既不允许继续建立 token，也不满足清理身份/token 前提，同批 resume/abort 无法收尾。

修订要求：明确禁止产生这种非空无 token 状态的原子写布局，或在首个目录内文件写前 durable 记录 stage 实体及精确控制临时文件归属，使重开可以只续做/清理确属本批的部分 token。若采用目录外临时文件，也须明确其作用域、归属与残留处理。逐一给 tmp-write/fsync/rename/dir-fsync 中断后的唯一合法动作；不得凭 basename 推定归属。不要求删陌生内容，也不把“保全闭锁”冒称可幂等收尾。

### B-R3 · 部分修 · 发布身份转换须定型

草案 L33、**L37–40、L68、L77–78、L91** 已声明同卷 rename 保持身份，但未明确原 empty root 与未来 final root 的不同身份角色。最短状态为：原空根实体 E 被 reservation 观察；stage 根为 S、DB 为 D；P4 rmdir(E) 后 rename(S → final)。若继续按 reserved 的最近既有祖先/根 E 重核，则合法转换会被 L38 判祖先置换；若忽略整条链，又可能放过真正替换。不能让实现自行猜何时改绑。

修订要求：明确 E 仅作发布前目标仍原 empty 的证明；稳定父/祖先不变；sealed/intent 预先绑定 S、D 为未来 final 身份。intent 后 stage 或 final 必须按阶段唯一映射同一 S/D；允许 stage 名消失、final 后缀出现及 E→S 的授权转换，不能任意换实体。published 后按稳定父＋final 名与 S 识别别名；E 保留为历史证据。absent-root 分支同样写明。此项是契约缺定型，不声称现有产品已发生误判。

### B-R4 · 未决 · v1 输入拒绝是兼容裁决，不能默认获批

草案 **L17** 拒绝机制 v1，同时 L11 要收口现有公共 restore_backup。固定代码 backup.py L1221–1272、L1524–1563 未按 v1 拒绝完整备份；data_cli.py L253–272 将较旧版本标 migration_required，仅拒未来版本；data_admission.py L205–226 可观察合法 v1，L231–239 才在普通准入拒绝它。因此普通业务拒 v1，不能直接推出隔离导入也须拒 v1。此为静态路径对账，未执行构造或产品测试。

修订要求：对原本能通过真实完整校验的合法 v1，明确保留 held 导入/只读复核并继续禁普通业务；或把拒绝列为兼容范围收缩，说明现行差异、理由与影响，由有权者显式裁决。旧未批准 v2 草案曾写同样拒绝，不构成授权。坏结构/未来版本仍按真实解析器拒绝，不要求接受损坏输入。

## 文字上已处理与实现证明义务

- 已处理：L19–22 明确任意数量全空骨架与 shared gate 取舍，不继承128及其他新增政策上限；L44–58 明确 namespace＋entity 连续 access 和全写者归属；L28–46 明确固定权威、完整只读观察与 gate 内过期检查；L68–70 保留完整静态本体且不默批 new_runs_only/release。
- 尚无产品证明：scope 等价/所有平台别名与硬链接；缺 main→anchor→entity 的写前连续性及实体锁失败现场；全部公开/后台/维护/材料入口；首权威竞争和只读 registry；review 全量范围与零写（含 SQLite 临时文件、journal、锁控制文件）；publish/abort/resume 所有真实进程屏障与故障断点；真实 OS/文件系统 durable 原语。现文不能代替这些证据。
- 容量：L71 对 staging 给出明确逻辑字节比较，承认非预留且不借2GiB；registry/目标父的“实际探测”还应明确未知/不足的失败判据与阶段处理。无需另造统一固定余量；不能把 metadata/journal 或并发耗用叫已保证。

结论只允许将四项反馈给作者修稿；不批准契约、实现、平台支持、恢复服务或 PR144 状态。修订后按新稿 SHA 重新审受影响条款。
