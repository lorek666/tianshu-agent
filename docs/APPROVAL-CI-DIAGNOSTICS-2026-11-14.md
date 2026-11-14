# 审批后删除未发生：CI 诊断记录

2026-11-14。原故障根因仍为 **NOT_ESTABLISHED**；本片补充失败诊断，不宣称修复审批或删除逻辑。

## 原始失败

PR #146 head `3e9da0bd63399fc86488018605138b54b22fb427` 的首次 [CI run 43559488826 / attempt 1](https://github.com/lorek666/tianshu-agent/actions/runs/43559488826) 中，Windows 分片 0 的 `tests/test_integration_m5.py::test_approval_is_decided_through_ipc_and_run_continues` 唯一失败。实际 checkout 为 `819cc342b8359ce0e45a6b3b4b75907e8829a573`，与 head 同 tree。`resumed=True` 与 `Run succeeded` 断言已过，后续文件不存在断言失败；其后的事件和审批清空断言未执行。该片为 1 failed / 1583 passed / 12 skipped / 1595 deselected（800.66s），Windows required 汇总失败；Ubuntu、macOS、UI、Windows 分片 1 成功。

[issue #147](https://github.com/lorek666/tianshu-agent/issues/147) 单独跟踪本次失败。issue #145 是另一失败节点，不能在没有可复现证据时认定同因。原 run 没有取消或重跑，也不以本机绿色替换其红色。

独立第二读者在固定 77 份源码的临时副本上，用 macOS arm64 / Python 3.12.13 运行原始离线 FakeModel 用例 50 个独立进程，50 次各 1 passed，0 超时。它只证明本机未复现，不能证明 Windows、flaky 或基线归因。原 CI 为 Windows Server 2025 / CPython 3.12.16 runner，不能外推用户设备。详见[原始独立 triage 报告](evidence/approval-ci-2026-11-14/triage-report.md)。

## 本片诊断范围

提交前记录基于 main `8f2a508e68f27efc4100b7466fcd79043341f7fa`，唯一测试改动为 `tests/test_integration_m5.py`，最终文件 SHA256 为 `f138d6c13e3ed2966870626effed5488c776bfd66052c3a8800396b35203f01a`。原 13 条断言、真实 FakeModel/IPC 路径、时限与 finally 清理保持。成功路径不调用新增采集器；失败时在 client 和临时根清理前附加诊断，再抛出原错误。

诊断选择 Run、审批卡及决定、old.txt metadata、真实工具/执行/审批/租约/收据和终态事件；不读文件正文或环境变量，不直接输出采集异常的私有消息。租约使用 SQLite `mode=ro` 查询，不通过 Store 构造初始化 schema。c1 结果、收据及两个终态的短关键字段先于详细链输出，rule_id、resource 在长 error/reason 前；没有增加生产锁、重试、skip、时限或 CI 配置。

每项最多 1200 Python 字符，最多四条关键摘要加 36 条详细事件，总 note 最多 24000 Python 字符；**不是 24KB**。详细链和任意长字段仍可能有损，截断明确标记，截断行不保证是完整 JSON。`events.run(limit=1000)` 只取首个最多 1000 事件的返回页，不翻页；`missing_in_page` 仅描述该页，不能推断整个数据库缺失。独立 v3 报告中的“首1000页”是这一返回页的简称，不能读成请求了 1000 页。字段白名单和既有 redact 不能保证识别所有未知格式秘密；采集也没有通用的墙钟硬上限。

## 受控证据与独立复审

真实删除执行器完成正常校验后，临时探针仅抛一次 ToolNotAllowed。修改前后均由原文件不存在断言在 call 阶段失败，setup/teardown 通过。新增诊断显示 c1 rejected/none_applied、审批已消费、无收据及 run.end=noop → run.finish=succeeded。**这是受控拒绝，不是原 Windows 故障复现或根因修复。** 初版独立读者另实跑受控拒绝和采集器抛错，确认原 AssertionError 对象、断言帧和非零退出保留；v3 对未变原节点、执行方及 except/finally 边界按身份继承，没有冒充重复实跑。

初版独立审发现总截断丢关键尾部、长 error 吞 rule_id；优先摘要版又被独立长链探针发现短 resource 遗漏。两次原 PARTIAL、作者真红和原始输出均保留。第二版本机全量因此由 root 主动中断，pytest exit 2，不算通过；它不是 GitHub 原 CI 的取消或重跑。第二读者自身曾额外要求截断行完整 JSON 解码，其 exit 1 作为过强观察器要求单列，未当作第二个产品缺陷。

最终独立结论为 **PASS_DEFINED_DELTA_SCOPE**，绑定上述最终文件及 manifest `1338d0901d802b5c4c39c6ac48fa3b22b377929f2d29c2a13a03fe31a6803591`：两项新正断言保住 c1/status/effect/error、收据 resource/after_state/intent_seq/call_id 与两个终态；同输入真实导入旧 helper 的两项负控仍重现旧缺口；原成功节点一次通过，collector=0、诊断 RPC=0。原 13 assert 及相关 AST 保持，长链 note 实测 24000 字符 / 66634 UTF-8 字节。见[最终独立报告](evidence/approval-ci-2026-11-14/priority-v3-independent-report.md)；它不批准正式 Git 提交、Windows、原根因修复或合并。

## 最终本机门禁与提交边界

在 1080 项 Git 可见文件的固定输入上，`uv lock --check`、`uv run --frozen pytest`、`uv run --frozen ruff check .` 全部 exit 0：**3186 passed / 8 skipped（544.97s）**。执行前后文件 hash、HEAD 与 Git 状态一致，唯一产品/测试 delta 是目标测试。继承的模型、凭据和 endpoint 环境覆盖在子进程中剔除，只记录变量名；没有真实或付费模型调用。之后仅发布本段事实、知识库与原字节证据，测试/生产/CI/锁文件保持不变。完整命令与结果见[本机门禁原件](evidence/approval-ci-2026-11-14/final-local-gates-results.json)。

以上为提交前的内容与本机证据，不代表正式提交或新原生 CI 已通过；后者须绑定实际提交和对应 GitHub 检查。原 PR #146 首红不重跑、不覆盖，issue #147 仍开放且根因为 NOT_ESTABLISHED，不能与 #145 合并归因。恢复 B 产品及付费模型、十小时 soak 均未运行。

[公开子集索引](evidence/approval-ci-2026-11-14/subset-index.json)逐项记录所选原件的来源、字节数与 SHA256，它不是完整本地封存包。初版/中间失败/最终独立材料和本机门原件分别保留；公开脚本以 `.py.txt` 保存原字节，避免历史探针被当作仓库运行时代码或 lint 输入。
