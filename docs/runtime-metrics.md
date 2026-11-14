# 本地运行指标与控制面隔离（PRD §10.6，W2）

> 分支 `feat/runtime-metrics`，基线 `main@da84d92`。
> 本文记录**实际跑过的命令与输出**。没有实测支撑的数字不写；做不到的写在 §7。

---

## 1. 交付了什么

1. **`src/tianshu/metrics.py`（新）**：进程内指标采样器。写本机数据根
   `<data_root>/metrics.jsonl`（原子追加、一行一 JSON），**不联网、无自动上传、
   无第三方依赖**；提供纯 Python 的 p50/p95（nearest-rank）。
2. **慢订阅者隔离（核心修复）**：`daemon` 的推送从"主循环里同步 `conn.send()`"
   改为**每连接有界发送队列 + 独立发送线程**；队列满 / 发送超期 ⇒ 断开该订阅者
   并记 `slow_subscriber_dropped`，**其他连接与请求处理不受影响**；订阅者重连按
   `since_seq` 游标补读，审计事实仍在事件日志里。
3. `src/tianshu/ipc.py`：新增 `ServerConnection.abort()`（POSIX 用
   `dup()+shutdown(SHUT_RDWR)` 唤醒阻塞中的发送线程；Windows 退化为 close）。
4. 测试：`tests/test_metrics.py`（11 条）、`tests/test_slow_subscriber.py`（4 条）、
   `tests/test_soak.py`（默认跳过、`TIANSHU_SOAK=1` 才跑）。
5. 本文档：指标定义、测量方法、本机实测值、未冻结的 SLO 与原因、剩余 unknown。

**没有做**：不引入遥测 / 上报端点（D7：诊断只本地导出）；不改 `host.py`；
不改既有判据；不新增运行时依赖；不冻结 SLO 数值（PRD §10.6 明确留给发布候选）。

---

## 2. 指标定义与口径

写入：`<data_root>/metrics.jsonl`，每行一个 JSON 对象：

```json
{"ts":1794360183044,"metric":"slow_subscriber_dropped","value":1.0,"kind":"counter",
 "pid":14756,"instance_id":"daemon_f0c04cf759fb","reason":"send_stall",
 "subscribed":true,"last_sent_seq":286,"sent_frames":24}
```

| metric | 单位 / kind | 定义与测量点 |
|---|---|---|
| `startup_ready_ms` | ms / duration | `serve()` 调用 → 端点已 bind+listen、accept 线程进入循环、自连握手成功（即 `on_ready` 之前的那一瞬）。`cold_start` 标记数据根此前是否有 `events.db`（冷/热分开聚合） |
| `cancel_ack_ms` | ms / duration | 收到 `run.cancel` RPC → 控制面把响应入队（ack 是"控制面确认"，**不是**"执行器停下"） |
| `cancel_stop_ms` | ms / duration | 从 `run.cancel` 起，到该 Run 落到终态（`succeeded`/`failed`/`cancelled`/`fenced`）为止；`state` 字段带终态；超 `CANCEL_STOP_TIMEOUT_S`（60s）仍未终态 ⇒ `value:null` + `reason:"timeout"` |
| `restart_recover_ms` | ms / duration | 启动恢复扫描耗时；`cold_start` 与 `runs_scanned` 一起写（扫描 1 条和扫描 0 条不是一回事） |
| `rss_mib` | MiB / resource | `resource.getrusage(RUSAGE_SELF).ru_maxrss`（macOS 字节 / Linux KiB，已归一为 MiB）。**这是峰值常驻，不是瞬时 RSS** |
| `cpu_pct` | % / resource | 相邻两次采样的 `process_time` 差 / 墙钟差 ×100。相对**单核**；多线程同时忙时 >100% 是可能的 |
| `disk_growth` | 字节 / resource | 数据根总字节数相对采样启动基线的增量；同一行带 `disk_bytes` 总量。条目超过 50,000 时记 `null`（unknown：指标不许把守护进程拖进百万文件 walk） |
| `event_seq_head` | 事件序号 / event | 采样时刻事件日志 `MAX(seq)`；采样线程用**自己的** `cross_thread` 连接读，不跨线程借用控制连接 |
| `slow_subscriber_dropped` | 计数 / counter | 慢订阅者被断开的次数，`reason ∈ {queue_full, send_stall, backlog_stall}`；正常关 UI / 客户端 EOF 不计入 |

