# PR147 审批失败诊断候选 · 独立规格与行为复审

2026-11-14。范围仅为冻结的诊断候选；这是独立第二读者的离线定向评审，未读取作者自查结论、作者验证原件或 live 测试正文。原 PR146 Windows 红及根因 **NOT_ESTABLISHED** 均不变。本报告不构成 Windows 通过、原根因修复、当前提交复审或合并批准。

## 结论与修复分级

- **已修／本范围有证据**：原成功行为与13个断言保持；成功不调用采集器、没有新增诊断 RPC；一次真实审批消费后的受控写前拒绝，在关闭客户端、退出 Harness、删除临时根之前补出与同次真实事件一致的诊断；采集器抛错仍保留同一个原 AssertionError 对象、原 L413 断言帧和非零退出。
- **部分修／仍有损**：输出已限长、标记截断，且环境／非白名单字段与已登记或可识别格式的合成秘密没有泄露；但并不保证任意长错误、任意长事件链下的全部必需字段可见。独立合成探针证明，总截断会丢尾部 c1 result、receipt、run.end、run.finish；单项截断会丢长 error 后的 rule_id。不得将这一部分写成“完整取证已解决”。在此次实际34事件／24链的原用例受控失败中没有截断，必需的现存事实全部可见。
- **未修／未验证**：原 Windows CI 失败的根因与跨平台修复；本轮没有 Windows runner、full suite、产品修复、付费／真实模型调用或新 CI。

在定义为“原单次 c1 审批测试失败时，尽力附加有界诊断”的窄范围内，没有复现阻断性行为回归。截断情况下的完整取证仍为明确限制；若验收要求任何输出量下都完整保留关键字段，本候选不能据此通过该项。本报告不给 merge approval，也不把固定 base+patch 评审冒充后续正式提交已通过。

## 冻结身份与静态对账

- Git base/head：`8f2a508e68f27efc4100b7466fcd79043341f7fa`。
- 冻结目录：`/tmp/tianshu-g3-20261113/pr147-root-frozen-ni0h01n0`。
- `manifest.json` SHA256：`549501b83fffb6f9dd992ccec40c9d3edb90d3bd46ea9b2a95b92f39d52978cc`。
- `candidate.patch` SHA256：`b3a5d6dbd78d651f7f753f5f9461abdc4cbc9e59e3f3708e23608c783df2d48b`（9,129 bytes）。
- 候选 `tests/test_integration_m5.py` SHA256：`fa9921084936dd0cb9374418e1cf9601a7c4789f5b54fae462baf275326c6fc7`；基线对应文件：`90bacf76c02db4f77ee4092a3b76f01a839ad62c4a7f67d981d8744fc1746d9d`。

冻结1066个 regular 文件、34,842,644 bytes 的路径全集及各文件 hash 已与固定 Git 对象对账，唯一变化是目标测试；生产、CI、依赖锁及其他1065件原字节不变。原审批函数的13个 assert AST（包括消息）完全相同，FakeModel、DaemonHarness、root、_client、_wait_state、_wait_run 的完整 AST 与 base 相同；原等待时限及 client.close 未变。未新增 skip、xfail、放宽断言或重试成功逻辑。

执行仅使用自有最小副本：candidate 与 baseline 各77个固定源文件。所有 `tianshu` 模块实际导入路径均在相应副本 `src/`；测试也来自副本，目标 hash 与上述一致。执行后154件复制源逐字节复核与对应 Git／冻结原件相同；外部冻结1066件、manifest 与 patch 也再次核对未变。详情见 `freeze-binding.json`、`facts.json` 和各 probe 的 `result.json`。

## 独立行为证据

环境是 **macOS 26.6.2 / arm64 / Python 3.12.13 / pytest 9.2.0 / ruff 0.17.4**；复用现有解释器，没有安装、升级或修改环境包。子进程先剔除匹配既定名称规则的模型／凭据／endpoint 及 pytest 继承注入变量，只记录被剔除的名称，未输出、保存或哈希真实值。显式设置副本 PYTHONPATH、关闭字节码与第三方 pytest 插件自动加载。

真实节点均是冻结原节点 `tests/test_integration_m5.py::test_approval_is_decided_through_ipc_and_run_continues`。独立 pytest plugin 只在内存包装该客户端的 call/close 与采集入口、记录 Harness 退出，并将测试临时根定位到 reviewer 自有短路径；没有修改源码或原 assert。

1. `baseline_success`：原节点一次，exit **0**；setup/call/teardown passed；collector **0**。
2. `candidate_success`：原节点一次，exit **0**；setup/call/teardown passed；collector **0**。两次本机轨迹恰好均10个 RPC；除轮询外的顺序完全相同。结论依赖零采集调用及原 RPC 顺序，不把调度造成的轮询次数差异当作新增 RPC。
3. `real_failure`：仅在内存对真正 `FileExecutor.execute` 的 c1 注入 `ToolNotAllowed("independent prewrite probe")`；真实审批、grant、intent、ExecutionPermit、dispatcher 异常处理与事件持久化照常。exit **1**，setup/teardown passed，call 仍在原 **L413 `assert not (ws / "old.txt").exists()`** 失败。采集器接收的异常对象与 pytest 报告中的对象相同。
4. `collector_raises`：同一真实写前拒绝，再让采集器自身抛 RuntimeError。exit **1**；同一异常对象、原 L413 帧仍保留，附注仅 `M5 approval diagnostics: collection_failed=RuntimeError`，没有把采集器错误替换成主失败。
5. `synthetic`：8个明确标为合成的薄边界用例，exit **0**，覆盖 run_id 缺失、RPC/stat/只读打开失败、不可序列化值且禁止 repr、坏 events 形状、白名单与脱敏、非 ASCII 超量、长 error 字段顺序、有效只读 leases 投影。它们不是原 CI 复现，也不冒充真实事件证据。

