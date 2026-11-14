# PR144 两测试诊断增量独立窄复审

结论：**PASS_NARROW_TWO_TEST_DIAGNOSTIC_DELTA_NOT_HEARTBEAT_FIX**。只接受已核的 CRLF 协议修正与失败诊断增量。心跳根因仍未确定、未修复；本机 60 次绿不能抵消原 Windows 真失败。

## 绑定与范围

- 日期：2026-11-14；基线完整 SHA：`96e3e485eb4ecbecadbadb54a60414967ab63649`。
- 固定候选：`/tmp/tianshu-g3-20261113/low-disk-pr144-diagnostic-freeze-gpdzfziw/candidate-complete`。
- 新清单 1030 项，SHA256 `2922a34503134aa6674c905dffc016b87a839687cb41ec66dbed6839a1448bc9`；相较已审 96e3 清单只有 `tests/test_low_disk_run_creation.py`、`tests/test_m2_acceptance.py` 两项变化，其余 1028 项仅按清单精确身份继承，不冒称重新复审全部文件。
- 两个变更文件及最小夹具原字节重新核对；`events.py`、`worker.py`、`model.py` 另外实际重算 SHA256，均等于新旧清单。没有接受被拒的共享锁提案。
- 实际测试仅复制两 test、support、conftest、pyproject 共 5 件，生产模块从固定 candidate/src 加载。所有数据库、authority、临时数据均在本审核输出；真实模型不参与。

## 核验结果

1. 整文件 AST 机械核对通过：creation 只新增 CHILD_TEXT、READY/GO 文本传输及 stderr 表达；heartbeat 只由 6 次变 60 次并新增 run 别名与消息。原成功、空 errors、暂停断言及真实双进程 READY/GO 屏障、竞争、重开持久化断言均保留。
2. 原候选两个目标：`2 passed in 3.15s`，pytest exit 0。只跑一次；真实数据库只读回查为 60 个 succeeded、1 个 paused，60 条 run.end 均 succeeded 且 error=null。
3. CRLF 正控：仅自有 copy 的子进程 READY 输出改为原始字节 READY\r\n，仍运行原双进程竞争与持久化测试：`1 passed in 0.35s`。这是 macOS 强制协议探针，不能称为 Windows 实跑。
4. 坏 READY 负控：仅原始字节改成 NOT_READY\r\n，pytest exit 1，setup/teardown 均通过，call 在原 READY 断言失败并显示 `'NOT_READY\n' == 'READY\n'`。没有放宽协议断言。
5. 诊断 reason 负控：只经自有 pytest 插件将 ScriptedModel.complete 定点抛出 RuntimeError(`DIAG_REASON_PROBE`)。真实 Worker/AgentLoop 收尾后，原 succeeded 断言在 call 阶段失败；新增消息显示 iteration=0、worker.errors=[]、完整 run.end.error=`模型调用异常（attempt 1/3）：DIAG_REASON_PROBE` 及实际事件类型序列。此红是主动注入的预期负控，不是心跳根因复现。

所有命令、环境白名单、Python/平台、模块来源、exit 与耗时见 `runs/*/execution.json`；完整 stdout/stderr 与 pytest 各阶段证据在对应目录。AST 证据见 `logs/static-results.json`，数据库回查见 `logs/positive-db-observation.json`。所有临时 mutation 均恢复，5 件最小夹具末次 hash 与候选一致。

## 不能外推的事实

- 原生 run `43549430150` conclusion=failure；采集 errors=[] 只说明采集无错。原 Windows shard1 的 READY CRLF 和正常收尾 failed 两个 call 红仍如实保留。
- heartbeat 消息的 run.end 是完整 payload，但 events 仅为类型名。若缺少 run.end，当前增量不能保证给出终止 reason。
- 原生失败日志没有终止 reason，不能据此确定 SQLite、共享连接或其他根因。生产完全未因本轮修改而修复。
- 本轮没有跑 full/UI/旧矩阵，没有调 API、rerun/取消 CI、改 live、commit、push 或 merge；不批准父代理之后的 full、事实文稿、正式提交或新 CI。

证据只占本审核目录约数 MB，没有复制旧庞大源码/证据集合。pytest 的 5 个 convenience symlink 已记录并移除，避免封存穿透链接。普通文件 0444、目录 0555；`hash-index.json` 覆盖除自身外全部普通文件，封存后另做只读哈希/尺寸/权限核验。