聚合：`metrics.summarize(records)` 给每个 metric 的
`count / unknown / p50 / p95 / min / max`。**`value:null`（unknown）单独计数，
不进 p50/p95 的分母**——"没测到"与"测得 0"必须分开。

读取：`metrics.read_metrics(data_root)`；坏行抛带行号的 `ValueError`（指标文件损坏
不该被当成"没有数据"）。**但"正在写的最后一行"不算坏行**：文件末尾没有 `\n` 收尾的
那一段先尝试解析，失败就跳过（`tail -f` 语义），下一次读它就会完整；已由 `\n` 收尾的
坏行仍然报错。写侧是"一行一次 `os.write`"，但**不把"一次 write 对并发读者原子"当契约**
——收尾轮 CI（ubuntu）实测过：慢订阅者用例一边采样一边轮询，读到了断在第 37 个字符的
`{"instance_id":"daemon_...",`，原实现把整条测试判成"指标文件损坏"。

### 2.1 缺阈值时输出 unknown（不写假 SLO）

```python
SLO_THRESHOLDS: dict[str, dict[str, Any]] = {}     # 刻意留空
slo_report() -> {"status":"unknown",
                 "reason":"SLO 阈值未冻结：数值留给发布候选（PRD §10.6）",
                 "thresholds": {}}
```

冻结 SLO 需要：发布候选的硬件基线、静默机测量、样本量与置信口径。当前只有
"本机非静默环境下的量级"，写通过/不通过会变成假验收。

---

## 3. 慢订阅者隔离：修复前后

### 3.1 修复前（真 bug）

`daemon._main_loop` 单线程顺序执行 `_drain_requests()` 与 `_push_events()`；
`_push_events()` 里对每个订阅者同步 `conn.send()`。一个订阅后不再读 socket 的
客户端会把内核发送缓冲写满，`send_bytes` 阻塞，**主循环被卡住**：状态查询、
审批、取消、shutdown 全部排在后面。这是"关 UI 任务继续"的反面——一个坏 UI
能把整个控制面拖死。

### 3.2 修复后

- `_ClientConnection` = 有界 `queue.Queue`（默认 `SEND_QUEUE_MAX=1024`）+ 一个
  daemon 发送线程。`conn.send()` 只入队，**不写 socket，不阻塞主循环**。
  同一连接的所有帧（响应与推送）走同一队列，顺序由入队顺序保证——`subscribe`
  的响应先于历史推送。
- 推送按"最小游标"分批读日志；某条连接队列高于高水位
  （`SUBSCRIBER_BACKLOG_WATERMARK=0.75`）时先不入队，事件留在日志里，下一轮再试。
- `_check_slow_subscribers()`：`sending_for > SEND_STALL_S`（默认 5s）或
  `backpressured > SUBSCRIBER_BACKLOG_STALL_S`（默认 5s）⇒ `drop(reason)`：
  标记断开 + `abort()` 唤醒阻塞中的发送线程（POSIX `shutdown(SHUT_RDWR)`；
  Windows 只能 close，线程何时醒由系统决定，不承诺）。
- 断开后 `_reap_connections()` 记 `slow_subscriber_dropped`；其他连接完全
  不受影响。
- `subscribe` 只登记游标（`last_sent_seq = since-1`），历史补读与增量统一由
  主循环分批追：**历史很长也不会在一条请求里卡住主循环**，且天然有背压。
- shutdown 顺序：停 accept → 停驱动 → **排空各连接已入队的帧** → 关连接/listener
  → 释放锁 → 关库。排空是为了不让客户端把"shutdown 已接受"看成"响应丢了"。

---

## 4. 测量方法与实测值（本机 macOS arm64）

### 4.1 环境（只对本机成立）

