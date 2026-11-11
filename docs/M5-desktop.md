# M5 桌面闭环：三进程、IPC 契约与恢复语义

> M5 目标（PRD §11）：**Host / UI / Daemon 三进程打通、权限审批 UI**。
> 退出条件只有一句，但它是整个里程碑的判据：**关闭 UI 后任务继续，重启后恢复**。
>
> 本文写的是**这一版实际实现的形状**，包括与 PRD / TECH-ARCH 的偏差。偏差不藏：
> 每一条都在 §2 的表格里，理由和替换时机都写清楚；能力没做的写在 §9。
>
> 验收证据见 `docs/M5-verification.md`（实际跑过的命令与输出，不是设计意图）。

---

## 1. 三个进程，各自的墙

```
┌──────────────────────────────────────────────┐
│ UI（浏览器里的单页应用，React + TS）          │
│ 任务 / 运行时间线 / 审批卡片 / 成本 / 撤销     │
└───────────────┬──────────────────────────────┘
                │ loopback HTTP + SSE（预览壳）
┌───────────────┴──────────────────────────────┐
│ Host（Python 进程）                          │
│ 单实例 · 拉起并监护 Daemon · UI 桥 · token    │
└───────────────┬──────────────────────────────┘
                │ ipc.py：AF_UNIX / AF_PIPE
┌───────────────┴──────────────────────────────┐
│ Daemon（Python 进程）                        │
│ 任务状态 · 事件日志 · AgentLoop · Worker 驱动  │
│ 审批决定 · 恢复扫描                           │
└───────────────┬──────────────────────────────┘
                │
         SQLite（WAL）· 快照 · blobs
```

**墙的划法与 PRD §1.2 一致**：

| 进程 | 拥有 | 不拥有 |
|---|---|---|
| UI | 交互、展示 | 任何业务状态（刷新页面重放事件即可重建） |
| Host | 进程树、系统集成、UI 桥、token | 任务状态、模型调用、策略判定 |
| Daemon | 任务/事件/审批/上下文/工具路由 | 系统权限弹窗、窗口 |

**为什么"关 UI 任务继续"不是靠 UI 保持连接实现的**：Daemon 的驱动循环不依赖任何
客户端。UI 只是订阅者；它断开、页面刷新、浏览器被杀，Daemon 一律当"少了一个订阅者"，
而不是"任务该停"。反过来说，Daemon 停下来时 UI 也必须能自己发现（SSE 断开 → 提示
"守护进程已退出"），不许假装一切正常。

**为什么"重启后恢复"不是重放日志那么简单**：重开 UI 只解决"看得见"；真正的恢复发生在
Daemon 启动时——扫"有 intent 无 result"的调用、把 `queued`/`parked` 的作业重新排队、
保留仍然有效的待批审批。重复副作用的防线来自 M1 已有的 intent/result + receipt，
M5 不重新发明一套。

---

## 2. 与 PRD / TECH-ARCH 的偏差（逐条登记）

| # | 文档原样 | 本版实现 | 为什么现在这样 | 换回去的时机 |
|---|---|---|---|---|
| D1 | 桌面应用 = **Tauri 2 · React · TS** | UI 是 React + TS，但**外壳是系统浏览器**，没有 Tauri/WebView2 | 仓库当前零 Rust/Node 运行时依赖；M5 的退出条件是进程语义，不是外壳。引入 Tauri 会把"三平台 CI"变成"三平台 Rust 工具链 + WebView2 依赖"，风险与工时都不属于 M5 | 单独工单：外壳 + 托盘 + 开机自启 |
| D2 | **Desktop Host (Rust)** | Host 是 Python 进程 | Host 的职责（单实例、监护、桥）与语言无关；换成 Rust 时 IPC 契约与 UI 契约都不动，只是把 `host.py` 换实现 | 与 D1 同一张工单 |
| D3 | UI ↔ Host 走**类型化 IPC（版本化）** | 预览壳走 **loopback HTTP + SSE**：`POST /rpc`、`GET /events`、`GET /state` | 浏览器没有"类型化 IPC"可用；为预览壳写 WebSocket 帧编解码是纯负债。RPC 的**语义**与 Daemon 的 IPC 一一对应，换壳时只换传输 | D1 落地时：WebView 内直接调 Host 的 `postMessage` 风格 IPC |
| D4 | TECH-ARCH §4.2 帧 = `[4B 长度][JSON]` | 消息语义一致（一条消息一个 JSON 对象）；帧编码用标准库 `multiprocessing.connection`（POSIX = AF_UNIX，Windows = 命名管道） | Windows 没有 AF_UNIX；自造长度前缀要分别处理"流式 socket / 消息式管道"，多一处只在 Windows 炸的差异。`authkey` 顺带解决传输层认证 | 需要跨语言/跨版本 IPC 时，只替换 `ipc.Server` / `ipc.Client` |
| D5 | 关闭窗口 ≠ 退出：最小化到托盘 | M5 没有窗口可关：**关浏览器标签页 = 断订阅者**；Host 一直活到 `tianshu host stop` 或 Host 进程被杀 | 没有外壳就没有托盘。语义上"关 UI 不停任务"已经被完整验证 | 与 D1 一起 |

