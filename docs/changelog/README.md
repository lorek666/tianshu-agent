# 变更与会话记录（无冲突碎片目录）

为根除并行分支在单一 `docs/KNOWLEDGE.md` 上的合并冲突，自 2026-11-18 起：

1. **历史归档（只读，完整保留原件）**
   - [archive-2026-11-sessions.md](archive-2026-11-sessions.md)：原 `docs/KNOWLEDGE.md` §8 全部历史会话日志
   - [archive-2026-11-status.md](archive-2026-11-status.md)：原 `docs/KNOWLEDGE.md` §2 逐 PR 状态与 CI 绑定流水
2. **新增记录规则**
   - **Tier 1（高危核心改动）** 或跨模块重要演进：在本目录新增独立文件 `YYYY-MM-DD-<scope>.md`（每 PR 独立文件名，互不冲突），记录背景、根因/决策、验证命令与第二读者结论。
   - **Tier 2 / Tier 3（常规功能 / 纯文档对账）**：直接以规范的 Git Commit 正文与 GitHub PR 描述作为单一事实源，无需重复维护流水账。
   - **不手写易过期数字**：模块总数、测试总条数、平台跳过数一律以当时提交的 `uv run --frozen pytest` 与 GitHub Actions 日志为准，不在文档中硬编码静态总计数。
