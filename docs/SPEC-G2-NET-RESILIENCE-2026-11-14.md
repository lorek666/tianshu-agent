# SPEC · 网络抓取的速率限制与连接重试（W2）

> 日期：2026-11-14 · 分支：`feat/net-rate-retry` · 基线：`main@c2cf1c7`
> 路线图：[ROADMAP-COMPLETE-AGENT-2026-11-14](ROADMAP-COMPLETE-AGENT-2026-11-14.md) W2
> 性质：**产品行为变更（非契约变更）**——C1–C5 不动；`net.py` 的模块 docstring
> 第 25 行「不自动重试」被**有意推翻**（本文件 §2 给出边界），文档对账同步
> [network-g2.md](network-g2.md) 与 KNOWLEDGE §7。
> 冲突面：`net.py` 自 8874538（G2 网络阶段）后无人在改；在途 PR #140/#141/#146/#150/#151
> 均不触碰 `src/tianshu/net.py` / `tests/test_network_fetch.py`。

## 1. 现状（代码事实）

- `net.fetch()` 是 GET 执行器：重定向只许同源、1 MiB 字节上限、整次抓取硬超时、
  每跳/每块可取消、失败抛 `NetError`（`reason` 是稳定分类）。
- 第 259–261 行注释已把**两个失败面**分开：「连不上 / TLS 握手失败（确定一个
  请求字节都没发）」与「读到一半断了（用量未知）」——这是给字节账本用的区分，
  正好是重试安全边界的现成事实。
- 登记限制（KNOWLEDGE §7 G2 行）：「无速率限制/重试」。
- 派发层 `dispatcher.py:626` 持 `NetLimits` 并传给 `fetch`；`build_stack(net_limits=)`
  已通——给 `NetLimits` 加字段即可，**dispatcher 零改动**。

## 2. 设计

### 2.1 连接重试：只重试「可证明没发出去」的失败

- **可重试集 = {`net.connect`, `net.connect_timeout`}**：这两类失败发生在
  `conn.connect()` 阶段、TCP 连接从未建立——**零字节到达对端**，重发不构成
  "第二次外发请求"，因此**不需要重新审批**。
- **明确不可重试**（服务器已见过这次请求，自动重发等于绕过逐次批准）：
  `net.tls`（TCP 已建立，字节到过对端）、`net.read` / `net.timeout` / 
  `net.protocol` / `net.redirect_*` / 任何 HTTP 响应（含 5xx——请求完成了，
  重试是模型的新决定，由它重新发起动作、重新拿批准）。
- 默认 `connect_attempts = 2`（1 次重试）；重试**共享同一个绝对期限**（总时长
  仍有上界），每次尝试前查取消；不做人为 backoff（死主机的连接超时本身就是
  惩罚，sleep 只会烧期限）。
- 重定向链每次尝试**重新跟随**（`visited` 跨尝试保留防环；`redirects` 报告
  取成功那次）。`Fetched` 增加末位字段 `attempts: int = 1`（dataclass 附加，
  既有构造零迁移；不进工具输出，不动 dispatcher）。

### 2.2 速率限制：进程级礼貌下限

- 进程共享「上一次 `fetch` 开始时刻」；本次开始前若间隔不足
  `min_interval_s` 就等待（分片等待，每片检查取消——等待中取消立即
  `NetCancelled`，不白等）。等待时间**不计入**抓取 deadline（deadline 从
  真正开始连接起算），总时长仍被外层 Run 纪律约束。
- 默认 `min_interval_s = 1.0`（对目标服务器的礼貌下限：普通用户的 agent
  连续抓 20 页也不至于 2 秒内打出 20 个请求）；`0.0` 显式关闭。
- 环境覆盖：`TIANSHU_NET_MIN_INTERVAL_S` / `TIANSHU_NET_CONNECT_ATTEMPTS`
  经 `NetLimits` 的 `default_factory` 读取（沿用本仓库 env 容错惯例：非法
  回落默认，不做静默禁用）；`build_stack(net_limits=...)` 的显式注入优先。
- 刻意不做：per-target 限速、令牌桶、并发上限——v1 的抓取是串行的
  （单 Run 单 Worker），进程级最小间隔已经覆盖真实行为；复杂化留给真实
  需求出现时。

### 2.3 为什么这符合授权模型（本 SPEC 的核心论证）

- 审批批准的是「对这个源上的这个 URL 发一次 GET」。连接失败时**这次 GET
  没有发生**（TCP 层零字节），重试是同一个已批准动作的继续尝试，不是新动作；
  一旦服务器见过请求（TLS 之后的一切失败），自动重发就是**第二个** GET——
  那必须由人重新批准。这条边界被服务器端命中数测试钉住（§4 #6）。

## 3. 判据（新增测试：单元在 `tests/test_net_resilience.py`，集成在 `tests/test_network_fetch.py`）