**不为偏差辩护的部分**：D1/D2 意味着 M5 交付的**不是**可双击安装的 App。它是一个能
在本机真实跑起来、有真实审批 UI、能证明进程语义的闭环。安装包、签名、托盘是外壳
工单的事，不在本文假装完成。

---

## 3. IPC 契约（`src/tianshu/ipc.py`）

### 3.1 端点与认证

| 平台 | 端点 | 认证 |
|---|---|---|
| macOS / Linux | `<data_root>/run/agent.sock`（目录 0700，socket 0600） | `run/ipc.key`（0600，32 字节随机）作为 `authkey` |
| Windows | `\\.\pipe\AgentOS-<data_root 的短哈希>` | 同上（命名管道自带实例 ACL，默认只允许当前用户） |

- `data_root` 默认走 TECH-ARCH §9 的平台标准目录；`TIANSHU_DATA_ROOT` 覆盖它。
  **测试与 CI 绝不允许写用户主目录**，所以测试一律显式传 tmp 目录。
- `authkey` 不是对抗"本机用户改自己的程序"（PRD §8.1 已明确不承诺），它防的是
  "另一个 App 的 bug 顺手连上来"和"旧安装包连错实例"。

### 3.2 三类消息

```
请求   {"id": "c7", "method": "run.start", "params": {...}}
响应   {"id": "c7", "ok": true,  "result": {...}}
       {"id": "c7", "ok": false, "error": {"code": "...", "message": "...", "details": {...}}}
推送   {"push": "event", "event": {"seq": 42, "type": "...", ...}}
```

- 一条消息 = 一个 JSON 对象。单条上限 8 MiB，超过就断开而不是解析。
- **推送没有 `id`**：客户端 `call()` 必须跨过路上遇到的推送继续找自己的响应
  （`Client` 把它们排队给 `next_event()`）。UI 的 RPC 与 SSE 是两条连接，但
  CLI/Host 的单连接上两者会交错，这条是实测踩出来的。
- 错误用 `code` 分支，不要解析 `message`。M5 用到的 code：
  `protocol_mismatch` / `unknown_method` / `bad_params` / `not_found` /
  `conflict`（状态不允许）/ `forbidden`（策略/审批）/ `internal`。

### 3.3 握手与版本

```
→ hello {protocol_min: 1, protocol_max: 1, client: "host/0.1.0", pid: 1234}
← {"ok": true, "result": {protocol: 1, daemon_version: "0.1.0",
                          instance_id: "...", event_seq_head: 42,
                          data_root: "...", started_at: 1699...}}
```

版本用**区间协商**：两边各报 `[min, max]`，交叠则取交叠上界；不交叠回
`protocol_mismatch` 并断开，不做"尽力兼容"。加字段不升版本，改语义才升。

未完成 `hello` 的连接只能调 `hello`，其它方法一律 `protocol_mismatch`。
`probe()` 的探活连接在握手前就断开，服务端把这种情况当"可重试"，不记错误。

### 3.4 方法表（v1）

