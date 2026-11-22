# 2026-11-22 · Prompt Cache 策略与观测候选

基线为 `7665e8684f5a124fe789f21909ee54325edc556b`，工作分支为
`codex/prompt-cache-policy`。本记录保存代码冻结、本机门禁与独立复审证据；
托管 CI 结果以本 PR 最终 head/base、合成 merge SHA 及对应运行记录为准。
真实付费端点命中率与任务质量对照尚未执行，不沿用上一批功能的验证结论。

## 源码核对与实现

本轮源码核实发现缓存用量 v2 与 Responses 无状态续接已经存在。
补齐的是 adapter 本地缓存策略、Chat 原生缓存明细、OpenAI 新版缓存写入字段，
以及复用 C5 真实观察的缓存/质量旁路报告，不重建既有事件契约。

`prompt_cache.py` 在私有 wire 副本上固定工具投影顺序和对象键，保留内置目录次序，
不重排 schema 数组或历史消息。`auto` 仅对官方 HTTPS OpenAI 端点使用稳定 key；
Anthropic 显式断点和新版 Responses 显式模式须主动启用。Responses 保持 `store:false`、
完整 input、opaque item 原值及原有压缩边界，不增加重试或探测请求。

Chat/Responses 的缓存读取与创建分别保存到既有 usage v2 字段，输入总数不重复增加。
旁路报告按官方 provider 语义计算可观测子集分母，未知端点、缺失回执和缺失用量不填零；
C5 安全、完成度及无效结论保持原样，缓存率不能抵消任务失败。

本轮没有修改 C1–C5 对象、事件/schema 版本、授权、预算、价格表、恢复或 CI 工作流。
PRD §7 的本地策略清单、独立 JSON Schema 和代码枚举共同维护；
不是新的模型消息或授权闭合枚举。

## 验证与门禁

红/绿及静态检查原件保存在 ignored `.omc/artifacts/prompt-cache-20261122/`：

- `baseline.log`：旧基线受影响域测试；
- `strategy-red.log`：旧实现缺少缓存策略/原生 Chat cache 读取；
- `metrics-red.log`、`negative-red.log`：缺少报告链路、非法计数被丢弃及失败条件丢失；
- `write-usage-red.log`：缺少原生 `cache_write_tokens` 投影；
- `report-boundary-red.log`：输出冲突未在调用前拒绝及缺少成本/耗时原事实；
- 后续 `*-green.log` / `final-directed.log`：修复后的定向与契约回归。

定向覆盖三协议 JSON/SSE 一致、真实 manifest、递归对象键排序、
输入不可变、稳定 key 与正文变更、最多四断点、opaque 保真、
原生读写 usage、真实主循环落库/重放/重建、未知分母、质量失败和安全门槛。
各次冻结的本机全量、lock/Ruff、独立第二读者及精确提交绑定见下文。
不在文档手写易过期测试总数，以原始执行日志为准。

## 独立复审发现与修复

首轮第二读者对 `78189fa70df51a53d49e83c77f67af281a3d4b5e`
（tree `1112753f582ebd4e19262b18297501a21a329a66`）结论为**部分修**。
原报告与失败探针保留，不能把该冻结视为 PASS；当时启动的全量已主动中断，
`aborted-first-full-*` 保留退出码与绑定，不计作最终门禁。

修复了六类独立复现：旁路目标覆盖评测事件库及路径冲突、模型与脚本恢复任务质量混算、
Responses 初始任务与动态尾部误识别、相邻轮历史查找断点丢失、
开头 system 键序不稳定、官方 Chat 扁平缓存计数逃过输入总量校验。
新增回归的旧树失败见 `review-fixes-red.log`，修后定向与原独立探针复跑分别见
`review-fixes-green.log`、`independent-probes-after-fix.log`。报告目标改为独立新文件，
使用独占创建；质量组保留 `scope`，显式模式保留两个历史位置且最多四个断点。
使用文档中的冒烟工作根另因既有 #204 路径限制失败，已改系统临时目录；
原失败日志保留，未借此宣称修复 #204。