| 项 | 值 |
|---|---|
| 机器 / 架构 | Apple M4 · arm64 |
| OS | macOS 26.6.2（Darwin 25.6.0） |
| Python | 3.12.13（uv 管理） |
| 内存 | 16 GiB |
| 数据根 | `/tmp/tsmeasure-*`（APFS，内置卷）；**不是**静默机：同一台机器上有并行会话 |
| 采样间隔 | `TIANSHU_METRICS_INTERVAL_S=0.2`（仅本次测量；生产默认 5s） |
| 数据规模 | 33 Task / 349 事件 / `events.db`+`artifacts` ≈ 1.8 MiB / 201 条指标记录 |

### 4.2 命令

```bash
cd /Volumes/Date/ts-wt-metrics
PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python /tmp/measure_metrics.py
```

脚本场景（不是单元测试替身，是**真 daemon + 真 IPC**）：
冷启动 → 20 个 Task/Run 跑完 → 3 次"模型调用期间取消" → 慢订阅者（4 条 ~180KB
事件把发送线程写阻塞）→ 热启动 5 个 Run → 构造一个非终态 Run 再热启动以测恢复。

### 4.3 实测值

| 指标 | 实测（本机，非静默） |
|---|---|
| `startup_ready_ms` 冷 | **6.68 ms** |
| `startup_ready_ms` 热 | **1.95 ms** |
| `cancel_ack_ms` | 0.27 / 0.33 / 0.66 ms（n=3，p50 0.33，p95 0.66） |
| `cancel_stop_ms` | 379.1 / 385.6 / 388.4 ms（n=3，p50 385.6，p95 388.4；模型调用被 gate 住 0.4s，说明 stop 跟的是**安全点**而不是 RPC） |
| `restart_recover_ms` | 冷 0.014 ms（扫描 0 条）/ 热 0.032 ms（扫描 0 条）/ 热 0.091 ms（扫描 1 条） |
| 慢订阅者窗口内控制面往返（`approvals.list` + `run.status` 一轮） | n=85，**p50 40.7 ms，p95 45.9 ms，max 48.4 ms** |
| `slow_subscriber_dropped` | 1 次：`reason=send_stall`，`sent_frames=24`，`last_sent_seq=286` |
| `rss_mib` | n=47，p50 38.69，max 38.69（峰值常驻；启动后很快稳定） |
| `cpu_pct` | n=47，p50 2.78%，p95 111.7%，max 131.3%（相对单核；启动/推送突发时多线程同时忙） |
| `disk_growth` | n=47，p50 4.07 MiB，max 4.08 MiB（含 180KB 级事件与 WAL） |
| `event_seq_head`（末尾采样） | 349 |
| `slo_report()` | `{"status":"unknown","reason":"SLO 阈值未冻结…","thresholds":{}}` |

**读法（避免把数字读成结论）**：

- n=3 的 p95 等于最大值，只作量级参考；`cancel_*` 的真实分布要在发布候选的
  静默机上用更大样本重测（§7）。
- 控制面往返 p50 ≈ 40ms 的主要成分是主循环 20ms 轮询间隔（请求要等下一轮），
  不是被慢订阅者拖慢；修复前同一场景在 5.5s 内都拿不到响应（见负控 §6）。
- `rss_mib` 是峰值常驻，天然单调；"最大值 = p50"不代表泄漏，只代表进程很快稳定。

---

## 5. 稳定性：有界浸泡（180s，本机实跑）

```bash
TIANSHU_SOAK=1 TIANSHU_SOAK_SECONDS=180 \
  PYTHONPATH=$PWD/src /Volumes/Date/天枢Agent/.venv/bin/python -m pytest \
  -o addopts="" -q -s tests/test_soak.py
```

输出（原文）：

```text
[soak] {"cpu_pct_p95": 113.8750143174466, "cycles": 236,
        "disk_growth_bytes": {"last": 5835526.0, "max": 5835526.0, "min": 255.0},
        "duration_s": 180.73, "endpoint_alive_after_exit": false, "errors": [],
        "event_seq_head_last": 5980.0, "lock_present_after_exit": false,
        "metrics_records": 624, "requested_s": 180.0, "residue_threads": [],
        "restarts": 1, "rss_mib": {"last": 46.84375, "max": 46.84375,
        "p50": 46.84375, "p95": 46.84375}, "slow_subscriber_dropped": 0,
        "stopped_cleanly": true}
1 passed in 181.38s (0:03:01)
```