| 方法 | 参数 | 结果 | 说明 |
|---|---|---|---|
| `hello` | 见上 | `welcome` | 必调，且必须是第一条 |
| `state` | — | `{tasks, runs, approvals, event_seq_head, daemon}` | 首屏；`runs` = 全部非终态 + 最近 20 个终态/fence。`daemon = {daemon_version, instance_id, pid, data_root, started_at}` |
| `task.list` | `{limit?}` | `{tasks}` | 按更新时间倒序 |
| `task.create` | `{goal, workspaces:[{root_path, mode}], allow_exec_cmd?}` | `{task}` | 建任务不建 Run |
| `task.revise` | `{task_id, goal}` | `{task}` | 改目标 → `goal_revision+1`，旧 Run 被 fence |
| `run.start` | `{task_id, out_name?}` | `{run}` | **只入队**，立即返回；执行由 Daemon 驱动 |
| `run.list` | `{task_id}` | `{runs}` | 失败不覆盖，历史全在 |
| `run.status` | `{run_id}` | `{run}` | |
| `run.pause` / `run.resume` / `run.cancel` | `{run_id}` | `{run}` | 控制请求与生效状态分开（PRD §4.3）。`run.resume` 只对 `paused` / `waiting_approval` / `reconciling` 入队；`created`/`queued`/`running`/`cancelling` 是 no-op（已经在跑或在队里，再塞会和 owner 抢 claim） |
| `approvals.list` | `{run_id?}` | `{approvals}` | 过期的先清再列（批了也不会执行的不该留在队列里） |
| `approvals.decide` | `{approval_id, decision:"approve"\|"reject", reason?}` | `{approval, resumed}` | `resumed=true` 只表示"已交给驱动线程"，**不表示已经跑起来**（续跑可能失败并被丢弃）。`reason` 目前不进库：审批记录没有自由文本字段，改了就要动 `schemas/m2/approval.json`；它只用于 UI 展示 |
| `events.run` | `{run_id, after_seq?, limit?}` | `{events, seq_head, has_more}` | 事件日志是唯一事实源 |
| `subscribe` | `{since_seq}` | `{subscribed:true, from_seq}` | 之后服务端先补历史再推增量 |
| `undo.plan` | `{run_id}` | `{changes:[{path, kind, reason?}]}` | 只读 |
| `undo.apply` | `{run_id}` | `{applied: bool, refused?: [str]}` | 走既有"还原也经审批"的派发路径；前置拒绝抛 `refused`（含原因列表），执行期异常抛 `undo_failed`，不吞 |
| `shutdown` | `{reason}` | `{ok}` | Host 显式退出时调。`reason` **进 Daemon 日志（stderr），不进事件日志**：事件日志是 run 作用域的（每条事件挂 `run_id`），而 C3 是封闭集合，为一个进程生命周期事件新增类型会同时改 schema 与回放口径，代价大于收益（见 §10 第 1 条） |

**视图**（字段是契约，UI 直接消费）：

```
TaskView     {task_id, goal, goal_revision, status, active_run_id, workspaces,
              created_at, updated_at}
RunView      {run_id, task_id, state, outcome, effect_state, control_requested,
              goal_revision, lease_epoch, started_at, ended_at,
              n_applied, n_unknown, undoable}
ApprovalView {approval_id, run_id, task_id, action, resources, reason, rule_id,
              isolation, state, created_at, expires_at}
EventView    {seq, type, run_id, task_id, ts, actor_kind, payload}
```

`action` / `rule_id` / `state` / `effect_state` 是**机器枚举**。UI 必须做人话映射，
不许把枚举直接甩给用户（PRD §14 F14：普通用户只看人话）。`digest` 不进视图：
审批卡不展示摘要，人和模型之间不该靠一串哈希沟通；需要排障时走 CLI。

`ApprovalView.isolation`（F2 起）是审批**之前**可见的隔离视图：exec 动作带
`{backend, declared, measured, level, unverified, contradicted, boundaries,
enforced_boundaries}`，非 exec 动作为 `null`。它是"批不批这条命令"的输入，
不是执行后的通知——执行结果里的同一份视图在 `tool.result.output.isolation`。
映射优先级（contradicted > unverified > 部分边界 > none）见
`ui/src/lib/humanize.ts::isolationSentence`。

`RunView.started_at` 是 `run.start` 落库时间（`runs.created_at`），回答的是"这个 Run
什么时候被要求开始"，**不声称**"第一轮模型调用发生在那一刻"——投影里没有后者这个
字段，编一个出来只会让排障时对不上时间线。

### 3.5 推送与游标

- `subscribe {since_seq}`：服务端先按 `seq >= since_seq` 补历史，再挂上实时推送。
- 每条推送都带 `seq`，客户端据此推进游标；**断线重连必须带 `since_seq`**，
  不是"从头再来"，也不是"只等新事件"。
- 事件日志是唯一事实源：UI 刷新/重启后，用 `state` + `events.run` 重建，
  不依赖任何内存态。

### 3.6 断线与超时的可判别语义

"这一刻没有消息"和"连接已经断了"必须能分开。读循环如果只判 `None`，对端一断开
`poll()` 就会立刻报可读、`recv_bytes()` 立刻 EOF，循环于是变成 100% CPU 的空转
——M5 实测踩过：演示里把浏览器关掉，daemon 就持续吃满一个核，直到进程被杀。
所以两条读路径的语义在这里钉死：

