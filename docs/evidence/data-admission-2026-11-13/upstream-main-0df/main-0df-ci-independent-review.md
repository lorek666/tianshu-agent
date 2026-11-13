# main 0df 原生 push CI 独立核对

- 核对者：`/root/verify_lifecycle`；时间：`2026-11-13T11:46:47.998046+00:00`。结论：`PASS_MAIN_0DF_PUSH_CI_EXACT_OBJECT`。
- 本提交首次 push run `43416712660`，attempt `1`，event `push`，branch `main`；最终 `completed / success`，UTC 开始 `2026-11-13T11:23:43Z`、最终更新 `2026-11-13T11:43:53Z`。本结论来自该 run 的原始 API／日志，未以 PR135 绿色替代。
- 四份原始 job 日志的 `git log -1 --format=%H` 均为完整提交 `0df7019aeed4f9116262e38f86561d5eff06d0e4`；当前 main ref 同为该对象。已独立回读、按 hash 保留的不可变 Git 对象显示双亲 `69119f009096df74fc8e370d5d1c6d71cad3f290`、`a38e814577ce721ed747aa40d77637cf10c857d7`，main 与 a38 的完整 tree 均为 `8ce2e0e817144fdca1336ff266b7d047f9cb462a`。
- Ubuntu job `131856335219`：`2434 passed / 13 skipped`，`309.17s`，success。
- macOS job `131856336782`：`2439 passed / 8 skipped`，`555.89s`，success。
- Windows job `131856335391`：`2410 passed / 37 skipped`，`1181.99s`，success。
- UI job `131856334965`：`80 passed / 11 files`；typecheck、build、已提交 dist 一致性步骤均 success。三个 Python 平台 lock／ruff 步骤均 success，各合计 2447 项；跳过原因保留在日志，未算作已测通过。
- 当前 head 仅四个 check，均属本 run 的 suite `117563625881`、GitHub Actions app `15368`；四 required 名称／app 逐项相符，`strict=true`、`enforce_admins=true`、禁 force-push／删除。
- 指定异常标记（pytest warnings summary、未处理线程警告、ResourceWarning、Fatal Python error、Exception in thread、Actions error）在四日志中均未发现。Actions／运行时弃用提示与 runner notice 保留于原始日志和 annotations。
- Windows 有 3 次 60 秒 `PYTHON STACK WATCHDOG` 输出；其后首个 PASSED 分别为 `test_real_user_scope_is_counted_separately_from_contract_cases`、`test_cli_compare_accepts_previous_report_as_baseline`、`test_100_random_fault_injections`，时间与完整栈见 JSON／原件；未将诊断输出称为失败，也未宣称耗时根因或稳定性能。
- 既有合并对象绑定 `/tmp/tianshu-g3-20261113/main-0df-independent-merge-binding.json`，SHA256 `50712a263734dbf781a60ce2e1933d7222bff36b3ae0a4f9398eb5a28ee016df`；本轮仅接续 CI 核对，未重做契约／设计验收。
- 原始证据目录 `/tmp/tianshu-g3-20261113/ci-43416712660`，清单含 24 份原件；manifest SHA256 `f9fcb4abe3264750fb3fdb88a4787e2437fa891bc0314b67db160a7367ed8011`。
- 详细 JSON `/tmp/tianshu-g3-20261113/main-0df-ci-independent-review.json`；SHA256 `a17a3437c45521fe972c7116763e73388d63feb9ee8f48815e03db54e59568b0`。各平台实际 OS／runner image／Python 3.12 补丁版本与完整 job／check／日志 hash 索引见 JSON。
- 范围仅本提交、本 run 与 attempt 1；未重跑测试／CI、修改仓库／CI、吸收 T1/T2 实现树、推送或合并。不外推后续对象、T1/T2/T3 验收或用户设备／未测平台支持。
