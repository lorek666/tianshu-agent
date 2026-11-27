# 2026-11-24 · MCP 入站结果 content-ref 持久化

## 原方案 → 新方案

- **原方案**：完整 MCP content blocks 与 `structuredContent` 直接进入事件、运行游标和上下文重放路径；入站正文缺少独立、可核验的内容寻址持久化边界。
- **新方案**：先写 canonical `tianshu.mcp-result.v1` blob，再在事件与 cursor 中只保留 ref、尺寸、摘要、`after_state="mcp:"+sha256` 和白名单元数据。live loop、resume、reopen 与 replay 在重建模型消息前水合；Receipt 独立读取并核验 canonical blob。

## 为什么

MCP 返回正文属于不可信入站内容，可能很大、含非文本 block 或非有限/不可编码值。把完整正文复制进多条审计记录会放大事件与快照体积，也让重放、恢复和 Receipt 各自持有不一致的副本。内容寻址 blob 提供单一持久化来源；事件仍只保存可验证引用，不把引用降级成普通 inline 输出。

## 契约与边界

- canonical JSON 使用严格 UTF-8、封闭 schema 与字段集合，拒绝重复键、非有限数字、非规范编码和超限正文；编码/解码/路径失败统一为 `McpResultError`。
- 显式 `blob_path` 与解析后的候选文件都必须位于数据库数据根内；空 locator 的 event/snapshot fallback root 也受同一根约束，越界、目录或文件 symlink 逃逸均拒绝。
- ref、尺寸、摘要、`after_state` 与白名元数据必须和 blob 一致；任一 content-ref 标记出现时不能回落为旧 inline 放行。
- 旧 inline 事件、cursor 与 Receipt 保持兼容读取；新结果使用 ref-only 形状。
- `memory.erase` 只清理记忆引用，不删除仍由 append-only 事件审计的共享 MCP blob。
- blob 先持久化再写引用；失败不提交虚假事件或 Receipt。副作用结果未知时仍不得盲目重放。

## 原候选的验证范围

- 真实 MCP server/HTTP/dispatch/crash 路径验证启动、调用、崩溃与恢复；live、resume、reopen、replay 和 Receipt 使用同一 canonical 内容。
- root-outside `blob_path`、空 locator 的外部 fallback root、目录/文件 symlink 逃逸、畸形 ref、摘要/元数据不符、lone surrogate 与非规范 JSON 负控必须拒绝且不崩溃。
- 旧 inline 兼容、合法 event/snapshot locator、事务失败与 `memory.erase` 保留审计 blob 有定向回归。
- 最终独立复审绑定完整 code/test tree（`src` / `tests` / `schemas` / `ui` 的 tracked + untracked 实际字节，按仓库相对路径排序）SHA256：

```text
f0ccf88cfee6b3ac25f81f968d846ffd101d6a13554b0a4108ac8990db1de892
```

路径解析与 `read_bytes()` 之间仍存在同 UID 替换链接的竞态；本项目按单用户协作边界记录它，不把 canonical containment 表述为物理隔离。

原候选的 mac-arm64 `uv run --frozen pytest` 退出码为 0；锁文件、Ruff、diff 与文档相对链接门禁通过。这个结论只绑定该段所记的历史代码树。当前整合见[重绑记录](2026-11-27-mcp-content-ref-integration.md)。

## Windows 分片超时二次跟进（2026-11-24）

