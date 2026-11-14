# PR148 合并事实与 main 首次 push CI 独立核验

**结论：FAIL_THIS_MAIN_PUSH_CI_ONLY。** 合并对象事实核验通过；main 自身首次原生 push CI 失败，不能签 PASS。当前证据审计完整，未将原始失败归类为 flaky、Windows 专属、基线失败或旧 issue 的同因，也未建立产品根因。

## 固定对象与只读范围

- PR：148；原 PR head：`25b683b19ebf6c0d8dd8c5bc6a38f1d0c6f582e6`。
- squash 合并 commit：`b450c339d546ae2007ed9754e70de0108c4b86c4`。
- 唯一 parent：`8f2a508e68f27efc4100b7466fcd79043341f7fa`。
- tree：`acc6e73a9a49b697c2c01afcedfc752b8d3376ae`，与先前已审 PR tree 一致。
- 合并时间：`2026-11-14T06:38:03Z`。
- 原生 CI：run `43567298082`，attempt `1`，event `push`，branch `main`，checkout `b450c339d546ae2007ed9754e70de0108c4b86c4`，无 previous attempt。
- 终态：`completed / failure`，更新时间 `2026-11-14T06:54:18Z`。
- 独立 fresh GET 窗口：`2026-11-14T06:58:26.798369Z` 至 `2026-11-14T06:58:28.912548Z`。

只读核对 GitHub GET 原件、原生日志、artifact ZIP 与固定 Git 对象。未改仓库/live/refs/远端；未 rerun、cancel、merge 或另发 collector；未执行产品、旧行为、本机 full 或付费测试。PR146 的旧 run `43559488826` 未触碰。

## 合并与身份核验

`merge-facts.json` 保留先前完成的合并事实核验：squash 请求/响应、fresh PR/main/commit、实际唯一 parent/tree、先前 PR tree 一致。此次再次对齐 fresh PR、main、Git commit 与六份原始 checkout 日志，并独立复算本地 Git commit SHA1。

六个 job 和六个 check 精确对应本 run/attempt/head，来自 GitHub Actions app `15368`，绑定同一 suite；没有用相邻 PR 的绿色 CI 代替 main 本轮结果。四项 required context 保持原名/app 绑定，strict、enforce_admins 仍开启，禁强推/删除，required approving review count 为 0。fresh main 仍为上述 b450；后续 main 推进不在本报告范围。

固定 CI 输入均从 b450 对象提取：

- `.github/workflows/ci.yml` SHA256：`85a6aeb82363810a78ce778e55370c45b6c2757833cff296971e6606b5c26d40`。
- `scripts/pytest_ci_shards.py` SHA256：`b5f7d48bec96b95ed6b030cc062984a1c2311e0921c82548f74dd33f6b35f57f`。

## 六个原生 job 的终态

- `windows shard 0 · py3.12`，job `132363808689`：success；1579 passed、18 skipped，session exit 0。
- `windows shard 1 · py3.12`，job `132363808565`：failure；1577 passed、1 failed、19 skipped，session exit 1。
- `test (windows-latest) · py3.12`，job `132367655364`：failure；汇总验证收到 `UPSTREAM_RESULT: failure`，拒绝准入并 exit 1。
- `test (ubuntu-latest) · py3.12`，job `132363808866`：success；3181 passed、13 skipped。
- `test (macos-latest) · py3.12`，job `132363808729`：success；3186 passed、8 skipped。
- `ui · typecheck / test / build`，job `132363808724`：success；固定 HEAD 的 13 个测试文件全部对齐，91 tests；typecheck/test/build/dist 检查各步骤成功。

失败分片的 pytest 步骤失败、上传原始报告步骤成功，setup-uv post 步骤 skipped；汇总 job 的验证步骤失败、setup-uv post 步骤 skipped。其余步骤按原件核对。注解总计 warning 6、failure 2、notice 4；两条 failure 均保留 exit 1 原文。

## 精确失败证据

失败 node：`tests/test_m2_acceptance.py::test_normal_finish_and_pause_do_not_report_heartbeat_failure`。

artifact 三阶段：**setup passed / call failed / teardown passed**。固定源码 `fixed-source/tests/test_m2_acceptance.py:726` 要求 `run.state == "succeeded"`；原始日志 `native-originals/job-132363808565.log` 的 1-based 行：