| 调用 | 超时 | 对端断开 |
|---|---|---|
| `ServerConnection.recv(timeout=...)` | 返回 `None`，`eof` 仍为 `False` | 返回 `None`，且 `eof` 置 `True`（此后再调用立刻返回） |
| `Client.next_event(timeout=...)` | 返回 `None` | 抛 `ConnectionClosed` |
| `Client.connect(timeout=...)`（握手期） | 抛 `ConnectionClosed`（预算用尽） | 抛 `ConnectionClosed`，**不重试**；stdout/stdlib 的裸 `EOFError` 不外漏 |

握手期的这一行是收尾轮补的（PR #88）：`multiprocessing` 的认证握手 `answer_challenge()`
在"对端 accept 后立刻关连接"时抛的是 **`EOFError`**（不是 `OSError`）；`Server.accept()`
早就把同种情况映射成可重试的 `ProtocolError`，客户端却把裸异常漏给了调用方——
`request_shutdown()` 只捕 `(IpcError, OSError)`，于是在"daemon 已经退出、端点文件还在"
的收尾窗口里被打崩（macOS CI 实测）。现在 `connect()` 把握手期 EOF 映射成
`ConnectionClosed`；**不重试**是刻意的：重试只会把"已经走了"拖成整个 timeout。

由此推出两条写法的约束：

- **服务端读线程**：`recv()` 返回 `None` 时必须再看 `conn.eof`，为真就结束这条
  连接（`daemon._read_loop` 就是这么写的），不能无条件 `continue`；
- **订阅方**（Host 的 `/events`）：`ConnectionClosed` 是终态，要结束 SSE 响应，
  让浏览器按 UI 的重连策略（连续 3 次失败后给手动重试）重来；把它当"这一刻没
  事件"就会一边空转发心跳帧一边烧核，而且浏览器永远等不到流结束。

回归测试钉住这条语义：`tests/test_ipc.py::test_peer_close_is_distinguishable_from_timeout`、
`tests/test_ipc.py::test_subscription_drop_raises_instead_of_returning_none`、
`tests/test_ipc.py::test_handshake_eof_becomes_connection_closed`（POSIX 真 socket：
只 listen、不握手）、`tests/test_ipc.py::test_handshake_eof_is_mapped_to_connection_closed_everywhere`
（打桩 `ipc._Socket`，Windows 也跑）、`tests/test_daemon.py::test_client_disconnect_does_not_spin_a_cpu_core`、
`tests/test_daemon.py::test_request_shutdown_returns_false_when_peer_hangs_up_handshake`、
`tests/test_integration_m5.py::test_event_stream_ends_when_daemon_goes_away`。

---

## 4. 生命周期

### 4.1 启动

1. `tianshu host` 拿 Host 单例锁（`run/host.lock`，PID + 启动时间）。
2. 探测 Daemon：`ipc.probe(data_root)` 为真 → 复用；否则 `spawn`
   `python -m tianshu daemon --data-root ...`，等端点就绪（上限 15s）。
3. Daemon 启动顺序：Daemon 锁 → **恢复扫描** → `ipc.Server.start()` → accept 循环
   → 驱动线程。顺序不能换：先 bind 会让人以为"能连上就是能干活"，而恢复还没做完。
4. Host 建 HTTP 服务：绑定 `127.0.0.1:0`（随机端口），把
   `http://127.0.0.1:<port>/` 打到终端——**URL 不带 token**（token 由页面从
   `<meta name="tianshu-token">` 读；URL 会进浏览器历史、shell 历史、聊天记录和
   截图，把访问权限放进去等于顺手分享钥匙）。`--open` 时调系统浏览器。
5. Host 把 token 注入 `index.html`（`<meta name="tianshu-token">`），其余静态资源
   原样返回。

### 4.2 运行期

- **Daemon 驱动线程**：循环 `worker.run_once()`（认领 → resume → 结算/挂起）；
  空闲时 50ms 轮询；不依赖任何客户端。
- **审批决定后自动续跑**：`approvals.decide` 成功且 Run 在 `waiting_approval` 时，
  Daemon 立刻把作业重新入队并由驱动线程接手（`resumed=true`）。人点完"允许"，
  任务应当继续，而不是要人再点一次"继续"。