覆盖：反复"建任务/跑完"、订阅/断连/带游标重连、取消、浸泡中段重启 daemon。
结论（只对本机）：180s / 236 轮后 RSS 稳定在 ~46.8 MiB（峰值口径），数据根增长
~5.8 MiB，无残留 daemon 线程、无残留端点/锁、退出干净。默认 scale 是 3 分钟，
`TIANSHU_SOAK_SECONDS` 可调；默认 CI **跳过**并显式说明原因（不是静默跳过）。

---

## 6. 回归测试与负控

### 6.1 回归（全部确定性、真实 daemon/socket）

| 文件 | 用例 | 钉住什么 |
|---|---|---|
| `tests/test_slow_subscriber.py` | `test_slow_subscriber_does_not_block_control_plane` | 订阅后不读的客户端存在时，`state`/`approvals.list` 在 0.75s 内持续返回；慢订阅者被断开、记指标；重连从 0 补读 1..head 连续无缺口 |
| 同上 | `test_send_stall_drop_and_reconnect_from_cursor` | 单帧写阻塞超期（`send_stall`）⇒ 断开；重连从**客户端游标**补读，序列恰为 `cursor+1..head` |
| 同上 | `test_other_subscriber_keeps_receiving_while_one_is_dropped` | 一个订阅者被断开时，另一个健康订阅者的事件流仍是完整连续序列 |
| 同上 | `test_connection_queue_is_bounded_and_full_means_drop` | 发送线程卡住时队列**有界**（第 N+1 帧触发 `queue_full`），并 abort 唤醒阻塞写 |
| `tests/test_metrics.py` | `test_record_read_roundtrip_and_fields` 等 | JSONL 可读、字段/kind/pid/instance 完整；p50/p95 nearest-rank；`unknown` 与数值分开 |
| 同上 | `test_unknown_is_null_not_zero` | 缺测量写 `null`，聚合 `count=0/unknown=1`；`slo_report()=unknown` |
| 同上 | `test_cold_then_warm_startup_metrics_are_real` | 冷/热启动各一条 `startup_ready_ms`，`cold_start` 正确，值 >0；采样线程真的写出 `rss_mib`/`event_seq_head` |
| 同上 | `test_cancel_ack_and_stop_are_measured_separately` | 模型调用期间取消：ack 立即返回、stop 在安全点后才落，`stop.value ≥ 100ms` |
| 同上 | `test_restart_recover_metric_records_recovery_scan` | 热启动写 `restart_recover_ms` + `cold_start` + `runs_scanned` |
| 同上 | `test_metric_file_stays_inside_data_root_and_module_has_no_network` | 指标只落数据根；`metrics.py` 的 import 里没有网络栈（D7） |

### 6.2 负控（把修复回退后必须红，实测输出原文）

**负控 A：把推送回退成"主循环里同步 send"**（`_ClientConnection.send` 直接
`raw.send`；不改测试）：

```text
E  tianshu.ipc.ConnectionClosed: 等待响应超时
1 failed in 5.52s
```

→ 控制面在慢订阅者窗口内彻底没响应，正是 issue 的形态。

**负控 B：指标采样写空**（`MetricsRecorder._append` 改成不落盘）：

```text
E  AssertionError: assert [] == ['startup_rea...ent_seq_head']
2 failed in 0.37s
```

**负控 C：指标谎填 0**（`_record` 把所有 value 强制 0.0）：

```text
E  assert 0.0 is None                       # test_unknown_is_null_not_zero
E  AssertionError: stop 计时必须覆盖到安全点（负控：谎填 0 会在这里红）
2 failed in 0.65s
```

三条负控都真的跑过；恢复后对应用例重新通过。

---

## 7. 未冻结的 SLO 与仍未验证的部分

**为什么没有 SLO 阈值**：PRD §10.6 把数值冻结留给发布候选。冻结至少要满足：
静默机（无并行编译/测试）、固定电源与文件系统、每指标足够样本量（p95 在 n=3
时没有统计意义）、明确"冷"的定义（本机冷启动仍是热页缓存）。在这些条件满足前，
`SLO_THRESHOLDS` 留空、`slo_report()` 返回 `unknown`。

