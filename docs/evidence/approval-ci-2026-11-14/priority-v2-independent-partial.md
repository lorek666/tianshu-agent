# PR147 诊断优先级 delta · PARTIAL

2026-11-14。固定新候选 `f198cef96da3a3d24e933151f6cf76e99dcb6644a91ab1e2ab26849775141ca4`；manifest `037c0b6aea96881212af054191e33be3e2885974d6ab0051c9c6378b92a90531`；delta `b71932915769679ab782233fcbcde2324dfc3ab0c1bcdd537afa643b1ac8fcdc`。仅从root只读新freeze及旧固定源复制到自有/tmp，不读live或作者结论。

**真实阻断：收据短resource仍被丢弃。** 新longchain正断言真实exit1，`delta_probe.py` L150。c1 result、run.end与run.finish均可见；优先收据只有 `{"call_id":"c1","intent_seq":7,"after_state":"tail-required-after"}`，没有输入中的 `resource: tail-required-resource`；verbose尾部被总截断。notes为24000 Python chars／66696 UTF-8 bytes，带 `[truncated total]`。这要求保留短关键事实，不是要求任意长字符串完整。

**长error：关键短事实在原始行可见；脚本有解析过强问题。** 单独完成第二项已授新正探针，真实exit1，`delta_probe-selected.py` L172。原始priority行同时含 c1、rejected、none_applied、tail-rule-identifier及至少40 chars的错误前缀，rule_id没有被吞。runner额外要求该截断行可被完整JSON解码，导致结构化可见性标记false；既有允许的 `[truncated item]` 本就会造成JSON未闭合。这是reviewer准备脚本的过强解析要求，不作为第二个产品缺陷。保留真实退出1、raw和checkpoint，不重跑或将退出改写成0。对既有raw的短字段前缀单独JSON解析及错误前缀核对已通过，未重新执行候选。notes为3161 chars／7309 UTF-8 bytes。后续只需调整解析适配，不削弱原关键字段值和绑定断言，也不要求作者修复整条JSON完整性。

两次真实导入均来自自有固定副本，checkpoint保留导入路径和源码前后hash。77件复制源及新freeze实际提供的manifest／patch／test三件执行后未变。1080条manifest中原1066条只有target与KN改变，1064未变文件已从旧固定源重新核hash；KN只核manifest与root已审identity相同，没有读取正文，新增docs也没有冒充本轮逐字节读取。原13 assert、完整原节点、FakeModel、Harness、root、client及wait helpers AST均等于v1；真实拒绝／collector fallback的旧证据只按未变身份继承，不是本轮重跑。

按root“发现缺口先报、最小PARTIAL后停写”的指示：**旧两负控、新成功节点、baseline、full、Windows、压力／50次重复均NOT_RUN**。未启动其它组，未改repo／refs／live，未给merge approval。旧PARTIAL报告不覆盖。原PR146 Windows红与根因 **NOT_ESTABLISHED** 保留，不能把此诊断修正当作Windows根因修复。

原始材料：`results/new-positive.raw.log`、`results/new-positive.command.json`、`results/new-positive/checkpoint.json`及其input/notes；长error对应 `results/new-positive-longerror*`。首个失败脚本完整保留；第二脚本只新增case选择，assert AST多重集相同，避免首例短路掩盖第二项状态。`binding.json`与`facts.json`列固定身份、完整源hash、实际退出、分类和NOT_RUN。此记录为PARTIAL/BLOCKED_RESOURCE，不是通过报告。完成本记录后停写，等待下一次唯一修正freeze与另授执行。
