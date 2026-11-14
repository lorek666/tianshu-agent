# B v2 独立窄文字复审

2026-11-14 · **REVISE_BEFORE_CONTRACT_PR / NOT_APPROVED**

范围：仅独立复审原报告 B-R1–R4、容量备注及 stage_owned/abort_intent 的受影响协议；未重审未变范围。DRAFT v2 共143行，SHA256 `76364357e1ca99b2f3c845efb41356d5cde00a6d241c916cfeeb3ae9f052f57e`。固定源码仅取 `9413c7ca66af56fce8ac1664e989cf8cd6fc9808`，tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`。未读作者 DELTA/self 结论，未执行产品/测试/探针/API/模型/付费，未改仓库或输入。所有 B 产品验收仍 NOT_RUN。

## 原问题的文字判定

- **B-R1 已修（原清理并发竞态）**：L47、L64、L70–72、L111–116 明定任何 stage/control 写删前取得 reserved 指定的固定 final namespace 操作独占；放 gate 后持续 namespace/entity 至事实终结，只向后取 registry，不反向重拿 gate。abort_intent 在清理前 durable，硬退后只能续同计划，不能另回 copy/publish。原“先删后锁”反例已被禁止；这不覆盖下述新增删除持久化缺口。
- **B-R2 已修（token 归属四窗口）**：L33、L87、L94–98 在首个 stage 内文件写前 durable 绑定 S/P、精确 tmp/正式路径及可重算字节。mkdir→stage_owned 前只允许空 S；tmp-write/fsync 中断可按准确前缀续做；rename 后与 dir-fsync 前后核正式 token 并 fsync；payload 不先于正式 token 持久化。未知项保全，origin 另有精确布局。stage_owned 自身无 hash 自引用。
- **B-R3 已修（E/P/S/D 转换）**：L35、L39–42、L89–91、L111 将 E 排除于稳定祖先，将 P 固定，并预绑未来 final 的 S/D。L89 明文允许 E→缺失→S，因此 rmdir(E) 后硬退、S/D 尚在 stage 时可同 intent 继续；不要求 E 永久仍在。两份冲突、S/D 丢失或 P 真替换均不授权。L42 的“原缺/空”须与 L89 的显式转换合读，不能实现成 empty 分支见缺失就拒绝。
- **B-R4 已修（合法 v1 兼容）**：L17、L130 恢复真实完整 verify 可接受的合法 v0/v1/v2 held 导入/专用只读复核，v1 报 migration_required，不迁移/伪造 progress/放行业务。与固定 backup.py L1221–1272/L1524–1544、data_cli.py L253–272、data_admission.py L205–239 的原兼容区别一致。
- **容量备注已修（准入判据）**：L79–83 分开规定 staging 的未完整校验文件全量＋已知控制字节、registry 的完整规范事实字节、final 父目录 metadata-only 时零可用拒绝；未知/坏类型/失败均下一写前停。每位置/阶段重核，不借2GiB或同卷跳过，不冒称 metadata/journal/并发耗用已预留。仅文字准入判据获本次确认。

## 新增阻断 N1 · 未修：删除持久化先后未定

**位置：L114–116，关联 L41、L118–119。** 新协议规定 durable abort_intent→逐项 unlink/rmdir→观察 S 不在→durable aborted，但未明确删除影响目录的持久化、尤其 S 最终删除后 P 的屏障必须先于 aborted。L118 泛称目录持久化能力，未补出此处必须遵守的提交先后。

最短时序：① 在尚无 origin 的合法 stage_owned/copy 阶段 durable 写 abort_intent；② unlink/rmdir 返回、进程观察 S 已无，但影响目录/P 尚未持久化；③ 位于另一卷的 registry durable 提交 aborted；④ 在声明允许的 OS/文件系统崩溃恢复中，未持久化删除可回现 S/旧材料，而链已为 aborted、L41 已解除范围。此状态不是“仍有 abort_intent 可同计划重试”，而是终结事实先于清理的持久证明。它是文字时序反例，未运行物理断电或产品探针。

**精确修订**：每个 unlink/rmdir 所影响目录须按平台声明能力持久化；S 最终删除后必须成功持久化同一稳定 P 的目录项，再核现场并 durable 追加 aborted。任一 flush 失败或中途硬退都保留 abort_intent，不解除预约；重开仅按原 CleanupPlan 重核、续删/再持久化，不能扩大计划。若平台不能保证所需屏障，不得追加 aborted。将“已看不见”与“已持久删除”分开，不能由 registry commit 或成功 rmdir 返回代替；验收需覆盖这些新增屏障，产品仍 NOT_RUN。

## DOC1 · 未修：本地 main 与远端跟踪引用混写

**位置：L4。** 当前只读 Git 核对：`refs/heads/main=016910970d381a66ab12a94a5c88080a8cbf7437`；`refs/remotes/origin/main=8f2a508e68f27efc4100b7466fcd79043341f7fa`。8f2对象与9413对象的 tree 均为 `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`，但不能写“本地main为8f2同tree”。应明确是本地已缓存的 origin/main 引用/8f2对象核验，不能据此推断远端实时状态或 CI 已通过。本轮未查 API/CI。

结论：原四项与容量备注已在文字层修复；**当前稿仍不能进入正式纯 docs(contract) PR**，须先补 N1 和 DOC1，再仅对这两处 delta 按新 SHA 独立复审。此结论不批准产品实现、平台支持、B 验收或恢复服务；不重开旧43项/10断点，也不借 main/PR144 状态作 B 证据。
