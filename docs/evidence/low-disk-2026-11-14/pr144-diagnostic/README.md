# PR144 测试协议修正与诊断：公开子集

这里只发布 `published-subset.json` 逐项列出的原字节。独立报告描述的是完整封存包；其临时数据库、快照、候选副本、源映射和完整索引未公开，不把本子集冒充完整复现环境。原始完整包已保存在本地 ignored 证据档案。

- `first-native-red/` 是旧 head `96e3e485eb4ecbecadbadb54a60414967ab63649` 的 run `43549430150` attempt1：六 job 中 Windows 分片1与汇总失败；采集 `errors=[]` 只表示采集无错。
- `local/` 是该 head 加两测试增量、1030源清单 `2922a34503134aa6674c905dffc016b87a839687cb41ec66dbed6839a1448bc9` 的完整本机门：3183 passed / 8 skipped，lock/full/ruff exit0，源和Git状态前后不变。
- `independent/` 保留第二读者原报告、事实、四条实际执行及最小观察记录。正控2项、强制真实子进程CRLF正控1项、坏READY及reason注入各1条call行为红；复原与静态继承详见原报告。

原候选目标可从本提交重跑：

```sh
uv run --frozen pytest -o addopts='' -q tests/test_low_disk_run_creation.py::test_two_real_processes_reopen_and_compete tests/test_m2_acceptance.py::test_normal_finish_and_pause_do_not_report_heartbeat_failure
```

完整门命令分别为 `uv lock --check`、`uv run --frozen pytest`、`uv run --frozen ruff check .`。独立负控的真实argv、环境白名单和实际加载源在每条 `execution.json`，pytest分阶段结果与stdout按原字节保留。

这不是 Windows 实跑、心跳根因复现或修复。后续正式提交、新原生CI和合并须另绑版本；原失败不销账。