诊断失败探针故意保持红，两个 exit 1 是负控成功的必要证据，不能汇总为“全部测试绿”。目标候选文件的独立 `ruff check --no-cache` exit 0，输出 `All checks passed!`。未运行 full/压力或50次重复。

## 同次真实事件逐项对账

在 `real_failure` 的采集入口，reviewer 另做 **1次** `events.run(limit=1000)` 真值读取并只读查询当前 leases；这一读明确标记为 `independent_truth_read`，不计入候选的3个诊断 RPC。候选随后恰好调用 run.status、approvals.list、events.run 各1次，无递归或重试。

`truth-events.json` 的34事件与5485 chars / 5521 UTF-8 bytes 的异常附注逐项核对：

- c1 `tool.intent` seq22、`execution.started` seq24；真正执行入口收到非空 ExecutionPermit。
- `approval.requested/decided/consumed` 均存在；消费的 approval_id 与原卡片及 intent 一致。
- c1 `dispatch.rejected` seq25 与 `tool.result` seq26 均来自真实 dispatcher；result 的 status 为 rejected、effect_state 为 none_applied；output.error 为 `ToolNotAllowed: independent prewrite probe`，rule_id 为 `dispatch.not_admitted`，request_id 与真值一致。
- **没有 receipt**：同次完整页 has_more=false，真值确实无 receipt，诊断显式 `missing_in_page: ["receipt"]`；不能编造其 resource、after_state 或 intent_seq。`run.end` seq34 的 receipt_ids 也为空。
- 当前 leases 真值为空，附注 `rows: [], missing: true, truncated: false` 一致。这只能说明采集当下的投影，没有推断历史上从未持有租约。job.claimed 的 owner_pid、owner_instance_id、claim_epoch=2 与真实事件逐字段一致；run 中的 lease_epoch 与 execution.started 也可见。
- `run.end` seq34 为 outcome=noop、none_applied；`run.finish` seq35 为 outcome=succeeded、none_applied；run.status/cached 的 succeeded 是真实值，未用它遮盖工具拒绝。
- old.txt 的 metadata 指示 exists=true、size=21；不读取或输出文件正文。采集发生在 root/DB/文件都还存在时，collector_exit → client_close → Harness退出的生命周期顺序有原始记录；夹具清理后 root 已删除，诊断 raw 仍在本包。

上述受控拒绝解释的是这一人为探针，不证明原 Windows CI 也经历了相同拒绝或有相同根因。

## 有界、保密与剩余限制

候选声明及实际实现都是每项 **1200 Python chars**、最多40条筛选后事件、总 note **24000 Python chars**，不是1200/24000 bytes。合成中文超量 note 实测24000 chars、**67396 UTF-8 bytes**；在 Unicode scalar 字符可 UTF-8 编码的前提下，note 自身有数学上的96000 bytes上界。该值不是整个 pytest traceback/log 的字节上限。

总截断会先截断字符串拼接结果；因此“前20+后20”不保证后20最终可见。合成64事件例中实际只剩20个事件行，尾部 c1 result／receipt／run.end／run.finish 全不可见，尽管 `missing_in_page` 不会将已在输入页里的事件写成缺失。`[truncated total]` 明示信息有损，但并不列出每个被总截断裁掉的事件。单项长 error 例保留 status、effect_state 与错误前缀，**rule_id 不可见**；`[truncated item]` 也不保证截断后的 JSON 可解析。首1000事件页不翻页，has_more／链超40时虽标记 truncated，仍不能把页内缺失推广成数据库中不存在。

合成 env 哨兵、非白名单 api_key/config/headers、output 内非白名单字段、args 的非 path 字段均未出现；已登记秘密、Bearer/sk-/URL密码格式被脱敏。未使用真实凭据。该证据只涵盖字段白名单与既有 `redact` 认识的格式，**不保证任意未登记且无可识别格式的字符串秘密不会出现在允许的 error/reason/path 中**。

静态调用上限为3个 RPC、1次只读SQLite连接／查询，events请求 limit=1000，approvals最多8、leases最多8（查询9以检测截断）。客户端原10s接收超时和SQLite1s busy timeout保持；未实测墙钟超时，并且 send、文件系统操作等不构成硬实时保证，不能宣称“采集必定31秒以内结束”。只读SQLite既在不存在DB时未创建文件，也在有效合成leases库中保持前后字节 hash 不变。

## 原始材料与复核方式

- `run_real_probe.py`、`run_synthetic_probes.py`：独立注入与断言原件。
- `launch_probes.py`：被清理的环境名称、外部解释器、准确命令及退出码留在各 `results/*/launch.json`；完整stdout/stderr留在 `pytest.raw.log`。
- `results/real_failure/`：truth events、truth leases、cached输入、exception notes与生命周期／导入证明。
- `results/collector_raises/`：外层兜底仍保留原失败的raw。
- `results/synthetic/`：每个薄探针的完整notes与量测。
- `verify_results.py`：独立结果／源hash／事件核对；本次exit0的命令记录为 `results/verification-command.json`。它在转存symlink前单次执行，不应在已封存目录直接重跑。
- pytest创建的4条current目录symlink已逐条记录路径／目标／mode／目标归属到 `symlink-metadata.json`，随后仅移除自有包中的这4条活链接。本包无活symlink；没有静默忽略它们。

复现应先复制本包到新的可写reviewer目录，删除新副本的 `results/` 后按各 launch.json 的 mode 在新副本运行 launcher，最后再运行核对脚本；解释器与外部冻结目录仍须是上述身份。不要改写本封存包或外部冻结原件。这里的源路径hash证明与负控只绑定所列固定base+patch；root后续实质改动必须重审受影响范围。
