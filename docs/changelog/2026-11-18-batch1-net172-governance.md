# 2026-11-18 · 第 1 批收口：Issue #171/#172 修复与工程治理减负

## 1. 在途并发与网络期限缺陷闭环
- **Issue #171（PR #173）**：`RestoreRegistry.append_event()` 改为在持锁事务内直接基于已校验的 `expected` 构造返回观察，提交解锁后不再重调 `observe()`，消除并发下「事件已落盘却误报 `RestoreRegistryBusy`」的假阴性，并补齐 `expected.meta` 与 `meta_bytes` 自洽核对。
- **Issue #172（`src/tianshu/net.py`）**：补齐 `0bdcf7b` 未覆盖的两类 `threading.Timer` 提前唤醒边界：
  1. `shutdown(SHUT_RDWR)` 使 `response.read()` 返回干净 EOF `b""`（早于 `time.monotonic()` 越过 `deadline`）时，有 `Content-Length` 不再误判 `net.read`，无 `Content-Length` 不再静默返回残缺 `200` 正文，统一由 `_remaining(deadline, deadline_interrupted)` 判为 `net.timeout`；
  2. `deadline_interrupted` 提升至 `_fetch_once` 跨重定向跳共享，杜绝第 0 跳解析 `Location` 或 `finally` 收尾期触发 `Timer` 后进入第 1 跳重置事件、多发未授权请求或误报 `net.connect_timeout`（独立第二读者终验判据 1–5 全 PASS）。

## 2. 工程治理减负与多头台账对账
- **拆分 `docs/KNOWLEDGE.md`**：将原 §2 逐 PR 状态流水与 §8 会话日志无损归档至 `docs/changelog/`，主知识库只保留架构铁律、关键决策、踩坑教训与当前边界，并取消易过期的手写测试/模块总数。
- **落地风险分级门禁（Tier 1 / Tier 2 / Tier 3）与工作树隔离**：更新 `AGENTS.md` 与 `CONTRIBUTING.md`，将独立第二读者与代码树 SHA 绑定聚焦于 Tier 1 高危核心路径，免除纯文档改动的本机全量重跑与 SHA 循环重绑；明确单会话独占 Worktree 与测试临时锁根隔离。
- **同步 `README.md` 与 `docs/ROADMAP-COMPLETE-AGENT-2026-11-14.md`**：修正 R1–R3（W13）、W2/W6/W7/W8/W10/W15 等已合入能力在文档中的陈旧「未合入/未开始」表述，同时如实保留四目标 release 实跑、真机验证与 10h 浸泡等 G3 未完成项。
