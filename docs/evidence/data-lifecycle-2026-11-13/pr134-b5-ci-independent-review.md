# PR #134 最终 b5 · 独立 CI 证据复核

日期：2026-11-13。第二读者 `/root/verify_lifecycle`，全程只读 GitHub；未改两工作树、未重跑CI、未批准或执行合并。

**最终 head `b5b5c7fed03f3bd7c75ce759708e383e5ec46091` 的 run `43409722913`（attempt 1）四项 required 均 success。该结论仅覆盖此 head 与实际被测 merge tree。**

四份原始job日志已收齐并逐项核 checkout：实际为 `3de38bcdfbf80d12bdebdf7a5c651c44909f4ebe`，父为 main `f2aef9da75d2e546af99baa523d196d1e099c196` 与 b5；GitHub Git对象API给出的 merge tree 与 head tree 均为 `7709aeeac0a0a55bb620867580f452f1940f2828`。不是把不同候选或历史绿色移用到当前提交。

本轮实测摘要：

- Ubuntu：**2434 passed / 13 skipped，278.86s**；Ubuntu 26.04.1 LTS，Python3.12.16。
- macOS：**2439 passed / 8 skipped，650.61s**；macOS26.7.2、arm64 runner，Python3.12.16。
- Windows：**2410 passed / 37 skipped，1061.41s**；Windows Server2025，image windows-2025-vs2026，Python3.12.16。首次Windows CI失败的备份UTF-8读取测试在本原生日志明确PASSED。
- UI：**80 passed / 11 files**；typecheck、build及`git diff --exit-code -- ui/dist`均success。

三个Python平台均合计2447项；平台与opt-in跳过逐条保存在原日志和JSON，未当作已测。全部job步骤成功，但日志并非没有提示：四job保留Node20/Actions强制Node24警告与DEP0040/DEP0169弃用提示，Ubuntu/UI有镜像迁移notice，macOS有arm64容量notice。未发现pytest warnings summary、线程/ResourceWarning、fatal或`##[error]`标记。

Windows有两次60秒`PYTHON STACK WATCHDOG`，分别对应baseline compare（80.79秒）和100随机故障注入（68.29秒），随后均明确PASSED；原堆栈完整保留。这里不把长诊断误称失败，也不据单轮成功宣称性能根因已明或稳定SLO。

结束后回读保护：四required精确匹配当前check runs，均绑定GitHub Actions app15368及本run check suite；`strict=true`、`enforce_admins=true`，禁止force-push/删除，批准人数配置为0。PR head仍b5、base仍f2，OPEN/未合并、MERGEABLE/CLEAN。正式review为0、全部review thread为0；这只记录未决review状态，不把“无人review”说成已有GitHub批准。

原始证据目录：`/tmp/tianshu-g3-20261113/ci-43409722913`；hash清单 `/tmp/tianshu-g3-20261113/ci-43409722913/evidence-sha256.json`，SHA256 `aeccb67dc9c9bc849cbb1fa4e0688ee00fac0c0622cdafd2fe7ae1c1de3d564c`。四job日志及最终run/jobs/checks/protection/review/对象响应均在该目录。

正式JSON：`/tmp/tianshu-g3-20261113/pr134-b5-ci-independent-review.json`，SHA256 `bef9c57e141412f378654f0f83e48d24db233574a73b981748911990c6b6c9fd`。

此报告不执行/批准合并、不覆盖T1或后续组合，不扩大原协作锁、低层在线备份、无restore apply等交付边界；Hosted CI不等于Windows10/11/ARM64设备与安装/升级体验。
