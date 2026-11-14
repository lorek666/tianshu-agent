# 两份测试增量的独立冻结内容验收

**PASS_FROZEN_TWO_TEST_FILES_NARROW_CONTENT_ONLY**。计划范围新增阻断 0。第二读者 `/root/review_admission_contract`，基线 `c2cf1c71c65f46dea161e41b140df99d137a5686`。本读者只读 live，在独立临时快照运行；没有 full / lock / 全仓 ruff / 原生 CI 结论。

精确冻结输入：

- MCP：8539 bytes，SHA256 `569b774b52f05cf4bd5f2c3f92f2df70037ecbae22b01877520302bae7acef5e`。
- M2：83675 bytes，SHA256 `f2200905cc0d644709c04af300e58abbc70222b6de890ce90b770e99c7a087ba`。

独立运行产品源 73 件与基线 Git 对象逐件原字节相同；哈希清单为 `runtime-source-index.json`。本包只保留固定源索引、必要测试/探针及真实输出，不复制全 source。环境 macOS 26.6.2 arm64 / Python 3.12，不能外推 Windows。

## 断言与范围：PASS

MCP 原 10 个 assert AST 全保留；unknown、无 Receipt、两次 replay 后日志不变的断言不弱化。两次 replay、`_server_config`、`_release` 与其余测试 AST 完全相同，真实 stdio 子进程、百万字节、200ms 配置与 join5 保留。仅目标派发测试有函数变化。

M2 原 normal-finish 函数源码、装饰器与 AST 完全相同，6 assert、60 轮、1ms 不改。函数/类变化仅诊断器、诊断装饰器与新增 enter/ring 回归。产品、CI、锁策略、重试和超时无增量。精确对账记录在 `facts.json`，脚本 `final_ast_and_facts.py`。

## MCP 最终 569b：PASS

独立窄运行 **4 passed in 1.43s**，逐节点 setup/call/teardown 全通过，原输出与命令见 `outputs/final-mcp4*`。

最终 SHA 五类探针均由独立读者重新执行。`regression_plugin.py` 已逐段阅读，真实组件只在指定屏障/负控接点拦截，旧作者结果不代替重跑：

1. 真实审批入口屏障保持 2s：1P2.65s，call 恰委派一次、配置 200ms、内层 call 区间 225101083ns，unknown/无 Receipt/两 replay 全跑到底。
2. 真实 unknown 结果结算入口屏障保持 2s：1P2.67s，done 已置位后结算等待不再污染传输等待；内层 call 223902333ns，原后续断言全执行。
3. 真实 stdin PIPE 恢复 blocking，真实 os.write 写 1000132 bytes，对端持真实 socket 屏障不读：预期 1F2.36s，done.wait(2)=False，第194行 returned 断言真红，worker 清理后退出。
4. 同一真实 blocking write 确认进入，观测2s后释放 peer，先等 call 结束/done 已 set，再返回原 wait=True：预期 1F2.36s，第196行 duration 断言真红。测试包装器实测 **2008697750ns > 2000000000ns**；plugin 内层区间2008663333ns是另一观测点，不混用。
5. 真实审批入口传输前抛错：预期 1F0.31s，0 call，在第178行 outcome 原错误断言失败；提前唤醒有效，worker 退出。

五组 setup/teardown 均通过，实际负控发生在 call，原始 events/命令/log 在 `ind-final-*`。第3/4项刻意破坏非阻塞约束，不能称为自发复现原 Windows 根因。

`probe_thread_start_final.py`：真实 Thread 实例的 start 抛唯一 RuntimeError，最终捕获就是原 marker，close1，ident=None、线程未存活，方法身份恢复。`probe_accept_cleanup_v2_final.py`：先建立真实子进程/peer，再让 accept 抛唯一 TimeoutError，原对象身份保留、close1；独立保留的 server 对应真实 process 和 worker 均在探针兜底 finally 前退出，未以注册表清空冒充进程退出。原件见 `outputs/*final-facts.json`。

## M2 最终 f220：PASS

独立窄运行 **14 passed, 29 deselected in 13.21s**，逐节点三阶段全通过，覆盖原 normal-finish、成功静默、原异常 identity、patch 恢复、事务提交/回滚/嵌套、原生 beat、ring/脱敏/弱身份/源别名和采集失败保护。原命令与输出在 `outputs/final-m2-14*`。Windows junction 分支本机未执行。

新增回归运行真实结果提交 `append_many → transaction → BEGIN`，开启 query_only 得到真实 SQLITE_READONLY，注入单调时钟推进7000000ns，再用180个真实事务淘汰ring；关键异常仍有 enter_start=1000、enter_elapsed=7000000、实际 append_many/_commit_result 调用链（≤4 canonical 帧）和有效 runtime，私有路径/正文不输出。7ms是注入的包装器区间，不是实际SQLite阻塞时间。

两项最小删功能变异仅在临时副本：删 enter_start 保存，新增回归第1162行 KeyError 真红（1F0.92s）；删逐帧 call_chain 输出，第1166行 actual=[] 的调用链断言真红（1F1.07s）。两者 setup/teardown 通过，真实 SQLite enter 拒绝及 ring 淘汰仍执行，未修改 live。

独立 `probe_m2_retention.py` 另证：真实 READONLY enter，超过4层的受控活帧仅保留4个 canonical条目；局部 sentinel 可回收，留存元数据无 frame/traceback/exception，caller_chain 不进入 ring/active。受控 frame 使用 canonical 源名编译；实际产品 caller、符号链接及外部副本由前述仓库回归另证，二者不混称。

格式压力把真实 native capture 展示字段扩到现有上限，填四 critical 源、16帧、8 active和ring：note恰24000 chars、最长行778 chars，有总截断标记，四种critical计时字段全保留。该长字段仅为格式压力，不是原CI现场。runtime只在失败note输出，成功静默；说明明确包含调度、并非纯SQLite wait、active context并非OS锁owner。

## 历史与边界

旧基线/3d69的真实accept抛错泄漏资源：基线既有洞，最终真accept探针已销账。中间9abd新finally无条件join未启动线程覆盖原marker：独立FAIL原件保留，最终569b仅在ident非None时join，真start探针已销账。旧5类结果保留在 `ind-ind-*`，绝不替代最终SHA。

观察器误差如实保留：系统python3一次缺pytest，尚未执行探针；accept修订首次路径错配重复旧版；人工approval gate与新close_all不兼容，在观察器gate断言失败。该candidate不能支持产品判断，最终使用真实连接peer后accept抛错的新探针重验。前置错误探针覆盖真实审批，未穷举所有构造器异常。

本报告通过的是两份冻结测试的计划窄内容。main首次push Windows两个失败仍为FAIL，实际锁机制及原MCP卡点仍未知。没有生产根因已修、Windows已绿或整分支已通过的结论；full/lock/ruff、正式Git对象和新CI由root另核，issue145/147与恢复B状态不据此改变。

复现时把包复制到新的可写临时目录，先从指定基线 Git 对象还原73件src并核索引，放入各probe所需snapshot目录，共享复制原support/conftest；不要在只读包内覆盖原件。`run_ind.py <fresh-label> <mode> <snapshot-final>`支持上述五mode，负控预期exit1；其余probe及 `run_narrow_and_mutants.py` 给出完整重跑入口，原命令均保留在run.json。