**未验证 / 未知**（不写成"已支持"）：

- **Windows 实机未测**：`abort()` 在命名管道上只能 `close()`，阻塞中的
  `WriteFile` 是否被唤醒由系统决定；慢订阅者测试在 CI Windows runner 上会跑，
  但本机没有 Windows 实机证据。若 close 唤不醒，被断开连接的发送线程会留到
  进程退出才回收（POSIX 上 `shutdown(SHUT_RDWR)` 能确定唤醒）。
  `SO_SNDBUF`/队列行为差异也未测。
- **静默机指标未测**：§4 的数字来自有并行会话的非静默机，只作量级参考。
- **`rss_mib` 是峰值 `ru_maxrss`**，不是瞬时 RSS；要测"当前占用"需要平台 API
  （macOS `task_info`），本轮不做。
- **`disk_growth` 的 unknown 边界**：数据根条目 >50,000 时记 `null`，不提供
  精确值。
- **指标文件没有轮转**：~~默认 5s 采样约 17k 条/天（约数 MB/天），
  保留策略属于发布候选决策，本轮不假设。~~ **已交付（2026-11-14，见 §10）**：
  按大小轮转 + 保留 4 份已落地为默认行为；仍未做的是发布候选层面的保留期决策
  （当前 80 MiB 上界是默认参数，不是冻结的保留期政策）。
- **慢订阅者的宽限窗口内仍会重复读日志**：floor 游标被最慢的订阅者钉住时，
  主循环每轮从该游标重新读 `PUSH_BATCH=256` 条事件并逐条构造帧（不写 socket）。
  有界但不免费；本轮没有做"按连接维护独立读游标"的优化，压测时 CPU 的短时
  尖峰部分来自这里。
- **控制面响应可能超过协议上限**（新发现，未修）：`state` 返回最近 50 个 Task
  的完整 `goal`；当很多 Task 带超长 goal 时响应会超过 `ipc.MAX_MESSAGE_BYTES`
  （8 MiB），服务端 `encode()` 抛 `ProtocolError`、连接断开。测量脚本第一次跑
  时踩到（46 个 180KB goal 的 Task 后 `state` 直接 8.4MB）。这是 `control.py`
  的响应形态问题，**不在本工作流文件所有权内**，登记为剩余 unknown。
- **SLO 与告警**：本轮没有告警、没有上报、没有仪表盘；`metrics.jsonl` 是**数据**，
  不代表任何运行时决策（PRD §10.6 只要求本地测量能力）。

---

## 8. 验收对照（SPEC §10）

- [x] 新代码有回归测试；关键修复（慢订阅者隔离、指标不许写假数）有负控，负控真的
      跑过并记录回退后红的样子（§6）。
- [x] `pytest -o addopts="" -q` 全绿、`ruff check .` 干净（数字见 PR body；本轮
      实测 `1331 passed, 7 skipped`，基线 `1316 passed, 6 skipped`，新增 skip 只有
      默认跳过的浸泡用例）。
- [x] 文档写了当前边界、未验证项、仍不成立的宣称（§7）。
- [x] 没改 `host.py`；没改既有判据；没加运行时依赖；没联网。
- [x] PR body 含：改了什么、为什么、判据、负控摘要、剩余 unknown。

## 9. 将显式指标文件与已有报告一同保全（开发工具，2026-11-13）

`scripts/baseline_evidence.py` 接受至少一份显式 C5 报告，以及至多一份显式
`metrics.jsonl` 文件；不扫描数据根、不采样、不调用模型、不启动 soak：

```bash
mkdir -p .omc/artifacts
uv run --frozen python scripts/baseline_evidence.py \
  --repo "$PWD" --report /absolute/path/report.json \
  --metrics /absolute/path/metrics.jsonl \
  --out .omc/artifacts/baseline-evidence-new-id
```

仓库必须 clean，输出父目录须已存在、目标目录必须不存在；仓库内目标须由真实
`git check-ignore` 确认已忽略。指标先逐条严格解析（包括没有换行的尾段），再对
字节副本调用 `read_metrics()`；截断坏尾行拒绝收集，不沿用实时读者对正在写尾段
的容错。这个检查不生成聚合数值，也不冻结 §7 的 SLO。