- 1355：该 node `FAILED`。
- 1820：`AssertionError: iteration=55, worker.errors=['OperationalError: database is locked'], run.end=[], events=['run.start', 'job.enqueued', 'run.state', 'job.claimed', 'job.parked']`。
- 1821：`assert 'queued' == 'succeeded'`。
- 1826：该 node 的 `46.66s call`。
- 1864：`1 failed, 1577 passed, 19 skipped, 1597 deselected in 753.14s`。
- 1865：`Process completed with exit code 1.`。

该失败分片没有 `PYTHON STACK WATCHDOG`。这是一条 call 阶段断言失败证据，不是 timeout 结论；`database is locked` 是断言中记录的 worker 错误，尚不足以确定锁竞争来源或根因。

汇总原始日志 `native-originals/job-132367655364.log:238` 明示 `UPSTREAM_RESULT: failure`；240 行明示 `CI shard verification failed: Windows shard jobs did not all succeed`；241 行 exit 1。汇总没有把上游红灯包装成成功。

## artifact 完整性与诊断边界

两份 ZIP 的服务端大小/digest、CRC、唯一成员 `shard.json`、run/head/attempt/shard 身份均一致。各分片 selected=finished=1597；同一 full collection 为 3194 个 node、123 个文件；按固定规则独立重算文件分组，互斥且并集完整。9549 条阶段报告无缺失、无重复，顺序与 raw 逐项结果对齐；Actions 遮罩的 3 个参数身份在 `facts.json` 逐项保留，由同源 artifact 补全身份。

- shard 0 artifact `14103134336`，131164 bytes，4774 条阶段报告，SHA256 `41436249ad161f3378332218ede72e5cbbf56ec0a0d5344f287ed7bd27f76d70`。
- shard 1 artifact `14102774881`，132968 bytes，4775 条阶段报告，SHA256 `b63627914506e4b90ce4b10a18035de3b75ddf6a1d920237aea21e3cf7da5ac3`。

Windows 总计 **3156 passed / 1 failed / 37 skipped**。原始逐项结果、摘要、skip 原因、警告及阶段报告已对账，不能把完整跑完等同通过。

审批 node `tests/test_integration_m5.py::test_approval_is_decided_through_ipc_and_run_continues` 本次三阶段均 passed（分片 0 raw 902 行）。固定源码的审批失败附注入口只在该测试捕获异常时调用；六份日志均未出现 `M5 approval diagnostics:`。因此本次失败没有命中 PR148 的审批失败诊断，不能据此验证该 hook 在真实审批失败中的效果。

成功分片 0 有三次 watchdog，起始行 598、691、1583；其后对应 node 分别为 `test_real_user_scope_is_counted_separately_from_contract_cases`、`test_cli_compare_accepts_previous_report_as_baseline`、`test_100_random_fault_injections`，均 PASSED。完整栈保存在 `facts.json`；不能将这些栈移作失败分片的证据，也不能外推性能或 C 扩展占 GIL 时的 60 秒栈保证。

## 成功准入与未决项

未修改已有成功准入守卫。`audit_failure.py` 的独立红灯事实审计实际调用并确认四次拒绝：run 非 success、六 job 非全 success、upstream failure、即使假设 upstream success 也被 shard exit 1 拒绝。审计脚本 returncode 0 仅表示红灯事实及拒绝核验完成，**绝不表示 CI 通过**。准备阶段的 `verify_native.py` 是成功路径草案，没有用于签署 PASS。

issue #147 根因为 **NOT_ESTABLISHED**；issue #145 **未修**。旧 PR146 红灯继续保留。先前 PR148 的绿色原生 PR CI 仅覆盖其自身 run，不覆盖本次 main push 红灯。未授权产品或测试修复，故没有开展修复。

Windows 证据只覆盖本次 Microsoft Windows Server 2025 x86_64、CPython 3.12.16 CI，不能宣称 Windows 10/11、ARM64 或桌面体验实测。

`PREPARATION.json`、`merge-facts.json` 的 pending 字段和 `early-failure/finding.json` 是先前时点的历史快照；本报告与 `facts.json` 记载最终结论。精确封存文件集、大小、SHA256、模式见 `index.json`；原件复制来源与回读记录见 `native-copy-provenance.json`，实际核验命令、退出码及脚本 hash 见 `audit-run.json`。
