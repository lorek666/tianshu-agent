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

## 验证范围

- 真实 MCP server/HTTP/dispatch/crash 路径验证启动、调用、崩溃与恢复；live、resume、reopen、replay 和 Receipt 使用同一 canonical 内容。
- root-outside `blob_path`、空 locator 的外部 fallback root、目录/文件 symlink 逃逸、畸形 ref、摘要/元数据不符、lone surrogate 与非规范 JSON 负控必须拒绝且不崩溃。
- 旧 inline 兼容、合法 event/snapshot locator、事务失败与 `memory.erase` 保留审计 blob 有定向回归。
- 最终独立复审绑定完整 code/test tree（`src` / `tests` / `schemas` / `ui` 的 tracked + untracked 实际字节，按仓库相对路径排序）SHA256：

```text
f0ccf88cfee6b3ac25f81f968d846ffd101d6a13554b0a4108ac8990db1de892
```

路径解析与 `read_bytes()` 之间仍存在同 UID 替换链接的竞态；本项目按单用户协作边界记录它，不把 canonical containment 表述为物理隔离。

本机 mac-arm64 完整 `uv run --frozen pytest` 已退出码 0；锁文件、Ruff、diff 与文档相对链接门禁通过。托管 required CI 结果以关联 PR 的 Actions 日志为准；本机通过不替代平台验证。
