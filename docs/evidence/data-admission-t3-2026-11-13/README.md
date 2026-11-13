# T3 本机候选证据

本目录保全显式迁移 CLI 的首轮失败、接线后定向与最终冻结；范围和实际行为见 [T3 记录](../../DATA-ADMISSION-T3-2026-11-13.md)。没有 T3 独立批准、最终组合全量、原生 CI 或 Windows 设备结论。

- `red-first.log.txt.gz`：入口尚缺时的 25 failed / 26 passed，7.13s。只证明 parser 尚无 migrate，不作为深层六类协议或互斥负控。
- `red-data-cli.py.txt`、`red-test-data-migration-cli.py.txt`、`red-test-data-cli.py.txt`、`red-source-binding.json`：该次 RED 的精确源/测试副本与 SHA256。首次未到达的 payload 列下标随后按真实 schema 改为显式 SELECT，未改产品掩盖夹具错误。
- `green-attempt-1.log.txt` / `green-attempt-2.log.txt`：最小接线后 51 passed，补三种标准根与两种编程异常后 56 passed；分别为过程结果，未覆盖后续原生平台。
- `final-cli.log.txt`：冻结候选两文件最终 56 passed，9.02s，EXIT 0。实际到达六个独立真实故障及各正控、两子进程屏障、四真实 77 与续做/再调 0、进度/DDL 真实失败、链接、脱敏和异常传播。
- `final-lock.log.txt`、`final-ruff.log.txt`、`final-diff.log.txt`、`final-runtime.log.txt`：lock/ruff/diff 及运行环境原件。宏观运行环境为 Darwin arm64，测试 Python 为 3.12.13。
- `final-local-results.json` / `freeze-product-tests.json`：命令、真实退出码及三份源/测试的前后哈希。临时日志路径为作者原始位置，同名原件保存在本目录。
- `baseline-files.json.gz` / `scope-check.json`：T3 开始时 HEAD + 精确 T2 冻结的 463 项现存文件；最终 459 个非授权基线对象无漂移，24 个非知识库 T2 原件无漂移。知识库去掉两条 T3 新增段落后恢复精确 T2 哈希。
- `placeholder-check.log.txt`：对 T3 源/测试的占位与跳过模式检查。仅有真实平台链接构造失败的 skip；空 context body 只用于打开真实 EventLog 初始化，未添加占位测试、test.skip/.only 或未实现产品分支。
- `author-ledger.md`：范围、RED/GREEN 裁定及未决验收。
- `archive-manifest.json`：本目录各原件 SHA256，自身不列入。

复现最终定向：

```bash
uv run --frozen python docs/evidence/data-lifecycle-2026-11-13/run_clean.py \
  uv run --frozen pytest -o addopts='' -q \
  tests/test_data_migration_cli.py tests/test_data_cli.py
```

`run_clean.py` 仅在测试子进程局部去除项目/provider 配置，不打印值或改全局环境。测试自身子进程通过 `tests/support.py` 明确 UTF-8。链接平台原生不支持时按真实异常记录 skip；本机最终没有 skip，不据此声称 Windows 设备已验。

作者未跑 full、UI 或新原生 CI，未 commit/push。独立第二读者及父会话整合实际 main 后的验证另绑最终内容/提交，不借本目录的本机绿代替。
