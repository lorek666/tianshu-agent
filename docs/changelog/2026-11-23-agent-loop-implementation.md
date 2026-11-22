# Agent 主循环：本地集成与验收

日期：2026-11-23。分支：`codex/agent-loop-implementation`。
状态：主循环本地候选已实现；首轮完整原生 CI 失败已留证，
修复候选 `b7f0f13` 的独立增量复审通过后，全量发现新增诊断测试资源串扰；
最新 `94fd94e` 已通过新的完整本机回归及一文件独立增量复审。
新候选仍须完整 required CI，尚未合入或发布。
规格见 [Agent 主循环](../SPEC-AGENT-LOOP-2026-11-22.md)，
计划见[实施计划](../superpowers/plans/2026-11-22-agent-loop.md)。
契约为 [PR #223](https://github.com/lorek666/tianshu-agent/pull/223)，
实现为 [PR #225](https://github.com/lorek666/tianshu-agent/pull/225)；
PR 页面最新 head/base 与检查结果是远端门禁依据。

## 为什么改

普通工具循环和 Run 成功不能证明用户目标完成。本批将任务输入、步骤依赖、
验收证据和推进边界分别持久化，形成
`Plan → Execute → Verify → Ask/Replan → Finish`。
复用原事件日志、Run/Job、Dispatcher、Grant 与预算账本，不增加运行时依赖，
不把模型输出、技能文本或计划确认变成授权。

## 已实现范围

- **Mission**：通用稳定提示、任务配置、验收条件和精确工具身份快照；
  CLI、桌面、workflow、Goal、恢复与 replay 共用版本化入口。
  历史无 Mission 的 Run 保留原解释，不改历史消息哈希。
  MCP 在真实 claim 后、写事务外发现；pending 输入先持久化，再固定正式 Mission。
  阶段工具清单是冻结上限，与现场身份精确取交集，schema 漂移不自动升级。
- **Plan / Verification**：有向无环图、依赖、预期产物、检查和步骤状态；
  CAS 确认/局部重规划，只失效受影响证据。独立验证 Run 默认不调模型，
  文件、哈希、来源、真实命令退出码和人工决定各有对应证据；命令仍走 Dispatcher。
  模型“已完成”不能写通过事件，人工入口不能覆盖机器失败或未知副作用。
- **Goal V2**：手动与有限自动推进、持久化阶段和幂等 Run/Job；
  累计请求预算与父子账户、绝对期限、轮次及无进展上限。
  每轮目标、有效完成项、剩余项和阻断来自事件/验收事实。
  暂停、取消、人工恢复及完成入口均校验精确状态修订。
  旧 G1 不自动升级，也不能通过 G1 关闭入口绕过 V2 验收。
- **有限授权**：认证人类预览、签发、查看和撤销。
  支持受限目录覆盖及完整参数绑定的精确命令；按次数、字节、期限约束，
  在执行侧原子消费，派生单动作 Grant，执行前再核验。
  单个 Run 之外不继承；完全访问保持禁用。
- **执行准入 / MCP 生命周期**：绑定实际程序、参数、环境、工作区、数据根、
  后端、策略及现场探针。MCP Session 按 Task/Run 隔离，在发现前准入，
  暂停/取消/终结安全点及空闲 Worker 收尾。未知调用不自动重放，
  清理未知保留资源阻断。远程 MCP 不借用本机进程隔离结论。

## 桌面入口

1. 在新建输入框使用 `/goal`，或在任务面板选择“建立连续目标”。
   创建只保存配置，不启动模型、命令或 MCP。
2. 填写验收条件，选择“每轮手动继续”或“有限自动推进”。
   默认上限为 3 轮、32 次累计模型请求、30 分钟、连续 2 轮无进展；
   桌面使用人工完成与确认重规划策略，金额不因此成为硬保证。
3. 显式推进后审阅并确认结构化图。执行与验收分为不同 Run，
   人工检查到达待审状态后才可决定；页面展示证据与阻断原因。
4. 需要有限授权时，在“有限范围授权”选择目录覆盖或已有命令请求，
   核对预览、次数、期限及绑定后签发，可在同处撤销。
   暂停/恢复/完成仍走真实 RPC，刷新后从事件读取。

新 UI 与旧 G1 历史面板并存；`/compact`、正文计划和 Skill 命令保持各自入口。
Skill 只作为本轮输入，不自动安装或执行引用脚本。

## 独立复审发现与修复

独立第二读者在隔离工作树使用真实 ControlPlane、Worker、Dispatcher、
事件库、文件、Receipt 与临时数据根发现问题；ScriptedModel 仅提供模型选择。
已修复并在 `3d21e34c14c2294c377cfbd4d13e3bac2b1216f5` 冻结上独立重验：

- Evidence、嵌套观察或完整命令日志丢失后，旧步骤会失效；Goal finish 和人工
  decide 重核材料及事件绑定，缺失时拒绝且不写成功事件。
- 多步写入按 `(call_id, tool_id)` 核对原生 intent/result。文件验收只绑定目标、
  路径祖先、workspace 身份和 Task 修订，无关下一步写入不误废前一步；
  改目标、换 workspace 或在命令后改源码仍会失效。
- Consent 消费、撤销及余额从事件重建。V1 issued 没有单调期限锚点，
  投影全丢后原授权保守过期，不能按回退墙钟重授 TTL。
- 子任务真实人工否决沿 Goal 父链传播。第一次 recover 持久取消旧父子 Run，
  安全终结后第二次显式 CAS 才能解除精确否决；不重置轮次、预算或期限。
- 正式及 pending Mission 正文进入 GC 保护与备份引用核验，
  缺失或篡改材料不能发布备份。

原独立 PASS 绑定 tree `74234b762ab6365bef0cb80e8d19c92143f929be`，
非文档清单 SHA-256
`8caa1380cad62784389d08238dbdb85b9a1d6faafd59eee53d355b96357ac702`。
此后的评测修复已在 `b3d485d53d90437916939f126c4beeb17429a01d`
完成窄增量独立复审，结论已修 / PASS，绑定 tree
`7b1fb1057e6fd74fa7916135915219c084fe91ad`，非文档清单 SHA-256
`90bf1cbf22ee09f67dc55c5df15921d31ca9d2fd03c915e577cf3794a68458d6`。
这份结论不覆盖后续修复树，也不代表全量或远端 CI 已通过。

`b3d485d..ac02f34` 的最终窄增量复审已完成，结论 PASS、无阻断回归，
绑定下文 `ac02f34` 的 tree 和非文档清单。第二读者在独立工作树重跑受影响域，
并以相同探针对照旧 SLO 生产函数、旧进程身份解析函数；旧函数均退出 1，
当前函数通过。新的 MCP 探针核对真实 `model.intent/model.result` 空集，
再驱动同一 Run，验证真实启动标记出现；原生 Seatbelt 拒绝与清理也重新验证。
父会话已逐字节核对报告、绑定和全部引用证据，没有把复审自述替代原件核验。

最终报告 `incremental-ac02f34-review.md` 的 SHA-256 为
`f9f847a30f9ee9b0cc49fb52be8cb4de3b139983c3c20321b9761c1b3e1d1741`；
绑定 `incremental-ac02f34-binding.json` 的 SHA-256 为
`0726b6063e84e80d8b2a0c691107307ec14258dac338575860825430b4a133ad`。

**未修的非阻断建议**：正式低盘 control 测试仍有一处不存在的 `model.request`
空集断言，应改为真实事件名；由[工单 #226](https://github.com/lorek666/tianshu-agent/issues/226)
跟踪。该测试的模型调用计数、pending 作业和真实 MCP 启动标记断言仍有效，
上述独立探针另补真实事件及驱动正控。按冻结规则延后此 P3，不写成已修；
平台隔离未决项也不因复审 PASS 而消失。

## 全量发现的回归与修复

`b3d485d` 的完整回归已真实退出非零，原件完整保留，未以中断或重试覆盖：

- 低磁盘 RPC 与授权竞争夹具的 DB、data root 异根，却重开旧事件 blob 目录；
  写入与重开统一使用真实 `default_material_paths()`，生产材料准入不放宽。
- 只读写提案与子任务记忆写提案改在 Mission/Run 路由提前拒绝。测试改核对真实
  模型提案、固定路由、core 未派发事件和无副作用；另加绕过路由的 Dispatcher
  拒绝检查，保留真实子进程屏障、撤销先后和内层记忆执行器复核。
- 控制面入队不提前发现 MCP。用真实对端启动标记核对零启动、零模型调用；
  驱动阶段发现仍由独立 Mission/MCP 回归覆盖。
- SLO 崩溃生产者与恢复 daemon 的事件材料目录不一致，导致恢复等待超时；
  测量脚本与 daemon 使用同一实际布局，不延长超时或放宽验收阈值。
- 补齐原生审批测试夹具的实际 data root；进程身份和 PID 文件改用字节读取，
  遵守跨平台文件纪律，不改变准入或授权策略。
- PRD 规则表补齐 `c4.scoped_consent` 与 `dispatch.consent_unusable`，
  已经独立[契约 PR #223](https://github.com/lorek666/tianshu-agent/pull/223)提交，
  并整合进实现分支 ancestry。

修复后的受影响域、SLO/短时浸泡、Mission/MCP、执行准入、有限授权和材料回归
均已退出 0；修复中间出现的测试夹具错误也保留原件，不冒充成功。

## 前轮验证与版本绑定

本地原始证据保存在 ignored `.omc/artifacts/agent-loop-implementation/`；
独立原件保存在独立工作树的 `.omc/artifacts/agent-loop-final-review/`。
运行数据库、凭据、日志和截图不提交到公开仓库。

- 代码候选：`ac02f34f4bb7e64fbc45e284bae8f2e4f5eea581`，
  tree `aa3edd0e90560aa5da571eaa6bc5a868162ebc6f`，
  非文档清单 SHA-256
  `c80a7c3c83e82e86d44e2b652aa64b29b63e1f9f825f13004e6153f06f24585d`。
  定向原件为 `mission-regression-repairs-green-v2.log/.exit`、
  `execution-material-domain.log/.exit` 和 `rule-docs-green.log/.exit`。
- 全量：`uv run --frozen pytest` 在此代码冻结上完整结束，退出 0。
  原件为 `full-ac02f34.log/.exit` 与 `full-ac02f34.binding.json`，
  日志 SHA-256 为
  `b971205c799fa66a3ec414713256eba06ce6cde69dc5c8784e9fb98ad1235a53`。
  动态总计以原件为准，不在本文复制静态测试总数；
  平台适用性与跳过原因另见对应定向及 CI 日志。
  此前 `b3d485d` 的完整红测和 `3d21e34` 的中断红测分别保留，
  不能将其或只跑到末尾但缺退出码的日志称为全量通过。
- 静态门禁：`uv lock --check`、`uv run --frozen ruff check .`、
  `git diff --check` 已退出 0；普通文档回填后再核对链接与非文档树。
- UI：`016a4996568e960d049559724c7c7e77e55f8fda` 上已执行冻结安装、
  typecheck、test、build，均退出 0，dist 随提交保存；
  后续本批未改 UI/Rust。
- 新评测为 `m4.7` / `RU-D1@1.1.0`。只读 Mission 的早期路由拒绝须绑定
  真实政策、快照、模型提案及 core 未派发事件；对应负控必须红。
  与 `m4.6` 等旧报告不可比，详见 [M4 验证记录](../M4-verification.md) §7.5。
- `b3d485d..ac02f34` 的新代码增量独立复审已通过；最终候选的 GitHub required CI
  尚待完成。不能用其他 PR、较老 SHA、本机绿或 draft 跳过记录替代。

## 首轮原生 CI 失败与修复候选

首轮完整 run `45051156861` 已结束。Ubuntu Python、UI 及 macOS/Windows Rust
成功；macOS Python、Windows 两个 Python 分片及其 required 汇总失败。
原件、失败退出码及中间失败均保留，不取消其他平台或以重试覆盖。
该轮绑定 head `1610c7bee8cbe28b5feac5bc1c840f4d964138c7`、
base `bd3c88cdf71326bef3f3ae93d465a15801e4a76b`；
合成 merge 及已核验的 UI 实际 checkout 为
`b2903e90f6b6dae3072a47ee5665461d8e98022b`，其 tree 与该 head 相同。
PR #225 已退回 draft。

失败原因及修复：

- NTFS 的 `st_dev/st_ino` 可能超过 JCS 安全整数范围。仅在指纹哈希输入中，
  将超范围身份无损编码为十进制字符串；安全整数保留原表示，不截断或浮点化，
  不放宽 canonical 对越界 JSON 数字的拒绝。
- 中文夹具与历史 JSON 不再依赖系统默认文本编码，改用 UTF-8 字节。
  独立进程的 cp1252 探针保留修复前失败与修复后通过原件。
- POSIX IPC 在并发关闭后可能使用已被复用的描述符。真实 socket、子进程与
  屏障已复现旧读取偷走 metrics 文件前四字节，以及旧发送覆盖其他文件。
  生命周期锁内复制私有 I/O Connection，阻塞期间释放锁；close/abort 借用原
  socket 做 shutdown 后 detach，再关闭原连接，不在关闭时申请新描述符。
  已关闭句柄和描述符耗尽的清理缺陷亦经红绿修复；metrics 坏行检查没有放宽。
  Windows 原生 AF_PIPE pending/cancel 分支未改，不能据此声称其 HANDLE
  复用竞态也已修复，原生结果仍须新 CI。
- soak 的在线订阅改由单独线程独占连接并持续消费，不等 effects 终态再读。
  真实 Host/Daemon 屏障探针验证完成前已消费模型流，断线和缺流负控必须失败；
  线程无法收尾也必须非零。B4 故意慢订阅、严格游标补读和 watchdog 保持原样。
  首轮 Windows 日志未提供 drop reason，不把延后消费的已复现机制冒充该次
  `send_stall` 的直接诊断。

修复代码冻结为 `b7f0f133f3080c5aa9565343504bd8cfd5e3bb3f`，
tree `bf4685aa2e0e01a36586965ef08222ebc0303291`，
非文档清单 SHA-256
`0a90fc48b9f3bbcacccfc068d632fba7d8d18ed9e1a0ca418f412f3d28c0db34`。
本轮 IPC/共享连接/慢订阅/metrics、SLO/soak、Plan/Verification/Goal/Control
受影响域及 cp1252 探针已退出 0；lock/Ruff/diff 已退出 0。
完整本地回归 `full-b7f0f13.log/.exit` 已真实退出 1，原因见下节。独立第二读者
`/root/agent_loop_incremental_review` 已完成正式窄增量复审，结论已修 / PASS，
范围内无新增阻断；父会话已逐字节核对报告、绑定、全部证据、真实旧源码负控退出码、
最终绿色、原始 diff、工作树身份及非文档清单。
安全整数旧字节、各目录/文件身份分量、关闭幂等、线程收尾异常及 B4 保持性均有
独立探针；完整工作区仅绑定文件内容而不绑定文件 inode 的既有范围被明确保留，
对应跳过没有算作通过。
报告 `incremental-b7f0f13-review.md` 的 SHA-256 为
`c1af2504eef80b85f781916ba3ac78d66804cfa56273151729ecdc05e328976e`；
绑定 `incremental-b7f0f13-binding.json` 的 SHA-256 为
`bfd44bb25726c5c346e9401e565e88427c7b6757e12f4819e3fc12b44f788a20`。
旧 `ac02f34` 的全量/独立 PASS 不覆盖该新树。
本轮红绿及静态原件哈希收录在 ignored `ci-repair-b7f0f13-evidence.json`。

### 全量发现诊断测试资源串扰

`b7f0f13` 全量仅在新增在线 soak 的成功臂失败，日志 SHA-256 为
`2b9459ae6da78f76a87b54c80a30058d241e6cd5c6eab4d66ac9eb1fdf4de74e`。
原始诊断报告、采样及状态已即时复制留证：失败是
`runner_open_fds_max_exceeded: 743 > 512`，FD 增长为 0，
Host/Daemon 及全部工作负载通过。测试直接在长寿命 pytest 进程运行诊断，
把此前用例累计的描述符算进了 runner 的绝对上限，不能把该全量写成通过。

新增真实父进程描述符噪声回归，先在原测试结构下复现失败；
再将在线探针放入独立 Python 解释器，关闭继承描述符。
真实 Host/Daemon、流消费屏障、断线/缺流负控及全部诊断资源检查均保留。
没有提高上限、主动 GC、删断言或更改生产脚本。
`ci-soak-runner-isolation-red.log/.exit` 为退出 1 原件；
修复后完整 SLO/soak 域和 lock/Ruff/diff 均退出 0。

新测试树冻结为 `94fd94e5b07d937ea8ac04e3ce34e2aa41578ee7`，
tree `da8081a0cbf817ea91399341d7c24e2bf78906cf`，
非文档清单 SHA-256
`96d9f3235c753a525e4180a83d99e6b5b23d42922110b40ee8d4e1b52b9b708c`。
产品代码相对 `b7f0f13` 未改，但测试树已变化，因此不沿用其全量或增量复审结论；
新完整回归 `full-94fd94e.log/.exit` 已真实退出 0，日志 SHA-256 为
`169913a2f09c12194b9286c362190a7d3a8f831728a4984b3f67ea37a37f1acd`。
`full-94fd94e.binding.json` 已记录实际退出码和结束时的非文档清单核对；
执行期间仅上述普通 Markdown 文档变化。lock、Ruff、diff 和文档本地链接检查
均退出 0；后续仅文档提交须再核对非文档清单相等，远端 CI 仍绑定最终 PR 对象。

独立第二读者 `/root/agent_loop_incremental_review` 对 `b7f0f13..94fd94e`
的一文件正式窄审已通过，范围内结论为已修 / PASS，无新增阻断。父会话已重新读取并
逐字节核对全部证据、命令日志、历史失败副本、旧版冻结回执、原始 diff、
复审工作树 HEAD/tree/clean 和非文档清单，接受回执为
`94fd94e-independent-review-parent-verification.json`。
旧 wrapper 父进程 FD 污染、真实子进程执行旧 soak、子进程自身 FD 超限和关闭
`close_fds` 后继承哨兵的负控均真实退出 1；最终独立探针、受影响域及静态检查退出 0。
本轮独立探针最初的 AST 引号比较错误已保留失败原件，仅修正探针的结构比较后
重做负控和最终验证，没有修改产品以迎合探针。
报告 `incremental-94fd94e-review.md` 的 SHA-256 为
`fdab9ebfc65223dea35e095edaf418288d0217d993ae16f1771e211dfbf883dc`；
绑定 `incremental-94fd94e-binding.json` 为
`dbd6d6b14fd5ce676fba61667afd1f8e1782c6847457567ab1901317ddb3ad37`；
重读校验回执 `incremental-94fd94e-artifact-verification.json` 为
`cac33a1529f4500945ab50c8aed0eb835e2c7a23c27c62916b1deedbd2433cd5`。
该窄审不代替本机全量和 hosted CI；Windows 原生运行仍未评估。

## 未交付与未验证

P0-E 未整体交付，剩余范围由[工单 #224](https://github.com/lorek666/tianshu-agent/issues/224)
跟踪。Windows 原生隔离后端与整树监管仍未实现；
macOS Seatbelt 的真实脱组后代可以存活，`process_tree=not_enforced`。
当前自动命令/本地 MCP 要求完整四边界，证据不足会在启动前拒绝；
这是拒绝路径通过，不是完整沙箱支持。Linux 或其他对象须有自己的新原生证据，
不能套用命令探针到 MCP，也不能回落 `local` 继续自动执行。

真实付费模型驱动 UI、真实第三方 MCP、Windows/ARM64 用户设备、
原生安装体验及对应平台完整自主执行尚未验证。受管 Session 的本地夹具
和普通受信任人工路径不证明恶意进程被完整隔离。
没有开放无界循环、完全访问、模型自批或绕过未知效果核查。
