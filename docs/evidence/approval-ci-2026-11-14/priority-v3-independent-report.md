# PR147 v3 诊断 delta · 独立复审

2026-11-14 · **PASS_DEFINED_DELTA_SCOPE**。固定test SHA256 `f138d6c13e3ed2966870626effed5488c776bfd66052c3a8800396b35203f01a`；manifest `1338d0901d802b5c4c39c6ac48fa3b22b377929f2d29c2a13a03fe31a6803591`；delta-from-v2 `606a939d1edc2085793f754643d743a98e6ac3760c2d346b76b7848f980306c5`。仅评审root只读freeze及旧固定源；未读live或作者结论。

**本次短关键事实缺口已修。** 两项新正断言均通过：64事件长链中，c1结果的call_id/status/effect/error、收据的call_id/resource/after_state/intent_seq以及run.end/run.finish全部可见；长error的同一priority行保留c1、rejected、none_applied、rule_id及错误前缀。旧冻结fa992的真实helper在完全相同输入上重现关键尾部不可见、rule_id丢失，两个负控按预期通过。两个薄runner均exit0；负控exit0表示旧缺口被实际复现，并非旧候选修复通过。

新原审批节点仅执行1次，pytest exit0、setup/call/teardown passed，原13 assert保持。collector=0，诊断RPC=0；非轮询RPC顺序与v1一致。v3总11次调用、v1原成功记录10次，差异是普通状态轮询，不是新增诊断。全部导入确来自对应固定副本；77件新复制源、77件旧固定源及新freeze实际提供的4件文件执行后hash不变。

1080条manifest与v2路径全集相同，只有target条目变化；两份精确补丁与旧固定原件逐字节重建相同。新增docs／KN没有读取正文，不把manifest身份继承冒充此次读取内容。原13 assert、完整原节点、FakeModel、Harness、root、client及wait helpers跨v1/v2/v3 AST相同；production来源未变。旧真实拒绝／collector fallback证据仅对未变执行方、原节点、采集调用和except/finally边界继承；此次新增priority输出只做了已授薄边界验证，不宣称重新运行过真实失败路径。

长error探针已修正reviewer自身的过强JSON解析要求：只解码实际已观察到的短字段前缀，再核对同一行错误前缀，保持c1／状态／rule_id的精确值与绑定。长链resource／call_id／状态／终态断言均未改变，assert AST多重集不变。v2原误判exit1及其raw/facts/PARTIAL封存原件完整保留，没有覆盖或改写。

**范围与未决。** 本次note量测（不含落盘末尾换行）：新长链24000 chars／66634 UTF-8 bytes，新长error3153 chars／7301 bytes；旧负控分别24000／67398、1952／4094。仍是每项1200 chars、总24000 chars、最多40条事件；不是24KB。verbose链与任意长字段仍会有损，截断行可不是完整JSON；首1000页不翻页、既有redact能力边界及无通用墙钟硬上限继续保留。此次只解决已定义的短关键事实优先级，不承诺所有任意长字段完整。

baseline、真实拒绝／fallback重复、full、Windows、压力／50次重复均本轮NOT_RUN；root自己的full门未作为本报告证据。原PR146 Windows红及根因 **NOT_ESTABLISHED** 不变，不将诊断修正当作Windows根因修复；未给merge approval，也不把本固定base+patch结论冒充后续正式SHA或native CI已通过。

准确命令／退出码／完整raw在 `results/*.command.json`、`results/*.raw.log`；输入、输出、导入路径及前后hash在各 `result.json`、`checkpoint.json`、`binding.json` 与 `facts.json`。pytest的1条current symlink按canonical根核属包内，完整metadata保留后移除；没有活链接。索引列全部regular exactset及hash，文件0444／目录0555。