- **UI 断开**：只减少订阅者。Host 的 SSE 端点断开后，该连接从推送集合移除；
  Daemon 不因此停止任何 Run。
- **驱动线程保底**：连续 10 次异常且期间没有任何一次成功驱动时，Daemon 判定
  "驱动已经坏死"——打一条日志并停止（交给 Host 按 §4.3 重启）。不这么做的话，
  数据库锁冲突、线程归属错这类错误的表现是"任务不动"，比崩溃还难查。

### 4.3 退出

| 场景 | 行为 |
|---|---|
| 关浏览器 / 刷新 | 无影响 |
| `tianshu host stop` | CLI `POST /shutdown`（**只接受 POST + token**，GET 回 405）→ Host 先让 Daemon 走 `shutdown{reason:"user"}`：停驱动线程 → 关连接 → 关 listener → 释放锁 → 退出（等 5s，超时杀进程树）；**Daemon 收掉之后**才关 UI 桥、清 `host.lock`、放主线程退出 |
| Host 被杀 | Daemon 是独立进程，继续跑到下一次显式 shutdown（这正是"关 UI 任务继续"） |
| Daemon 崩溃 | Host 监护：指数退避重启（1s/2s/4s/8s，上限 30s），重启时走 §4.1 的恢复扫描 |
| 机器重启 | 下次启动时锁文件是陈旧的（连不上端点），Daemon 接管并恢复 |

**顺序即契约**（两条都由真子进程测试钉住）：

- `/shutdown` 只接受 POST：`tianshu host stop` 发的就是 POST，端点若挂在 GET 上，
  stop 永远 404，用户看到的是"Host 未在超时内退出"；
- 清理回调挂在 **server 实例**上而不是 handler 类属性上：普通函数成为类属性会被
  描述符协议绑定，调用时多塞一个 handler 实例，`TypeError` 让清理静默不执行；
- 主线程必须等"Daemon 已收掉"才退出：先放主线程，进程结束会把清理线程一起带走，
  `stop` 报"Host 已退出"，Daemon 却留成孤儿继续接活。

### 4.4 单实例

不是靠一个好用的 PID 文件——PID 会复用，锁文件会残留。判据是**"端点能不能连上"**：

```
锁文件存在？
 ├─ 能连上端点 → 有活的 Daemon：第二个实例拒绝启动（Host 直接复用）
 ├─ 连不上（残留文件/陈旧锁）→ 接管：删 socket、写新锁、bind
 └─ 锁文件不存在 → 正常启动
```

两个实例同时抢锁的竞态窗口由 `O_CREAT|O_EXCL` 保证只有一个能创建锁文件；
创建成功后再做一次 `probe`，若此时发现别人已经 bind（极小窗口），就退让并复用。
**测试覆盖这条竞态**（`tests/test_daemon.py`）。

---

## 5. 重启恢复（本里程碑真正的判据）

Daemon 启动时做三件事，顺序固定：

1. **恢复扫描**：对库里状态为 `running` / `waiting_approval` / `reconciling` /
   `cancelling` 的 Run 逐个看事件：有 intent 无 result 的调用交给既有
   `recover_run` 核对（能确认没执行的补 rejected；能确认执行了的补 receipt；
   两边都确认不了的落 `unknown` 并阻断该工作区的后续写入）。
2. **重新排队**：`queued` 的作业、以及"Run 在等人/等审批但作业已 park"的，
   按状态决定重新入队还是保持等待。**不要**把 `waiting_approval` 的 Run 续跑——
   那等于绕过审批。
3. **推送补洞**：`event_seq_head` 让每个新订阅者从自己的游标补齐。

**恢复的每一步都必须是幂等的**：Daemon 可能连续重启两次，第二次不能因为第一次
已经补过 result 而重复派发。这条由 dispatch 记录 + `tool.result` 的唯一性保证，
M5 只做"按顺序调用"，不新增判定。

---

## 6. UI 的边界（写给前端）

- 业务状态**只能**来自事件日志 + `state`；不许在前端维护"我猜任务在跑"的状态。
- 断线要**主动告诉用户**：「与守护进程的连接已断开，正在重连」；重连成功后带
  `since_seq` 补齐，不整页刷新。
- 审批卡片的信息量以"普通人能判断"为准：动作、目标、为什么需要确认；不显示
  `digest`、`request_id`、枚举值。内部细节放折叠区。
- `effect_state=partial` 必须原样显示成「已停止，但部分修改已生效」，不能只说
  "已取消"——那是在骗用户（PRD §4.3）。
