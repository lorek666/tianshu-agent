# 天枢 Agent · PRD §10.6 本地运行质量 SLO 基线与浸泡验证报告（2026-11-18）

> 关联工单：`G3-B10`（版本化冻结 SLO 门禁与测量套件）· `G3-B11`（≥10 小时常驻浸泡测试框架）
> 契约与阈值配置：[schemas/slo/slo-v1.json](../schemas/slo/slo-v1.json) · [schemas/slo/slo-thresholds-v1.json](../schemas/slo/slo-thresholds-v1.json)
> 测量与浸泡脚本：[scripts/measure_slo.py](../scripts/measure_slo.py) · [scripts/run_soak.py](../scripts/run_soak.py) · [tests/test_slo_and_soak.py](../tests/test_slo_and_soak.py)

---

## 1. 实测环境与冻结元信息

- **裁决结论（Verdict）**：**`PASS`**（模式：`release`，每项指标 `N >= 10`）
- **策略版本**：`slo-thresholds-v1`（`policy_version = 1`，规范化 JSON SHA256 前缀 `0602a7b1ec84265e…`）
- **实测时间（UTC）**：`2026-11-18T04:00:58Z`
- **硬件与系统**：`Apple M4` · `arm64` · `macOS 26.6.2 (Darwin 25.6.0)` · 内存 `16.0 GiB` · `APFS` · Python `3.12.13`
- **负载声明**：本机多工作树并行开发环境下实测（**非独占静默机**）；全部运行使用独立临时 `TIANSHU_DATA_ROOT`、`TIANSHU_AUTHORITY_ROOT` 与 `TIANSHU_TEST_LOCK_ROOT`，零外部网络连接，所有导出字段遵守 D7 本地默认脱敏（`d7_local_redacted`）。
- **热启动种子库规模**：`50` 任务 / `50` 已完成运行 / `1500` 条哈希链事件 / `events.db` `1,437,696` B（约 `1.37 MiB`）/ `artifacts` `15,600` B。

### 复现命令

```bash
uv run --frozen python scripts/measure_slo.py \
  --output-json .omc/artifacts/slo-baseline.json \
  --output-md .omc/artifacts/slo-baseline.md
```

---

## 2. 核心 SLO 实测分布与冻结阈值表

| 指标 (`metric`) | 测量口径 | 样本数 | `min` (ms) | `p50` (ms) | `p95` (ms) | `max` (ms) | 冻结 `p95` 上限 (ms) | 单次峰值上限 (ms) | 结果 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| `startup_cold_ms` | 空数据根拉起真实 `Host` + `Daemon` 子进程至 HTTP `/state` + `task.create` 可用 | 10 | 480.639 | **492.421** | **502.034** | 502.034 | 2200.0 | 4500.0 | `PASS` |
| `startup_warm_ms` | 预置 50 任务 / 1500 事件热库拉起真实 `Host` + `Daemon` 并完成扫描至 `/state` + `task.create` 可用 | 10 | 478.441 | **493.655** | **502.708** | 502.708 | 2500.0 | 5000.0 | `PASS` |
| `cancel_ack_ms` | 真实不读慢订阅者（IPC + SSE）背压及慢模型/慢工具阻塞期间，`run.cancel` 至收到 RPC 确认 | 10 | 26.972 | **41.800** | **142.499** | 142.499 | 500.0 | 1200.0 | `PASS` |
| `cancel_stop_dispatch_ms` | 慢订阅者背压下从发起 `run.cancel`（含 40ms 受控在途窗口）至安全点停机并阻断新工具派发 | 10 | 127.211 | **175.671** | **253.823** | 253.823 | 1000.0 | 2500.0 | `PASS` |
| `approval_rpc_ms` | 真实不读慢订阅者（IPC + SSE）背压窗口内，经 Host `/rpc` 执行 `approvals.decide` 落库并入队唤醒 | 10 | 26.902 | **51.678** | **62.040** | 62.040 | 500.0 | 1200.0 | `PASS` |
| `restart_recovery_ms` | 4 类真实 `os._exit(77)` 硬退故障后拉起真实 `Daemon` 至任务恢复完结或明确落 `reconciling` | 10 | 221.280 | **228.295** | **273.397** | 273.397 | 2000.0 | 4500.0 | `PASS` |

- **组件内耗时拆分**：除去父子进程 `python` 解释器启动开销，`Daemon` 进程内部 `startup_ready_ms` 中位数冷启动为 `11.825 ms`、热启动为 `7.603 ms`；崩溃重启恢复扫描内部 `restart_recover_ms` 的 `p95` 为 `3.117 ms`。
- **阈值冻结与防事后放宽机制**：`schemas/slo/slo-thresholds-v1.json` 与 `scripts/measure_slo.py` 内的 `FROZEN_V1_*` 常量双向绑定。门禁评估器在未同步升级 `policy_version` 与 `schema_version` 的前提下检测到任何阈值上调或 `min_repeats` 下调，立即以退出码 `2`（`threshold_relaxed_without_version_bump`）拒绝；缺指标、未知样本、样本数不足或超限则以退出码 `1` 拒绝。

---

## 3. 慢订阅者隔离与在途副作用分账核验（Suite 2）

