# SPEC · 网络抓取的速率限制与连接重试（W2）

> 日期：2026-11-14 · 分支：`feat/net-rate-retry` · 基线：`main@88ec0ab`（rebase 后）
> 路线图：[ROADMAP-COMPLETE-AGENT-2026-11-14](ROADMAP-COMPLETE-AGENT-2026-11-14.md) W2
> （**悬空披露（复审 P2）**：账本文件在本 PR 分支上不存在——它随 #151 交付，合并前该引用不可验证）
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
  `conn.connect()` 阶段、**应用层请求字节为零**（TLS 握手字节可能已到对端，
  2026-11-15 复审如实更正措辞——`net.tls` 是握手后的分类、不在可重试集）。
- **重试附加条件（2026-11-15 复审修复）**：仅当**该次尝试未跟随任何重定向**
  才真的重试。已消费重定向 = 服务器已见过至少一个 GET，重发是第二个 GET
  （复审探针实测：重定向后第二跳连接失败曾被自动重发，服务器收到
  `['/a','/a']`，且因 visited 跨尝试保留被误报成 `net.redirect_loop`）。
- **明确不可重试**（服务器已见过这次请求，自动重发等于绕过逐次批准）：
  `net.tls`（TCP 已建立，字节到过对端）、`net.read` / `net.timeout` / 
  `net.protocol` / `net.redirect_*` / 任何 HTTP 响应（含 5xx——请求完成了，
  重试是模型的新决定，由它重新发起动作、重新拿批准）。
- **如实记录（复审 P1-2）**：`net.connect_timeout` 在可重试集里但实际几乎
  不可达——连接超时 = 整个剩余期限（`timeout=remaining`），首试烧尽后
  deadline 检查会拒绝第二次尝试；重试在实践中只对快速失败（RST/拒连）生效。
  per-attempt 预算切分是否值得做见 §6 未决。
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

- 审批批准的是「对这个源上的这个 URL 发一次 GET」。连接失败且未跟随任何
  重定向时，**应用层的这次 GET 没有发生**（请求字节为零；TLS 握手字节除外，
  如实），重试是同一个已批准动作的继续尝试，不是新动作；一旦服务器见过
  请求（读过响应 / 已跟随重定向 / TLS 之后的一切失败），自动重发就是
  **第二个** GET——那必须由人重新批准。这条边界被服务器端命中数测试钉住
  （§3 #4 + 复审新增「重定向已消费不重试」）。

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
| 9 | limits_validation | `connect_attempts < 1` / `min_interval_s < 0` / `min_interval_s` 非有限值（inf/nan，复审 P2 补）→ `NetError(net.limits)` |
| 10 | 既有 network 测试 | 全绿不变（默认 `min_interval_s` 对 loopback 双请求测试的影响见 §3.1） |
| 11 | no_retry_after_redirects_consumed（复审 2026-11-15 新增） | GET /a → 302 /b 后第二跳连接失败 → 终态 `net.connect`（原始分类，非 redirect_loop 误报），服务器只见过 1 次 GET `/a`（NC-D 锚点） |
| 12 | limits_validation 的 inf/nan 分支（复审 P2 补） | `min_interval_s=inf/nan` 构造即拒（此前 inf 会让速率等待死循环并持住进程锁） |

**§3.1 兼容性注意（复审更正措辞，2026-11-15）**：默认 1.0s 间隔会拖慢既有
测试里连续两次 fetch 的用例。实现取的是**conftest autouse 夹具统一钉
`TIANSHU_NET_MIN_INTERVAL_S=0`**（全测试进程关间隔、产品默认不变）——不是
逐用例 `_stack(net_limits=...)` 显式注入（初版 SPEC 误写）。

## 4. 负控（变异后必须红，逐条实跑记录）

- **NC1**：重试循环被拆（`connect_attempts` 强制按 1 生效）→ 判据 #1、#2 红。
- **NC2**：等待逻辑被拆（间隔判定恒 0）→ 判据 #6、#7 红。
- **NC3**：把 `net.read` 加进可重试集 → 判据 #4 红（服务器命中数从 1 变 2——
  这条负控钉住 §2.3 的授权边界本体）。
- **NC-D（复审 2026-11-15）**：拆掉 fetch() 的 `redirects_followed` 守卫 →
  判据 #11 红（第二个 GET 发生、原始 net.connect 被误重试）。

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
- **per-attempt 预算切分未做**（复审 P1-2 登记）：`net.connect_timeout` 因此
  实际几乎不可达（见 §2.1 如实记录）。若要让它真的可重试，需要把总预算按
  尝试次数切分——时序行为变化较大，另立工单裁决，本 PR 不夹带。