- PR head `be7948f2f9e2e14adb96c0f2921c02c342f17184`、base `c5fcff0715a02a89704581ffdc0a15c5789705f6` 的 run [45335797049](https://github.com/lorek666/tianshu-agent/actions/runs/45335797049) 中，shard0 完成（pytest 21m44s）；shard1 没有 FAILED/ERROR，但在 pytest 24m47s、约 91% 处被取消。PR 事件的合成 merge `GITHUB_SHA`/checkout 为 `e137eaffa20209ba4d8b690182c1ceb4c6c0b7ca`，其双亲正是上述 head 与 base；shard0 artifact 也绑定该合成 merge，不表示运行了另一份 PR head。因 shard1 未进入 `sessionfinish`，`shard.json` 不存在，上传与汇总按既有规则失败。该记录是超时红，不是通过证据。
- 新增 MCP 节点本身全部进入 shard0，但整文件贪心分组因此重排了 52 个既有文件；旧表也未校准新增的 `test_prompt_cache.py`，其 100 个快节点被默认按慢节点估算。实际完整文件区间与估计的偏差累积到 shard1。
- 现从该轮 shard0 完整日志、shard1 中所有选中节点均完成的文件提取状态区间，按半秒单位向上取整并加每文件一个调度单位；取消文件中未完成的部分不使用。该轮缺失文件回退到完整 run `45055708214`。`test_mcp_content_ref.py` 与 `test_mcp_crash.py` 也使用本轮已实测值。
- 当前完整 collection 的历史回算中，两片文件数与节点数不要求相等，估计权重均为 1437 秒；该值只用于解释分组，不是验收上限或新 runner 的耗时保证。定向分片测试覆盖新增 MCP 文件，验证器仍要求真实上游 success、精确 SHA、完整 collection、互斥全集与逐阶段报告。
- 未删测试、未提高 25 分钟上限、未忽略 artifact 上传失败、未改变 wrapper/fatal handler/60 秒 Python 线程栈。校准提交 `3f334c89c50e86f9a3697e1d318b92d53156031c` 的 Windows 两分片及 required 汇总已全绿；后续纯文档回填仍须由 required CI 绑定最终提交。
- 最终校准树独立第二读者复核为 **已修**：逐文件重算与真实完整区间一致，取消分片只使用全部选中节点均完成的文件，剩余缺失项回退完整 run；PR head/base/合成 merge 身份与知识库条目连续性也经第二次只读复核确认。完整 code/test tree SHA256：`6ff6a699bcbe907afbb6e892df7c140aea75d5c77f8ef492e9a910afec62fa18`。
- 最终树原候选的 mac-arm64 `uv run --frozen pytest` 退出码为 0；定向分片测试、锁文件、Ruff、diff 与文档相对链接门禁通过。
- 校准提交的 [run 45345940207](https://github.com/lorek666/tianshu-agent/actions/runs/45345940207) 已完成且 `conclusion=success`，所有 Python、Rust 与 UI 作业成功。PR head `3f334c89c50e86f9a3697e1d318b92d53156031c`、base `c5fcff0715a02a89704581ffdc0a15c5789705f6` 的合成 merge/checkout 为 `6802dfff254c43ee6f5809338ae3f2d182290a93`，tree 与 head 相同。重新下载的两份 Windows 原始报告均绑定该 SHA，完整有序 collection 互斥全集、逐阶段通过/跳过与 session exit=0 已复核；没有失败、漏报或重复。

## Windows 分片超时最终校准（2026-11-24）

- 文档 head `228e13ffc78e2f9a5f24ca376556e692c7719418` 的 [run 45350527384](https://github.com/lorek666/tianshu-agent/actions/runs/45350527384) 是超时红：Windows shard0 在 pytest 99%、原有 25 分钟上限处被取消，未进入 sessionfinish，也未产出 `shard.json`；shard1 及其余 Python、Rust、UI 作业成功，Windows required 汇总按缺件失败。该轮不能作为通过证据。
- 最终校准 head `3153e3d93c447c4dd6d3845246e3bd65576d6806`、tree `d000f5bc0420ec72e966e69c0a33f4097bf1be6d`，base 仍为 `c5fcff0715a02a89704581ffdc0a15c5789705f6`。七个文件权重只取成功轮与取消轮中完整文件区间的较大值；取消轮里未完成或未开始的文件不参与校准。
- 最终 head 的 [run 45360540032](https://github.com/lorek666/tianshu-agent/actions/runs/45360540032) 已 `conclusion=success`，Python 两分片与 required 汇总、macOS/Ubuntu Python、macOS/Windows Rust、UI 作业全部成功。合成 merge/checkout SHA 为 `76c32c3394de3ee9007ac908c32139f15bd811f3`，双亲为上述 head 与 base，合成提交 tree 与 PR head tree 一致。
- 重新下载的两份 Windows 原始报告都绑定 `checkout_sha = github_sha = 76c32c3394de3ee9007ac908c32139f15bd811f3`、`run_id = 45360540032`、`run_attempt = 1`、`pytest_session_exit = 0`。本地 verifier 确认完整有序 collection 的互斥全集和逐阶段报告，无失败、漏报或重复。实时数量见该轮 CI 的原始分片报告。
- 未删测试、未提高 25 分钟上限、未忽略缺件、未改变 wrapper/fatal handler/60 秒线程栈诊断；两片设计与原有平衡断言保持不变。后续纯文档回填仍需 required CI 绑定最终文档提交。
- 最终文档 head `8528655f969b5158b37ed23710acd623b19aabb7`、base `c5fcff0715a02a89704581ffdc0a15c5789705f6` 的 [run 45530099115](https://github.com/lorek666/tianshu-agent/actions/runs/45530099115) 已 `conclusion=success`：macOS/Ubuntu Python、Windows 两分片、macOS/Windows Rust、UI 与两项平台汇总作业全部成功。该轮只回填证据，未改 code/test tree；最终文档提交自身也已由 required CI 绑定，不沿用校准代码轮的绿冒充文档轮通过。
