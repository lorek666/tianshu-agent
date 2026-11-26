# 2026-11-26 · Agent 主循环主干同步

[#229](https://github.com/lorek666/tianshu-agent/pull/229) 先合入了 Windows 三片与 state
背压原始诊断。#225 的范围更大，已有 macOS 两片与 Windows 四片及逐分片工件验证。
同步基线时保留 #225 的分片验证器、文件分组、SLO 阈值、25 分钟上限和四项 required 检查；
在每个 macOS/Windows Python 分片复用 #229 的失败诊断写入与上传，名字含 run、attempt 和 shard。

本次同步未改 `src/`、`schemas/`、`ui/` 或 `desktop/`。缺少 macOS 上传步骤时，工作流回归在
`macos_python must upload state-pressure evidence` 断言处失败；补上传后，工作流、分片报告、
工件解析和 state 背压诊断的定向回归通过。新增负控验证不完整、重复或越界的分片索引会被拒绝。
裸全量 `uv run --frozen pytest` 在外置盘短路径隔离根通过，lock、Ruff、actionlint 与
Git 空白检查通过。独立第二读者判定本次增量「已修」，主控核对清单并在新导出完整重放了
报告落盘与上传负控，以及两个合法分片数的逐片 attempt 验证。测试树未因本段回填改变。
托管门禁仍须绑定合入前的实际 head/base 和 GitHub 合成提交，旧结果不覆盖新候选。

本机全量日志 SHA256：`e6fd8e6989e9b2f621dde80a3f82c3bd4064397f26cd974267564414f4d5a550`。
独立证据清单 SHA256：`dd3c9b82dab8b28de9ab4a09bba1b45ef841e14cb85f0b3258ee6550766a8ee8`。