第二轮复审绑定 `723d2b3664f2e838c68eb2e9347670cafd207753`
（tree `4132f7f3016ae6ba2df4df4e187d0e47fea741dd`），结论仍为**部分修**：
在本机卷上，尚未创建的大小写/Unicode 等价输出可互相覆盖；
第三方 Chat 仅增加空或无关 details 就被误套官方总量校验。
该轮全量也已中断并保存为 `aborted-second-full-*`，不计作通过。
新增永久红测见 `review-edges-red.log`，修复后相关回归及全部原独立探针见
`review-edges-green.log`、`independent-all-probes-after-edges.log`。
路径预检现复用完整路径的保守比较键，保留既有硬链接检查；
第三方用量始终校验数值及别名冲突，官方 Chat 和 Responses 保留严格总量检查。
该次实质增量由同一第二读者重新绑定，后续验证记录如下。

第三轮第二读者已将 `840d148460c10e6d9066fc5d4d72a31a071ac11c`
（tree `7751217cad3acbaa4e57d4e3b9d1b311f41f9d88`）的上述阻断判为**已修**。
该冻结的完整本机测试仍因 `test_product_never_uses_text_mode_file_io` 失败：
旁路报告的文本独占写入违反既有跨平台字节 I/O 约定。原件保留为
`failed-first-completed-full-*`，不计作最终通过。定向复现见
`binary-sidecar-red.log`；改为 `xb` 并显式 UTF-8 编码，保持 LF 与独占创建语义，
未修改或放宽原守卫。新增 CLI 原始字节断言，修后已重绑独立复审与最终全量。

## 首轮代码冻结与验证

代码冻结为 `a6f5fa50c9a3109b50a5aa84e47dd60e8d6a497f`，
tree 为 `8f68afea48afc3116ea5f40b4e5427185e96dc98`。
同一第二读者 `prompt_cache_independent_review` 亲自检查增量并重跑原探针、
Windows I/O 守卫与缓存报告回归，结论为**已修（fixed）**。原报告
`independent-binary/REVIEW-a6f5fa5.md` 的 SHA256 为
`273a28c087a6d49b18fee86be68c0fbd6181ea09a0c314d6a76a5f6f281bd353`。

该冻结的 `uv run --frozen pytest` 于 2026-11-22 19:12:32（UTC+08:00）
完整结束，退出码为 0。原日志 `final-full-pytest.log` 的 SHA256 为
`e70148901b74d8cb561934f154ba3eb2bd15237dbc95ffd686791bd3172df36b`，
命令、起止时间、head/tree/base 与退出码见 `final-full-binding.json`。
`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check`
及修改文档的本地链接检查均通过，原件位于上述 ignored 证据目录。

其后回填 Markdown 证据，形成 `656b66a221dc225b191f398f1888fc2d0eb5ea36`；
逐路径比较 Git tree 中所有非 `.md` 文件，相对该冻结零变化。该清单 SHA256 为
`0889a069a0cbaad2135388501f9922587a70da9784aac68fbc29f642ba8549ef`。
该次文档回填由同一第二读者独立核对，复用同一代码树的本机全量与代码复审。

## 首轮托管 CI 与测试读取编码修复