- **双通道真实不读慢订阅者**：同时挂载只 `subscribe` 不读取 socket 的 `ipc.Client` 与只建立连接不读流帧的 `Host` HTTP `/events` SSE 客户端，并连续推送 `≥180 KB` 大负载事件填满内核发送缓冲区。
- **观测与审计结果**：
  - 慢订阅者背压断连计数（`slow_subscriber_dropped`）：`17` 次；
  - 断连后 `since_seq` 游标续读核验：`True`（连续补读 `425` 条事件，序列严格连续、0 缺口）；
  - 控制面确认与在途操作解耦：全部 `10` 轮（`5` 轮慢模型 + `5` 轮慢工具）`cancel_ack` 均在在途操作放行前返回（`ack_before_inflight_release_all = True`）；
  - 已在途副作用单独列账：`5` 轮慢工具的首个已在途 `run_command` 正常完成并逐笔核验 `5 / 5` 张 `Receipt`；同一模型轮排在后面的第 2 个工具（`create_report`）在安全点被全部拦截，**取消后新工具派发次数 = `0`**；
  - 在途操作返回后的纯安全点停机耗时：`p50 = 103.924 ms`，`p95 = 133.843 ms`。

---

## 4. 崩溃重启恢复不变量审计（Suite 3）

跨 4 类真实子进程 `os._exit(77)` 硬退场景（共 `10` 轮）：

1. `approved_tool_after_intent`：审批已消费、工作区写租约已获取、`tool.intent` 落库后写盘前硬退 → 重启后依写前快照核定 `none_applied`，安全完结，`replay` 一致；
2. `approved_tool_after_write`：审批已消费、目标文件已落盘但未写 `tool.result` / `Receipt` 前硬退 → 重启后依写前证据与磁盘哈希核定 `applied` 并补齐唯一 `Receipt`，**0 次重复执行**；
3. `unknown_effect_reconciling_blocked`：写中撕裂导致磁盘既非 `before_state` 亦非 `expected_after` → 重启后发射 `recovery.blocked`、写入用户级 `authority` 目录阻断，并将运行转入 `reconciling`、挂起死主作业，**0 次盲目续跑**；
4. `unsettled_model_and_budget`：语义摘要模型调用发出后硬退（留下 `summary_versions(pending)` 与未决 `budget_reservations(pending)`）→ 重启后摘要标记 `failed`（**0 次盲目重调摘要模型**），未决摘要预占原样保留于 `BudgetLedger.reconcile()`（`2` 笔，未静默清零亦未重复建行），主轮次正常结算且 `replay` 通过。

汇总审计：**`blind_retries = 0` · `lost_receipts = 0` · `leaked_budget_reservations = 0` · `orphan_pids = []`**。

---

## 5. 常驻浸泡运行器验证与 ≥10h 执行说明（`G3-B11`）

### 5.1 本机确定性多周期实跑证据（15s 快速核验）

```bash
uv run --frozen python scripts/run_soak.py \
  --duration-seconds 15 \
  --sample-interval-seconds 2 \
  --output-dir .omc/artifacts/soak-15s
```

- **结果**：`verdict = PASS`，实跑 `16.52 s`，完成 `9` 轮完整混合周期，追加写出 `27` 条符合 `slo-v1.json#soakSample` 的实时指标记录与原子刷新 `status.json`：
  - 工作负载覆盖：`idle_windows=9`、`effects_runs=9`、`pure_only_runs=9`、`file_undos=9`、`streamed_turns=9`、`memory_ops=10`、`subscriber_disconnects=9`、`slow_subscriber_drops=3`、`cursor_reconnects=3`、`pause_resume_cycles=5`、`mid_run_cancels=5`、`scheduled_runs_fired=3`、`graceful_restarts=2`、`recovery_scans_verified=2`；
  - 资源水位：空闲总 RSS `p50 = 88.672 MiB`（`max = 99.219 MiB`），活跃总 RSS `p50 = 94.640 MiB`（`max = 100.828 MiB`）；打开 FD 峰值 Daemon `57` / Host `6` / Runner `7`；SQLite `9.03 MiB`；
  - 退出洁净度：`endpoint_closed_after_stop = True`、`locks_cleared_after_stop = True`、**`orphan_pids = []`**。

### 5.2 ≥10 小时正式浸泡运行方式与诚实边界声明

```bash
TIANSHU_SOAK=1 uv run --frozen python scripts/run_soak.py \
  --duration-hours 10 \
  --sample-interval-seconds 30 \
  --output-dir .omc/artifacts/soak-10h
```

- 运行期间可随时读取 `.omc/artifacts/soak-10h/status.json` 与 `samples.jsonl` 观察实时进度、RSS 最小二乘斜率（`MiB/h`）、FD 增量及磁盘增长；结束时自动生成脱敏的 `soak-report.json` 与 `soak-report.md` 并执行 `soak_limits` 门禁。
- **诚实边界（不伪造长时证据）**：
  1. 本次提交已完成 `scripts/measure_slo.py`（`N=10` 正式门禁基线）与 `scripts/run_soak.py`（`15s` 与 `6s` 多周期真实子进程验证），**尚未宣称已完成 10 小时墙钟挂机实跑**；G3 正式发版签字前须在目标候选提交上执行完整 10 小时浸泡并归档产物；
  2. 上述基线仅代表本机 `macOS arm64 (Apple M4)` 非独占开发环境实测值，不替代 `mac-x64`、`win-x64`、`win-arm64` 真实用户设备及真实付费线上模型的验收证据。