`inputs/` 原件和含本机路径的 `collection.json` 仅留本地私有。
`summary.json` 是固定字段的可分享摘要，不含路径、模型、端点、正文、分数或原始
输入 hash，且不能单独证明收集完成。完成收据最后原子发布，成功静默；发布前
失败没有完成收据，发布后进程被终止可保留收据。自身最终退出码由外部观察者取得，
不提前写成工具观察到的 exit0。

收据只证明本次收集观察，执行与批准固定为 `NOT_EXECUTED_BY_TOOL` /
`NOT_EVALUATED`；不认证历史执行，不检测变化后还原的所有瞬态变化，也不承诺
断电持久性、抗同 uid 篡改或 Windows ACL 隔离。本提交前独立内容复验与本机组合
lock/full/ruff 已通过；正式提交绑定及新原生 CI 仍待核，后续结果见
`codex/g3-baseline-evidence` 对应 PR。未新增十小时实跑、静默机测量或用户 Windows
设备证据。

---

## 10. 指标文件轮转与保留（2026-11-14，`feat/metrics-rotation`）

`metrics.jsonl` 原先只增不减（§7 旧条目）。本轮把「按大小轮转 + 保留上限」
落地为**默认行为**，方案与判据见
[SPEC-METRICS-ROTATION-2026-11-14](SPEC-METRICS-ROTATION-2026-11-14.md)。

**行为**（全部收在 `metrics.py` 内部，`daemon.py` 未改）：

- 追加前在锁内 `os.stat` 当前文件；达到 `max_file_bytes` 先轮转再写——单文件
  不超过 `max_file_bytes + 一行`。轮转是编号槽位链式 `os.replace`
  （`metrics.jsonl.1` 最新、编号越大越旧），最旧一份被下一轮覆盖，**没有
  DELETE**，没有「删除失败多留一份」的分支。
- 默认 16 MiB × 4 份（总上界 ≈ 80 MiB）。这是**估算默认值**（按 5s 采样
  ≈ 3–5 MB/天推算），不是冻结的保留期政策；可用
  `TIANSHU_METRICS_MAX_FILE_BYTES` / `TIANSHU_METRICS_MAX_ROTATED_FILES`
  覆盖，构造参数 `0` 显式禁用轮转（逃生门，行为有测试断言）。
- **样本永不因轮转而丢**：轮转失败（Windows 上读者持句柄会
  `PermissionError`）降级为继续追加当前文件，`rotation_errors` 计数 + stderr
  提示一次；当前文件可能暂时超过阈值，下一轮重试。槽位允许有空洞，读取侧
  按存在的编号处理。
- 每次成功轮转在指标流里记一条 `metrics_rotated` 事件（`value` = 被轮转文件
  字节数）；轮转事件的追加自身不再级联产生新事件（否则阈值小于一行时会无限
  递归，负控 NC3 以 `RecursionError` 证实）。
- 读取面：`read_metrics(..., include_rotated=True)` 按时间序（最旧在前）读回
  全部历史，`limit` 作用于拼接后全序列；`list_metrics_files()` 给出有序清单。
  **默认只读当前文件，既有调用方零迁移。**只有纯数字后缀算轮转件，
  `metrics.jsonl.tmp` / `.backup` 这类手放文件不进清单。
- 跨进程并发写入方（理论上 daemon 是唯一写入方）不加锁：`os.replace` 原子，
  输者按失败降级继续追加——指标是 best-effort 诊断面，不给它上事件日志级的
  一致性语义。

**验证**（本机 macOS arm64，2026-11-14）：`tests/test_metrics.py` 定向
**22 passed**（新增 8 条轮转/保留/读取/校验判据，既有 14 条不变全绿）；
负控三条实跑红——NC1 拆掉大小检查 → 轮转/保留 2 条红；NC2 轮转失败改为丢样本
→ 容忍用例红；NC3 摘掉事件级联抑制 → `RecursionError` 红；均已还原。
全量门禁与 CI 结果绑定对应 PR SHA，不在本文重复记录。
