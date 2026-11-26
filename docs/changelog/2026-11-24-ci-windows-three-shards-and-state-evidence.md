# 2026-11-24 · Windows 三片 CI 与背压诊断证据（#219）

## 背景与决策

main push run `45078725688` 的两个 Windows 分片累计约 49 分 16 秒；shard0 在剩余少量测试时触及 25 分钟上限并被取消。两片各自接近上限，继续微调文件权重无法提供可靠余量，因此改为三个独立 runner。

本次只增加并行度：分片插件、workflow matrix、汇总下载与验证统一要求三片；保留完整 collection、整文件不拆、互斥全集、包装器最终退出码、原生 fatal handler、60 秒 Python 线程栈、25 分钟上限和四个 required 名称。没有摘测试、`continue-on-error` 或提高超时。

同一 CI 变更一并修复 #219：state 背压测试在首次断言前原子保存完整脱敏 JSON 到 `RUNNER_TEMP`；真实子进程在首个失败谓词后 `os._exit`，证明日志被截断时 JSON 仍完整。macOS 和每个 Windows shard 用 `always()` 上传，工件名绑定 run ID、attempt 与 shard；缺文件仅告警，不改变原测试结论。

## 验证

- `uv run --frozen pytest -o addopts="" -q -rs tests/test_ci_workflow.py tests/test_pytest_ci_shards.py tests/test_slo_state_pressure.py`
- `uv lock --check`
- `uv run --frozen ruff check .`
- `git diff --check`

定向回归覆盖矩阵/汇总负控、完整报告验证、包装器退出码、诊断工件上传，以及 RUNNER_TEMP 存在/缺失两种持久化路径。最终三片 Windows 是否在 25 分钟内完成，只能以托管 required CI 日志为准。