- **`test_retry_exhausts_and_reports` 的时序余量**（复审 P3 记录）：macOS 上
  内核 RST 约 7.8s < 10s deadline，余量 2.2s——时序巧合成立但薄；deadline
  检查与 `_remaining` 之间存在 µs 级 `net.timeout` 顶替窗口。已知边界，如实
  登记，不假装已经消除。

## 7. 高危判定

不碰授权判定本身（审批、绑定、digest 都在派发层且未动）；重试边界恰恰是
**用执行侧可证明的事实收窄自动行为**。不构成强制独立复审项。

## 8. 实测回填（2026-11-14，macOS arm64 本机；2026-11-15 复审轮更新）

- `tests/test_net_resilience.py` 定向 **11 passed**（复审轮 +2 = **13 passed**）；
  网络面四文件
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

## 9. CI 首轮红与修复（2026-11-14，随修复提交补记）

- CI 首轮：ubuntu / windows-shard1 / UI 绿；**macOS、windows-shard0 红**，
  全部集中在 RU-W1 断网评测（`network_failed_honestly`：期望
  `net.connect*`，实测 `net.timeout`）；本机全量 3209 passed 绿——flaky 竞态。
- **根因**：closed 端口的第一次连接尝试等满 socket 超时（= 整次 deadline，
  实测 10.00s）；重试的第二次尝试开头 `_remaining(deadline)` 在 deadline
  已尽时抛 `net.timeout`——把「连接阶段失败、0 字节已知」顶成「连接建立
  后的失败、用量未知」，失败分类与字节账本口径一起错档。本机侥幸绿是因为
  第二次尝试的 `_remaining` 与 socket 超时毫秒级竞争赢了（elapsed=10.00s
  仍报 connect_timeout）；CI 调度稍异就输。
- **修复**：重试循环在进入下一次尝试前检查 deadline——已烧尽则重抛最后一次
  连接失败的**原始错误**（`last_connect_error`），不再进 `_fetch_once`。
  语义：deadline 是总预算，用尽即尝试用尽，最终失败 = 最后一次真实尝试的
  原始分类；假重试（0 预算的第二次尝试）一并消失。
- 判据：`test_deadline_exhausted_keeps_connect_classification`（fake conn
  烧满 deadline + `sleep(+0.1s)` **确定性**触发，calls==1、reason==
  `net.connect_timeout`）；负控：拆掉 deadline 检查 → 该测试红（还原后绿）。
- 教训追加到 NC 清单：**重试机器自己抛的超时不得覆盖被重试动作的原始分类**；
  且「本机绿」不排除毫秒竞态——CI 红才是竞态的如实暴露。

## 9. 复审轮（2026-11-15，独立第二读者 + 修复）

独立第二读者（不读作者结论）在 3591568 复审本 PR：总结论 **PARTIAL、无 P0**——
判据 9 PASS + 2 PASS*；负控 4/4 有牙（M-a deadline 检查、M-b 限速、M-c
net.read 入可重试集、M-d 拆重试循环，全红还原）；门禁独立复跑 3211P/8S exit0。
发现 2 P1 / 4 P2 / 4 P3，当轮修复：

- **[P1] 重定向已消费后连接失败被自动重试**（第二个 GET 无重新批准 + visited
  保留致 `net.redirect_loop` 误报；探针：服务器收到 `['/a','/a']`）→ 修复 =
  fetch() 记录 `redirects_followed`，已消费重定向的尝试一律不重试、抛原始
  连接错误；新判据 #11 + NC-D（拆守卫即红）。
- **[P1] `net.connect_timeout` 重试实际不可达**（连接超时 = 整个剩余期限）→
  如实改文档（§2.1），per-attempt 切分登记 §6 未决，不夹带行为变更。
- **[P2] 「TCP 从未建立、零字节到达对端」措辞过强**（TLS 握手 1498 字节实测
  已到对端）→ 全部改为「应用层请求字节为零（TLS 握手字节除外，如实）」；
  授权核心（HTTP GET 字节为零 + 服务器未见请求不重发）未破。
- **[P2] `min_interval_s` 接受 inf/nan**（inf → 速率等待死循环并持住进程锁，
  其余 fetch 全部阻塞）→ `__post_init__` 加 `math.isfinite`，判据 #9/#12 补。
- **[P2]** SPEC 头部 ROADMAP 悬空未披露 → 补披露；基线数字过时 → 更正。
- **[P3]** SPEC §3.1 注入方案与实现不符（实为 conftest autouse 钉 0）→ 更正；
  KNOWLEDGE 会话日志测试数 11→12 → 复审轮再更新；时序余量与 µs 顶替窗口
  → §6 如实登记。
