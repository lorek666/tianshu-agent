# Worker 默认批次 drain 修复

## 问题

`tianshu worker` 默认只调用一次 `Worker.drain()`。当排队 Run 超过单轮上限 64 时，命令返回码仍为 0，但只处理前 64 个作业，JSON 报告 `idle: false`，剩余作业留在 `pending`。

## 修复

- 未指定 `--max-jobs` 时，CLI 按 64 个作业一批重复调用 `drain()`，直到队列耗尽或遇到工作区争用。
- 显式 `--max-jobs N` 保持单轮最多 N 个作业的既有语义。
- `--once` 保持只处理一个作业。
- 增加 65 个真实排队 Run 的子进程回归测试，验证所有 Run 被处理、最终 `idle: true` 且 `run_ids` 不重复。
- CLI 文档和知识库补充单轮预算、默认批处理和 `idle` 语义。

## 验证

本变更的完整验证结果以对应提交和 CI 日志为准；本地命令按 `AGENTS.md` 要求执行并如实记录。