- 成本只做"累计了多少"，不做计价解释（F13）。

---

## 7. 安全边界

| 面 | 措施 | 不承诺 |
|---|---|---|
| Host HTTP | 只绑 `127.0.0.1`、随机端口、token 必须匹配（页面同源注入）；校验 `Host` 头 | 防不住本机恶意进程直接读内存/改程序 |
| 静态资源 | 只服务 `ui/dist` 下的文件，禁止 `..` 越界 | |
| IPC | 端点文件权限 0600/0700 + `authkey`；Windows 命名管道 | |
| 审批决定 | 只有 `approve`/`reject`；决定者身份写事件；`approve` 一次性消费 | |
| 日志 | token 不进日志、不进事件 payload | |

---

## 8. 错误与重试口径

- **不要重试幂等性未知的请求**：`run.start` 超时后重试可能建出第二个 Run。
  Host 在这类请求上用"先查再发"（`run.list` 看有没有同 `out_name` 的 Run）。
- `call()` 超时意味着"不确定服务端是否处理了"，不是"没处理"。M5 的选择是：
  请求带客户端生成的自然键（`out_name` / `approval_id`），重复请求天然幂等；
  没有自然键的（`task.create`）由 UI 禁用重复提交。
- Daemon 关闭时正在处理的请求：连接先关闭，客户端看到 `ConnectionClosed`；
  已经落库的变更不会回滚——这正是"事件日志是事实源"的含义。

---

## 9. M5 明确不做（写在这里，避免下一版读成"应该是做了"）

- Tauri / WebView2 外壳、系统托盘、开机自启、单实例 `open` 事件转发（D1/D2/D5）
- 安装包、签名、公证、自动更新（TECH-ARCH §8）
- OS Keychain（F12 仍是环境变量后端，请求 Keychain 报 `unsupported`）
- Computer Use（macOS 优先，v1 未实现）
- E2 生成作业（只保留类型定义）
- 多窗口 / 多用户
- 记忆与知识（F10，P1）
- 评测器的真实换模分数（M4 §7 的未验证项，与 M5 无关但同样没做）

---

## 10. 已知限制与取舍（M5 实测中确认，不是设想）

1. **Daemon 生命周期不写事件日志。** C3 的事件类型是封闭集合、且每条事件都挂
   `run_id`；`shutdown(reason)` 是进程级事实，硬塞进 run 作用域的日志会同时污染
   "按 run 回放"与事件注册表。取舍：`reason` 只进日志文件（`logs/daemon.log`），
   排障时看文件。要进事件日志就得新增 `daemon.*` 族并走 `docs(contract)`，留给后续工单。
2. **陈旧锁判定有 2 秒宽限窗。** 锁"新鲜"用 `started_at` 距今 <2s 判断；若第一个
   Daemon 在"拿到锁 → bind"之间耗时超过 2s（超大库的恢复扫描），第二个实例理论上
   可能误判陈旧并接管。TECH-ARCH §2.2 要求"PID + 启动时间匹配"更稳，但跨平台读取
   进程启动时间没有 stdlib 方案（Windows 需 WMI/PowerShell），属单独工单。
3. **推送在主循环里同步发送。** 一个不读数据的订阅者理论上能拖慢 RPC 响应。彻底
   解法是独立发送线程 + 非阻塞写；M5 用"本地 socket + 每批 256 条"把风险压在可
   接受范围，没有做这一层。
4. **驱动线程卡死 >5 秒时，连接刻意不跨线程关闭。** 关闭一条正在被别的线程使用
   的 SQLite 连接只会得到没人看得到的 `ProgrammingError`，还可能截断一次写入。
   取舍：保留连接，交给进程退出回收；测试钉住"shutdown 仍返回、锁已释放、没有
   ProgrammingError"。
5. **Windows 命名管道只做了 CI 覆盖，没有实机验证。** 传输层按 `ipc.py` 的抽象写，
   `windows-latest` 会跑同一套测试；但"实机"与"runner"的差异（杀软、ACL、长路径）
   不在本里程碑的验证范围内。
6. **同一毫秒的并发审批未压测。** 控制面是单线程串行处理，语义上天然不会并发；
   但"两条连接同一瞬间分别 approve/reject 同一张卡"只验证了先后顺序，没有做压力测试。
7. **`approvals.decide` 的 `reason` 被忽略。** 见 §3.4 表：审批记录没有自由文本字段，
   要存就得改 `schemas/m2/approval.json`（契约变更）。当前它只用于 UI 展示。
