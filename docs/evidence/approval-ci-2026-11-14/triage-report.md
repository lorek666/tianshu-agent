# PR146 原生 CI 首红 · 独立窄 triage

2026-11-14 · **NOT_ESTABLISHED**（根因未建立，未批准合并）

原 run **43559488826 / attempt 1** 为失败，PR head `3e9da0bd63399fc86488018605138b54b22fb427`；日志实际 checkout `819cc342b8359ce0e45a6b3b4b75907e8829a573`。所收 Git 原件中两者 tree 均为 `0348cdf86e290253f6bf6b8064c046bf1b687779`，checkout 的 parents 为 `8f2a508e68f27efc4100b7466fcd79043341f7fa` 与 head。本轮只读这些固定对象和提供的90份原件，没有新查 API，没有修改 repo、refs、live 或旧原件。

Windows 片0 job **132339744589** 的唯一失败是 `tests/test_integration_m5.py::test_approval_is_decided_through_ipc_and_run_continues`，call 阶段失败而 setup/teardown 均通过。日志 L1888–1892 明确落在测试 L271：`old.txt` 的 exists() 为 True；此前 L270 `run.state == "succeeded"` 已通过。整片 **1 failed / 1583 passed / 12 skipped / 1595 deselected，800.66s**。后面的 approval.consumed、receipt、审批清空断言尚未执行，不能假定它们通过。片1、Ubuntu、macOS、UI 成功；汇总 job **132343301042** 因 Windows 两片未全成功而失败。两份 Windows artifact 的 zip digest、实际 checkout、run/attempt、相同有序3191项 collection、互斥全集和 finished 覆盖已核，两个报告合计只有该 call 阶段失败。收集 errors=[] 不改变 run 红色，也不表示工具事件已被捕获。

在全新临时目录重建77份固定源，字节均等于 head，亦与 parent 相同；这只证明源码继承，不证明基线或 flaky 归因。用现有 **Python 3.12.13 / macOS 26.6.2 arm64**、与 uv.lock 一致的六项 dev 版本，在独立进程中运行**同一个未改动的离线 FakeModel 集成测试50次**：**50次各1 passed，0失败、0超时**。每次原始 stdout/stderr 与导入证明保留；57个 tianshu／测试模块路径均指向副本，77份源前后无变化。CI 是 Windows Server 2025 / CPython 3.12.16；本机没有执行 Windows 或分片包装器，没有跑 full、parent 对照、收费模型或外部 API。结论只能是**本机50次未复现**，不能据此归因 Windows、随机、issue145 或“纯docs所以一定是基线”。

固定源码给出的排查边界是：`approvals.decide` 的 resumed=True 只代表入队（control.py L623–668）；普通 rejected/none_applied 结果可被循环吸收并继续（loop.py L2136–2197），随后 FakeModel 的 end_turn 在无报告产物时得到 noop（L1992–1995），`_finish` 再把 noop 映射成 Run succeeded（L2990–3041）。这是**静态可行路径，尚无证据说明本次 CI 走过它**。真正的删除处理器在 unlink 完成后才返回 succeeded/applied（tools.py L485–519；sandbox.py L149–150），文件 receipt 也只在 applied 时生成并绑定真实 resource/intent/result（receipt.py L126–200）。因此 succeeded＋文件仍在尚不足以确定实际工具结果；现有 CI 失败输出没有 c1 的真实 tool.result、receipt 或对应审批／租约载荷。

下一份最小证据应在单独诊断分支中、`_wait_run` 返回后且原 L270/L271 断言之前捕获：run／原审批卡／decide 返回值、精确 old.txt 路径与存在状态，以及按 seq 排序的 c1 tool.intent、execution.started、dispatch拒绝、**tool.result 的 status/effect_state/error/rule_id**、receipt 的 resource/after_state/intent_seq、approval／lease／claim 事件、run.end／run.finish 的 outcome/effect_state/reason。诊断材料须在 root 夹具清理前写出，采集错误另记；**保留全部原断言与时限，原失败仍非零**。先由真实结果区分“未派发／被拒／结果不明／声明已删但路径仍在”，再决定下一步；本轮仅提出文字诊断，不实现它，不猜共享DB锁或加泛重试。

原始 CI 日志：`/tmp/tianshu-g3-20261113/restore-b-pr146-native-ci-20261114/job-132339744589.log`。完整失败 nodeid 见上；文件及 artifact SHA256、源码行范围与50次命令／结果索引见 facts.json、ci-input-index.json、source-manifest.json、results.json 和 index.json。只读验证器曾把 full_collection 的 `[nodeid,file_path]` 条目误当字符串，产生 TypeError；已按真实形状修正并留 observer-correction.txt，未导致产品失败或额外测试。

**PR146 原生 CI 仍为失败；本次不批准合并，不宣称已修复。** issue145 是另一失败节点，本轮不建立两者因果关系；BASE-B1 未修，B 产品及断点仍 NOT_RUN。封存前90份旧原件和77份固定源再次验 hash，原始50次日志／导入证明全部保留；此后停止写入。
