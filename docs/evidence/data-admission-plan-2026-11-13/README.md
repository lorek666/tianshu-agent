# 数据准入实施计划与独立终审原件

本目录保存 2026-11-13 的最终计划、计划级独立终审和推理探针原件。
先读 [独立终审](admission-plan-independent-review.md)，再读
[实施计划](admission-implementation-plan.md)。结果是
`PASS_IMPLEMENTATION_PLAN_ONLY`，计划阻断 0；不构成产品、迁移 CLI、恢复或 G3 验收。

权威契约为 `ccade5edbe47a66c0a48ad09e0667ee7393c148b` 中的
[data-admission-contract.md](../../data-admission-contract.md)，正文 SHA256 为
`c62dba28c2e4b4c2d17ce100da233ea26c71f8dcea5f4c0611608fbabf2f8584`。
最终计划 SHA256 为
`464849b2ea0f6e5a0dde4b00b4aed00b72d1e51c9af9bdda4d7d38d7eafde875`。
`archive-manifest.json` 记录十二份原件的来源、归档路径、长度和摘要；文件按字节复制，
原报告与 JSON 中的绝对路径保留当次现场含义。

十二原件及说明随后经第二读者[独立归档核对](admission-plan-archive-independent-check.json)，
结论 `PASS_ARCHIVE_ONLY`；该核对文件另按字节保全，不属于上述十二件历史输入，
也不构成产品验收。

## 时间与授权边界

计划与 freeze 中的 `not_started`、`product_implementation_authorized=false`
以及 16 个未完成步骤，是终审时的冻结快照，保持原文。其后父会话已经核对稳定基线
`30b973dfe21ce71f40ccfd7e84412de33d23877a`，并只授权 T1：实际数据库准入。
唯一作者 `/root/executor_data_cli` 在隔离的 `codex/g3-data-admission` 工作树实施
`data_state`、`data_admission`、EventLog 打开前缀及对应测试、历史夹具和事实文档。
T2 迁移绑定与 T3 迁移 CLI 尚未获得分段写权；T1 此时也未通过产品独立验收。
当前执行进展由知识库与 `g3-batched-delivery` 工作状态记录，不修改历史报告冒充新结论。

## 探针与复现

`admission-plan-probe.py.txt` 是作者的规划实验；`admission-plan-independent-probe.py.txt`
是第二读者的独立推理探针。它们只在合成数据上展示摘要、版本与机制必须分别比较的理由，
使用的是当时已有备份及旧迁移器，不能证明未来实现正确。作者 selfcheck 按原件保全，
第二读者没有将其内容作为终审依据。

重现时使用报告绑定的历史产品基线
`f509a53881c372d8d7f4b1e2718130edaff4ed06` 的隔离 checkout 和 Python 3.12。
把 `.py.txt` 复制到临时目录并改回 `.py`；只在临时副本中将 `sys.path` 与输出根
调整为该 checkout 和临时目录，并仅对子命令隔离 `TIANSHU_*`、模型 endpoint/key 环境，
不打印环境值。保留真实脚本文件供子进程启动，不对真实用户数据运行。
原报告列明哪些探针是前轮执行、本轮仅核冻结内容；不能把历史输出说成本次产品测试。

本目录不作为 CI 测试目录。新实现须按 T1/T2/T3 分别取得真实 RED/GREEN、
独立复审与最终候选的门禁证据。
