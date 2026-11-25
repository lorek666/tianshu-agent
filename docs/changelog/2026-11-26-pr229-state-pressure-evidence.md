# 2026-11-26 · 在 #228 四分片拓扑上保留 state 背压诊断（#229）

## 背景与决策

#229 提出把 state 背压报告在首次断言前原子落盘，避免 runner 取消或 pytest 日志截断后只剩缩略断言消息；它同时把 Windows 改成三个直接 runner / count3。该拓扑与 #228 已验证的两台 runner、每台两个隔离 worker、count4 严格报告布局冲突，不能直接叠加。

本次只吸收诊断能力：保留 #228 的 runner 数量、真实子进程并发、报告归属、严格 verifier、SLO 阈值、25 分钟上限、60 秒 Python 线程栈、原生 fatal handler 和四个 required 名称。完整 D7 脱敏 JSON 写入 `TIANSHU_SLO_DIAGNOSTICS_DIR/state-pressure/<shard>.json`，位于严格报告树之外；macOS 与 Windows 均用 `always()` 上传，工件名绑定 run ID、attempt 和 shard。缺件只告警，不覆盖原退出码。

## 验证

- 新增 CI 上传约束先在缺失 macOS 上传时真实变红，再实现转绿。
- 真实子进程在首个失败谓词后硬退，验证日志不完整时 JSON 仍完整；覆盖本地回退、Windows shard 0 与 shard 3 路径。
- 私有副本把原子替换目标改错后，两个 CI 路径均因预期报告文件缺失而变红；恢复原文件后转绿。
- `uv run --frozen pytest -o addopts="" -q -rs tests/test_ci_workflow.py tests/test_slo_state_pressure.py tests/test_pytest_ci_shards.py`
- `uv lock --check`
- `uv run --frozen ruff check .`
- `git diff --check`
- 独立第二读者在私有副本复核同一代码树（SHA256 `702f5b9752dab4e3761ff25898e5a91f7fd0185c4da842f209c0ccd5cb170cf9`），结论为「已修」；scoped 套件通过，原子落盘 mutation 按预期缺失 JSON 而红，恢复后转绿。主工作树复核后代码零变化。

hosted required CI 必须绑定最终 head/base、合成 merge SHA、run ID 和 attempt，结论记录在 PR 正文；本文件不预填尚未发生的托管结果。
