# G3-B10 · 慢订阅者背压下的 `state` 开发诊断

本诊断补充 [PRD §10.6](PRD-v1.md#106-本地运行质量与发布测量新增要求) 的状态查询测量口径。输出是独立的 `slo-state-pressure-dev-v1` / `diagnostic_pre_threshold` 数据，**不是发布裁决**。不更改 [SLO v1 基线](SLO-BASELINE-2026-11-18.md)、其六项必需指标、schema、冻结阈值或 CI 门禁；目前没有为 `state` 声明或冻结延迟上限。

## 复跑（仅限仓库隔离的 pytest 夹具）

```bash
uv run --frozen pytest -o addopts='' -q -s tests/test_slo_state_pressure.py
```

正例在 stdout 打印脱敏 JSON。诊断没有应用 CLI 入口；使用隔离的临时数据根、权威根、测试维护锁根和脚本模型，不接入生产凭据或外部端点。子进程启动前会核对既有的 pytest 测试锁根是否实际生效，失配就不运行诊断。原有 SLO 冒烟及发布模式不启用观测探针。

## 计量与取样

- 复用 [Suite 2](../scripts/measure_slo.py) 的真实 Host / Daemon 子进程和原有不读 IPC / SSE 客户端及种子事件。慢模型、慢工具、覆盖审批每试次附加一次普通认证 IPC `state`；在该试次的背压种子写入之后、原场景的工作运行之前发起，不将准备、等背压就绪或等断连计入响应时长。沿用夹具的 350 ms 发送/积压超时、15 ms 驱动间隔；不更改生产值。
- 标签只使用已有的 `hello.client` 观察元数据。Daemon 夹具在真实订阅推送已开始时发就绪信号，记录状态派发时同一连接、同一次进行中的发送，以及随后同一连接被原生看门狗以 `send_stall` 断开的证据；该发送在这些观测间没有已发送帧计数推进。累计断连数和客户端仅注册订阅都不能替代同源证据。
- 就绪、派发、断连记账和客户端 RPC 起止使用本机跨进程的高分辨率 `perf_counter_ns()`，避免 Python 3.12 Windows 的低分辨率 `monotonic()` 把先后事件记成同一 tick；派发必须落在该次客户端请求与成功响应之间。就绪与断连记账时的发送年龄（`send_age_at_ready_ms` / `send_age_at_drop_ms`）由 Daemon 本地的 `monotonic()` 计算，与看门狗同钟域，不和 RPC 时间戳相减。
- 缺计时、缺压力证据、试次/派发时间不匹配、已断开或不能确认同源看门狗断连均计作无效；逐试次保留原因、有效分母及 p50/p95/max。分位数使用 nearest-rank；空有效样本返回 `null`。负控在真实同一订阅者已断开后再做正常 `state`，即使 RPC 成功也不得进入分位数。
- 测量边界仅为 Daemon 认证 IPC 往返（含客户端编解码与服务调度）。不代表 Host HTTP `/state` 或 UI 端到端延迟、不证明同一时刻的 SSE 背压，也不证明背压覆盖了整个 RPC 或其后续取消/审批。

## 一次本机观测（非基线）

2026-11-19 12:05 UTC，Darwin 25.6.0 / arm64、Apple M4、16 GiB、Python 3.12.13；非独占开发机。被测 `sm0` / `st0` / `ov0` 三个试次均有同源证据，无无效样本、无跟踪到的残留进程，游标补读连续。`state` 延迟 p50 **34.586 ms**、p95 **34.745 ms**、max **34.745 ms**（此样本量下 p95 即 max）。这只是夹具内的小样本诊断；没有 Linux、Windows 或设备体验验证，也不能据此判定达到发布 SLO。
