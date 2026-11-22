# 2026-11-22 · Prompt Cache 策略与观测候选

基线为 `7665e8684f5a124fe789f21909ee54325edc556b`，工作分支为
`codex/prompt-cache-policy`。当前为本地候选；候选 CI、真实付费端点命中率与任务质量
对照尚未执行，不沿用上一批功能的 CI 或独立复审结论。

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
最终本机全量、lock/Ruff、独立第二读者及精确提交绑定待本轮冻结后回填。
不在文档手写易过期测试总数，以原始执行日志为准。

## 保留边界

- 没有运行真实付费供应商调用，不能声称命中率、成本或任务质量已经改善。
- `off` 禁用客户端附加字段，不能承诺关闭供应商隐式缓存。
- 没有更改 `/compact`、记忆或摘要的产品行为；仅采集既有条件，不自动组织随机对照实验。
- 无变换证据只能标为 `baseline_or_spilled`，不能认定完全原始上下文。
- 混合条件的任务成功不归因于单个请求；非官方端点的统计分母未知。
- 语义摘要辅助调用尚未持久化缓存明细；旁路报告单列该缺口，不能代替总账。
- 本地策略只固定到 adapter 实例，不是历史事件中的逐请求策略事实，设置面板没有新开关。

使用说明：[Prompt Cache](../prompt-cache.md)；设计：
[spec](../superpowers/specs/2026-11-22-prompt-cache-design.md)。