首轮完整候选 [CI 45016750084](https://github.com/lorek666/tianshu-agent/actions/runs/45016750084)
绑定 head `656b66a221dc225b191f398f1888fc2d0eb5ea36`、
base `7665e8684f5a124fe789f21909ee54325edc556b`，实际合成 merge checkout 为
`3b19562db6f89da41400d8ebdf5c406a27757a84`。Windows 分片 1 在测试调用阶段失败，
不是超时；Windows required 汇总正确拒绝，整轮为红。完整日志和两片原始报告保存在
`first-candidate-656b66a/`，日志归档 SHA256 为
`139a471e0e0b581552a191daf814c20f2b9f5d9dd2977920c40a5a14d32e2643`。
其他 job 的通过不能替代该轮完整门禁。

两个失败均是新增测试省略读取编码：CLI 测试读取 UTF-8 旁路报告、策略枚举测试读取
中文 PRD 时使用了 Windows 默认 CP1252。产品报告写盘仍是已经验证的 UTF-8 字节与 LF。
本地只在未声明编码的 `Path.read_text` 上注入 CP1252，原用例确定性复现同类
`UnicodeDecodeError`，见 `windows-cp1252-red.log`。修复仅为测试中的报告、
schema 和 PRD 读取显式指定 UTF-8；原断言、产品实现及 Windows 守卫不变。
同一 CP1252 探针下的定向与受影响回归通过，见 `windows-cp1252-green-*.log`。

修复期间 PR 恢复 draft。测试树变化后重新冻结并绑定完整本机门禁与独立增量复审，
再统一启动修正候选的完整矩阵；没有对旧失败作业直接重跑。

## 修正候选冻结与验证

测试编码修复冻结为 `61839baebdb11227f10e9dbaeb429d8fd542d575`，
tree 为 `1908403f55c497bea11c87e757dab9a79609202d`。
同一第二读者对该增量结论为**已修（fixed）**，亲自复现旧用例的 CP1252 失败，
并在相同探针下核验修复、缓存回归和原独立探针。产品代码、原断言、运行器与 CI
均未改变。原报告 `independent-windows-encoding/REVIEW-61839ba.md` 的 SHA256 为
`a6e5033540ed3dca05e536a3022564b431da9d401b5d2bccd7f9ae901e90a18c`。

该冻结重新执行的完整 `uv run --frozen pytest` 于
2026-11-22 20:04:21（UTC+08:00）结束，退出码为 0；开始和结束的 head 相同。
原日志 `post-ci-full-pytest.log` 的 SHA256 为
`ec975a569d9b22ba7e33a02f7a2ba4c991080ee277c05917fe8033c198fad0d5`，
精确命令与 head/tree/base 见 `post-ci-full-binding.json`。
`uv lock --check`、`uv run --frozen ruff check .`、`git diff --check`
及修改文档的本地链接检查通过，未以首轮冻结的全量替代本次验证。

后续仅回填 Markdown 证据；所有非 `.md` Git tree 条目须与该冻结逐路径一致。
非 Markdown 清单 SHA256 为
`f74effc7e8119ea4270aeb69cf6df0de28d3d151876e1863e57359e1b7c61873`。
文档增量由同一第二读者复核后，复用这一代码树的本机全量与代码复审。
托管结果继续以 [PR #222](https://github.com/lorek666/tianshu-agent/pull/222)
最终 head/base、实际合成 merge SHA 和对应完整 CI 为准；
本机 CP1252 探针不替代原生 Windows，旧红、draft 跳过与部分绿均不计作新候选通过。

## 保留边界

- 没有运行真实付费供应商调用，不能声称命中率、成本或任务质量已经改善。
- `off` 禁用客户端附加字段，不能承诺关闭供应商隐式缓存。
- 没有更改 `/compact`、记忆或摘要的产品行为；仅采集既有条件，不自动组织随机对照实验。
- 无变换证据只能标为 `baseline_or_spilled`，不能认定完全原始上下文。
- 混合条件的任务成功不归因于单个请求；非官方端点的统计分母未知。
- 本记录冻结时语义摘要辅助调用尚未持久化缓存明细；后续增量见
  [2026-11-23 记录](2026-11-23-summary-cache-telemetry.md)，不能据此替代总账或真实实验。
- 本地策略只固定到 adapter 实例，不是历史事件中的逐请求策略事实，设置面板没有新开关。

使用说明：[Prompt Cache](../prompt-cache.md)；设计：
[spec](../superpowers/specs/2026-11-22-prompt-cache-design.md)。