| # | 测试 | 判据 |
|---|---|---|
| 1 | retry_recovers_on_connect_failure | monkeypatch `_connect` 首次抛 `OSError`、二次成功 → fetch 返回成功，`attempts == 2` |
| 2 | retry_exhausts_and_reports | 目标是 `closed_port`：两次尝试都拒 → `NetError(net.connect)`，且 `_connect` 恰好被调 2 次 |
| 3 | connect_attempts_one_means_no_retry | `connect_attempts=1` + 失败注入 → `_connect` 恰 1 次，行为回到旧行为 |
| 4 | read_failure_is_never_retried（服务器命中数） | 夹具 accept 后发送半截响应再断 → `net.read` 失败，服务器**恰 1 次**连接（NC3 反向锚点） |
| 5 | retry_respects_cancel | 首次连接失败后 cancel=True → `NetCancelled`，第二次尝试不发生 |
| 6 | rate_limit_wait_and_cancel_check | 注入时钟的 `_rate_limit_wait` 纯函数：首次 0 等待；紧随其后第二次等待 = 剩余间隔；间隔已过则 0；等待中 cancel → 立即抛 |
| 7 | rate_limit_integration | loopback 夹具 + `min_interval_s=0.3`：两次 fetch 的墙钟间隔 ≥ 0.25s；`min_interval_s=0` 时无此约束 |
| 8 | env_overrides | env 设 `TIANSHU_NET_CONNECT_ATTEMPTS=3` / `TIANSHU_NET_MIN_INTERVAL_S=0.5` → `NetLimits()` 默认值生效；非法值回落默认 |
| 9 | limits_validation | `connect_attempts < 1` / `min_interval_s < 0` → `NetError(net.limits)` |
| 10 | 既有 network 测试 | 全绿不变（默认 `min_interval_s` 对 loopback 双请求测试的影响见 §3.1） |

**§3.1 兼容性注意**：默认 1.0s 间隔会拖慢既有测试里连续两次 fetch 的用例
（夹具内脚本化多 URL 任务）。既有测试通过 `_stack(net_limits=...)` 显式注入
`NetLimits(min_interval_s=0.0)` 即可保持原速——这是「测试注入自己的上限」
的既有模式，不是放宽产品默认。

## 4. 负控（变异后必须红，逐条实跑记录）

- **NC1**：重试循环被拆（`connect_attempts` 强制按 1 生效）→ 判据 #1、#2 红。
- **NC2**：等待逻辑被拆（间隔判定恒 0）→ 判据 #6、#7 红。
- **NC3**：把 `net.read` 加进可重试集 → 判据 #4 红（服务器命中数从 1 变 2——
  这条负控钉住 §2.3 的授权边界本体）。

## 5. 文档对账

- `net.py` 模块 docstring：「不自动重试」条目改写为新边界（可重试集、为什么
  TLS/read 不在内）；新增速率限制小节。
- [network-g2.md](network-g2.md) §7 边界条目同步（"无速率限制/重试" → 已交付，
  带日期与本 SPEC 链接）。
- KNOWLEDGE §7 G2 行与 §8 会话日志。
- 本 SPEC：判据/负控实测回填。

## 6. 边界与未决

- 重试只覆盖连接失败；**DNS 解析成功但解析到私有地址仍会照发**（DNS rebinding
  防护是另一张工单，不在本 SPEC 顺手做）。
- `attempts` 只进 `Fetched`（库层事实），不进 `tool.result` 输出——每工具 output
  子契约未做（§7 疤），扩输出属另一张工单。
- 进程级间隔对多 Run 并发是全局的：两个 Run 的抓取互相计入对方的间隔——
  这是「礼貌是进程属性」的取舍，如实记录。
- 取消语义不变：等待/新请求/新读可停，已在途的 TCP 字节不可撤回。

## 7. 高危判定

不碰授权判定本身（审批、绑定、digest 都在派发层且未动）；重试边界恰恰是
**用执行侧可证明的事实收窄自动行为**。不构成强制独立复审项。

## 8. 实测回填（2026-11-14，macOS arm64 本机）

- `tests/test_net_resilience.py` 定向 **11 passed**；网络面四文件
  （resilience / network_fetch / outbound_allow / research_eval）
  **64 passed**。判据计划 10 条 → 实际落 11 个测试函数（限速语义拆成
  纯函数 3 条 + 集成 1 条）。
- 负控三条实跑红还原：NC1（拆重试循环）→ recover/exhaust 2 红；
  NC2（拆等待）→ 语义/集成 2 红；**NC3 首轮打偏**——变异加在
  `net.protocol` 上，而半截响应场景在本机实际产生 `net.read`，负控假阴性
  被手动复现抓出（accepts==1、reason=net.read），修正变异目标后红
  （accepts 变 2）。教训照旧：**负控必须验证"变异命中了目标路径"**，
  红不红都要看清红了什么。
- `closed_port` 在 macOS 上表现为 `net.connect_timeout` 而非 `net.connect`
  （ECONNREFUSED 路径差异），断言沿用既有测试的集合口径
  `{"net.connect", "net.connect_timeout"}`——两者同属连接阶段、同属可重试集。
- 测试基建：conftest 增 autouse 夹具把 `TIANSHU_NET_MIN_INTERVAL_S` 钉 0
  （限速是进程级状态，不钉会让全套测试互相等待）；限速判据显式 setenv 打开。
- 全量与 CI 绑定对应 PR SHA，不在本文重复记录。
