# T2 本机候选证据

本目录保全 T2 的失败、后续基线重演、定向通过与冻结摘要；范围说明见 [T2 记录](../../DATA-ADMISSION-T2-2026-11-13.md)。没有独立实现批准、全量或原生 CI 结论。

- `red-first.log.txt.gz`：实施前初次 36 failed / 14 passed，含 API 缺失与真实未拒绝。
- `green-first.log.txt.gz`：首轮实现 7 failed / 43 passed，失败与夹具裁定未隐去。
- `red-original-behavior.log.txt.gz`：后续在精确 `585e3474b7f9f03bb118308470fac1f500c3a02e` 源码上重演，11 个真正未拒绝失败。`baseline-behavior-tests.py.txt` 和 `baseline-behavior-source-binding.json` 绑定当时测试与 70 个基线源码；不是当前全部测试或独立审阅。
- `red-preopen.log.txt.gz` / `green-preopen.log.txt`：迁移连接入口 pre-open 缺口的 3 红→3 绿。
- `final-binding.log.txt`、`final-related.log.txt`：最终 90 项迁移与 208 项关联回归。
- `final-byte-io.log.txt`、`final-lock.log.txt`、`final-ruff.log.txt`、`final-diff.log.txt`：额外字节 I/O 门禁与 lock/ruff/diff 检查。
- `final-local-results.json`、`freeze-product-tests.json`：最终命令、退出码、运行环境与四文件哈希；绑定源码/测试不含文档。
- `baseline-files.json.gz`：授权开始时全部跟踪文件的哈希，供核对非授权漂移；不代表该基线的本轮测试通过。
- `author-ledger.md`：实现裁定、失败归因和验证经过。
- `archive-manifest.json`：本目录原件的 SHA256，自身不列入。

复现最终两组命令使用仓库既有 `docs/evidence/data-lifecycle-2026-11-13/run_clean.py` 包装器，底层命令见日志首行。基线行为重演从上述完整 Git SHA 提取 `src/`、`tests/conftest.py` 和 `tests/support.py`，把冻结测试放入隔离目录，确保 `PYTHONPATH` 指向该目录的 `src`，只选择 `foreign_and_stale or projection_only or live_extra_and_saved or fts_optimize or initial_binding_requires`；它们应当 11 红，当前候选相同节点应绿。
