# M5 就绪竞态（issue #63）：根因、修复与证据

> 分支 `fix/m5-readiness`，基线 `main@540c543`（`1080 passed / 3 skipped`）。
> 本文只记录**实际跑过的命令与输出**；没跑过的写成"未验证"，不写成已修。

---

## 平台差异（ubuntu CI 抓到的反例）

`backlog 满时 connect 立即 ECONNREFUSED` 是 **macOS 的 AF_UNIX 语义**；Linux 的
backlog 只是提示，同样的连接会排队/阻塞而不是被拒。所以那条"队列满时必须如实
报 False"的真实 backlog 用例在非 macOS 上 skip（不是"绿不了就不测"：重试逻辑本身
改由 `test_probe_result_retries_transient_errors_deterministically` 用确定性桩
覆盖——第一次 `ECONNREFUSED`、第二次连通，`attempts>=2`；`retry_ms=0` 一次都不
重试；`ENOENT` 不重试）。

这条差异是 CI 在 ubuntu 上跑出来的（`assert True is False`），不是猜的。

## 0. 一句话结论

**根因不是"on_ready 早于 listen"，而是"探活方式"与"监听队列容量"的组合缺陷：**
`multiprocessing.connection.Listener` 的默认 `backlog=1`
让"一次 `probe()` 自己留下的未认证死连接"就能填满监听队列；队列满时 macOS 对
AF_UNIX 的 `connect` 直接返回 `ECONNREFUSED`。于是出现"端点文件存在、守护进程
活着、`probe` 连不上"——看起来像没监听，实际是"监听着，但队列被探活自己堵死"。

修复做三件事，**没有加 sleep、没有延长等待**：
1. 监听队列容量改为 `LISTEN_BACKLOG = 64`（容量问题用容量解决）；
2. `probe` 增加"端点存在但 connect 失败"的短暂重试 + 失败原因（`ProbeResult`），
   并把 `ENOENT`（真不存在）与 `ECONNREFUSED`（在监听但此刻队列满）分开；
3. 把"就绪"判据从"`on_ready` 被调用"收紧为**顺序证明**：`Server.start()` 已
   bind+listen → accept 线程真的进入 `accept()`（`wait_accepting`）→ 一次完整
   认证握手自连成功（`self_connect`）→ 才允许 `on_ready`；accept 线程致命退出
   必须带着原因让 daemon 退出，不许静默变僵尸。

---

## 1. 现象与历史数据（来自 issue #63 与 CI）

- `tests/test_integration_m5.py::test_crashed_daemon_recovers_run_without_duplicate_side_effects`
  首段偶发红：`on_ready` 已落盘（`daemon-ready` 存在），但 `ipc.probe` 10 秒内
  一直 False；子进程 `returncode=None`（活着）、端点文件 `exists=True`、日志为空。
- 历史复现率：开发者本机**文件级 32 次 2 次失败**；pristine `main` 80 次 2 次；
  CI（macOS runner，PR #65 只改一个 md 文件）也出现 1 次。
- 本轮拿到的 CI 原始断言（Lead 转贴）：

  ```text
  AssertionError: on_ready 已落盘但端点连不上；returncode=None，
  endpoint=/tmp/m5int-2zvp9zuo/run/agent.sock，exists=True
  1 failed, 1079 passed, 3 skipped in 188.16s
  ```

- 关键推论（当时只能到这一步）：`returncode=None` + `exists=True` + connect 失败
  ⇒ 进程活着、端点对象在、但"此刻不接受连接"。**没有观测能区分**"还没 listen"
  "accept 线程死了""队列满"——这正是需要先做探针的原因。

---

## 2. 先复现：探针与观测

> 目的不是"证明它偶发"（已知），而是拿到**故障瞬间的状态**并分清三种可能。

### 2.1 现场还原（诊断副本，不改被测语义）

把该用例复制成 `tests/_diag_integration_m5.py`（临时文件，未提交），子进程脚本
额外 `faulthandler.register(SIGUSR1, all_threads=True)`，失败时 dump 子进程全部
线程栈 + lsof。连续复跑得到同一形态，**子进程日志不再为空**：

```text
AssertionError: on_ready 已落盘但端点连不上；returncode=None，
endpoint=/tmp/m5int-.../run/agent.sock，exists=True；日志尾巴：
  [tianshu-daemon] 接受连接失败（继续监听）：连接在握手阶段失败（可重试）：[Errno 32] Broken pipe
  [tianshu-daemon] 接受连接失败（继续监听）：连接在握手阶段失败（可重试）：[Errno 32] Broken pipe
```

这条日志推翻了两个候选根因：

- **不是"accept 线程死/没起来"**：它正在 accept，并把探活连接按"可重试"处理；
- **不是"bind 了还没 listen"**：`Broken pipe` 出现在 `Listener.accept()` 的
  challenge/response 阶段——只有 socket 已经 listen、连接能被 accept 进应用层，
  才会走到"握手阶段失败"。

### 2.2 背靠背探活探针（真实 daemon 子进程，120 轮）

`/tmp/flap_probe.py`：每轮 `serve()` 起真子进程 → 等 ready → **连续两次**
`ipc.probe()`（模拟测试里"循环最后一次 probe 成功后紧接 assert probe"）。

修复前（`main@540c543` + 未改代码）：

```text
rounds=120 flips(True->False)=6
   trial=19 probeA=True probeB=False raw_after=True/OK(0.0000s)
            probeA2=False raw3=False/errno=61 [Errno 61] Connection refused(0.0000s)
   trial=55 probeA=True probeB=False raw_after=False/errno=61 ...
   ...
```

- **6/120 = 5%** 的"第一次连上、紧接着连不上"，与历史 ~2/32 ≈ 6% 同一量级；
- 失败 errno 是 **61 (ECONNREFUSED)**；
- 失败瞬间再探有时又能连上（`raw_after=True/OK`）——**端点一直是活的**，是队列在
  一进一出之间被填满。

修复后同一探针：

```text
rounds=120 flips(True->False)=0
```

### 2.3 裸 `Server` + 无 accept 线程（把机制缩到最小）

```python
server = ipc.Server(root); server.start()
# 不起 accept 线程：
[ipc.probe(root) for _ in range(2)]
```

修复前失败的形态是 `[True, False]`：第一次探活连接（连上就关、不做认证握手）
占满 `backlog=1` 的唯一队列位，第二次 connect 立刻 `ECONNREFUSED`。修复后
`[True × 8]`（见 `tests/test_ipc.py::test_start_returns_with_listening_endpoint_even_without_accept_loop`）。

### 2.4 饱和探活（解释"10 秒都连不上"）

修复前：4 条线程持续做 `connect+close`（模拟并发客户端/监护探活），主线程每
20ms `probe` 一次，8 秒内：

```text
false 次数最大连击 = 51
```

即：队列可以长时间保持"满"，`probe` 连续 51 次 False。**这就是"10 秒都连不上"
的形态**——不是永久坏了，而是探活自己被队列堵在门外。

修复后同一实验：

```text
修复后 8 秒探活：总计=57163，失败=0，最大连续失败=0
```

### 2.5 机制链条（根因）

1. `multiprocessing.connection.Listener(..., backlog=?)` 默认 **backlog=1**；
   本项目 `ipc.Server.start()` 没有显式给容量。
2. `ipc.probe()` 的语义是"**连一下、关掉**"（有意不做 authkey 握手，见其注释）。
   服务端 `Listener.accept()` 在返回连接前要跑 HMAC challenge/response；探活
   客户端不发响应就关，服务端必须先失败一次握手（`Broken pipe`）才能腾出队列位。
3. 修复前的 accept 循环把这种失败当"可重试"记一行日志再继续——**这个语义是
   对的**（探活连接本来就是空连接），但它默认"队列位永远够用"。
4. 队列满时，macOS 的 AF_UNIX `connect` **立即 ECONNREFUSED**（实测 errno=61），
   不是阻塞等；于是下一次 `probe` 在几十微秒内就失败。
5. 测试/监护的探活频率高于"accept 线程失败一次握手并回到 accept"的周转时间时，
   队列长期占满 → 连续 False；偶发红只是"最后一次成功 probe 后的紧接 assert
   恰好撞上满队列"（5%）。

**为什么 `on_ready` 的顺序不是直接根因**：`Listener.__init__` 内部同步
`bind()` + `listen()`，`on_ready` 在其之后才被调用；2.1 的 `Broken pipe` 也证明
listen/accept 都在。就绪标记之所以"看起来撒谎"，是因为它当时证明的只是
"start() 返回了"，没有证明"端点此刻能被服务"。

---

## 3. 修复（顺序 + 容量 + 可观测，不靠等待）

### 3.1 `src/tianshu/ipc.py`

- **`LISTEN_BACKLOG = 64`**：`Server.start()` 建 `Listener(..., backlog=...)`。
  backlog 是容量，不是调优——一条探活死连接不该能堵死整个端点。
- **`ProbeResult` + `probe_result()`**：返回 `ok / endpoint / reason / errno /
  endpoint_exists / attempts`。`probe()` 保留 bool 语义，是它的包装。
  - `ENOENT`：端点不存在 → 立即 False，**不重试**（"没有 daemon"不该变慢）；
  - `ECONNREFUSED / ECONNRESET / ETIMEDOUT / EAGAIN / EINTR` 且端点文件存在：
    在 `PROBE_RETRY_MS = 250ms` 预算内短睡重试（窗口来自 accept 队列排队，
    不是网络抖动）；预算用完仍失败 → False，调用方照旧走"陈旧锁接管/重启监护"。
  - Windows：命名管道没有 errno 分层，保持一次判定（不把 #50 的握手问题与
    "真没实例"混在一起）。
- **`self_connect()`**：自己连自己一次、**走完整认证握手**。`probe()` 只证明
  "listen 了"（内核可以替你在没人 accept 时排队），握手完成才证明
  "accept 循环在应用层是通的"。用旁路线程 + timeout 兜底，避免启动路径被一次
  卡住的握手拖死。
- **`Server.accepting` / `wait_accepting()`**：`accept()` 进入时先置事件再阻塞；
  `on_ready` 的判据是"线程真的站在 accept() 里"，不是"线程对象 start() 过"。

### 3.2 `src/tianshu/daemon.py`

- 新增 `_await_endpoint_ready()`，在 `_setup()` 里、`on_ready` 之前：
  1. `server.wait_accepting(ACCEPT_READY_TIMEOUT_S)`；
  2. 循环 `ipc.self_connect(...)`，直到成功或 `SELF_CONNECT_BUDGET_S` 用尽；
  3. 任何一步失败抛 `IpcError`（进程以非 0 退出 + 日志），**绝不**留下
     "标记了 ready 但连不上"的守护进程。
- accept 循环区分两类失败：
  - `ProtocolError`（握手失败/探活连接）：可重试，记一行日志继续（不变）；
  - 其它 `IpcError` / `OSError`：监听端点不可用 → `_fail_accept()` 置停止标志；
  - 外层 `try/except/finally` 兜住任何异常/静默退出：`accept_error` 记原因，
    `run()` 在 `_shutdown()` 之后把它抛给调用方，Host 监护据此重启并留日志。
- 文档同步：`on_ready` 的契约从"端点已经能连之后"改成
  "bind+listen、accept 进入循环、自连握手成功之后"。

### 3.3 `src/tianshu/host.py`

- 启动超时的 `HostError` 带上 `probe_result` 的 `reason / errno /
  endpoint_exists / attempts`；新增 `DaemonSupervisor.probe_detail()`（`alive()`
  仍是其布尔包装）。"没就绪"不再是一句没有现场的话。

### 3.4 `tests/test_integration_m5.py`

- 失败断言带 `probe_result` 的 reason/errno/attempts（现场自解释）；
- ready 之后**连续 8 次背靠背探活**必须全过（#63 的集成级回归）。

### 为什么这不是"修等待时间"

- 没有新增任何 `sleep`、没有调大超时；重试只覆盖"端点存在但 connect 失败"这
  一个语义，预算固定 250ms，且**不改变"能连上才算活"的判据**；
- 队列容量是结构性修复：探活连接不再能堵死端点；
- 就绪判据是顺序修复：`on_ready` 之后"端点可服务"由构造保证，不靠概率。

---

## 4. 回归测试与负控

新增/加强的测试（全部确定性，不靠 sleep 猜时序）：

| 文件 | 用例 | 钉住什么 |
|---|---|---|
| `tests/test_ipc.py` | `test_start_returns_with_listening_endpoint_even_without_accept_loop` | `start()` 返回即 listen；无 accept 线程时背靠背 8 次探活全过（backlog 回归） |
| `tests/test_ipc.py` | `test_probe_result_separates_missing_from_not_listening` | ENOENT vs ECONNREFUSED 的 reason/errno/attempts 分层 |
| `tests/test_ipc.py` | `test_probe_retries_when_endpoint_exists_but_accept_is_behind` | 队列满时 probe 在预算内重试；`retry_ms=0` 时必须立刻 False |
| `tests/test_ipc.py` | `test_self_connect_requires_a_live_accept_loop` | 自连必须完成认证握手，没 accept 线程时失败 |
| `tests/test_ipc.py` | `test_server_exposes_accepting_state` | `accepting` 反映"线程真的进了 accept()" |
| `tests/test_daemon.py` | `test_on_ready_waits_for_self_connect_proof` | 自连探测未通过时 `on_ready` 不许落盘（顺序回归） |
| `tests/test_daemon.py` | `test_on_ready_is_not_called_when_endpoint_never_proves_connectable` | 永远不可服务 → 不调 on_ready、抛 IpcError、释放锁 |
| `tests/test_daemon.py` | `test_accept_thread_fatal_error_stops_daemon_and_reports_reason` | accept 致命退出 → daemon 退出并带原因（不静默僵尸） |
| `tests/test_daemon.py` | `test_handshake_failures_on_accept_are_retryable` | 握手失败仍是可重试，不升级成致命 |
| `tests/test_integration_m5.py` | 崩溃恢复用例 | ready 后连续 8 次探活全过 + 失败带 reason/errno |

### 负控（回退修复后必红，实测输出）

**负控 1：`LISTEN_BACKLOG = 1`（回退容量修复）**

```text
E  AssertionError: 背靠背探活出现失败（backlog 太小）：
   [True, False, False, False, False, False, False, False]
1 failed in 1.79s
```

**负控 2：去掉 `_await_endpoint_ready()`（回退顺序修复）**

```text
E  AssertionError: 自连探测还没通过，on_ready 就已经落盘——就绪标记在撒谎
1 failed in 0.28s
```

**负控 3：`_PROBE_TRANSIENT_ERRNOS = frozenset()`（回退重试语义）**

```text
E  AssertionError: 没有等到 accept 腾出队列：
   ProbeResult(ok=False, reason='端点文件存在但无人监听（连接被拒）',
               errno=61, endpoint_exists=True, attempts=1)
1 failed in 0.08s
```

三处回退后恢复、再跑对应用例：

```text
... 3 passed
```

---

## 5. 稳定性验收：同一用例文件 100 次文件级运行

命令（CI 同口径）：

```bash
for i in $(seq 1 100); do
  PYTHONPATH=$PWD/src python -m pytest -o addopts="" -q tests/test_integration_m5.py
done
```

### 结果（2026-11-11，macOS arm64 本机；当时 load average ≈ 5，非静默环境）

命令（CI 同口径，逐次记录退出码）：

```bash
cd /Volumes/Date/ts-wt-ready
for i in $(seq 1 100); do
  PYTHONPATH=/Volumes/Date/ts-wt-ready/src \
    /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q \
    tests/test_integration_m5.py
done
```

结果：**100/100 rc=0，0 次失败**（`summary.txt` 100 行全部 `rc=0`；没有
`FAILED_*` 日志）：

```text
100 summary.txt
 100 rc=0
FAILED_*: 0
```

单次耗时 min=18.85s / median=19.14s / max=19.79s（n=100），每次都是
`10 passed`（该文件 10 条用例全过）。对照：修复前同文件级 **32 次里 2 次失败**
（issue #63 记录），修复后 **100 次 0 次**。

---

## 6. 全量回归

```bash
cd /Volumes/Date/ts-wt-ready
PYTHONPATH=/Volumes/Date/ts-wt-ready/src \
  /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q
```

```text
1089 passed, 3 skipped in 109.90s (0:01:49)
```

```bash
/Volumes/Date/天枢Agent/.venv/bin/ruff check .
```

```text
All checks passed!
```

基线 `main@540c543` 为 `1080 passed / 3 skipped`；本轮新增 **9** 条确定性用例
（`test_ipc.py` +5、`test_daemon.py` +4），集成用例是**加断言**而非新增；
skip 数与基线一致（3 条平台/环境跳过，本工单未新增 skip）。

---

## 7. 边界与仍未覆盖

- **Windows 实机未验证**：Windows 走命名管道，`probe()` 没有 errno 分层（代码里
  显式保留单次判定）；`#50` 的偶发握手失败与本修复无关、也未被本修复声称解决。
  负控里只有 `start()` 的 backlog 对 AF_UNIX 生效；Windows 的实例队列语义没有
  本机证据。
- **`self_connect` 超时后可能留下一个旁路线程**：`multiprocessing.connection` 的
  客户端握手没有超时参数，用线程 join 兜底；超时只放弃等待，线程会在服务端关闭
  连接后自行退出。daemon 启动失败路径随后就是进程退出，不会累积。
- **`PROBE_RETRY_MS=250ms` 是判据的一部分**：它会把"端点存在但暂时不可连"的
  判定延长最多 250ms（陈旧锁接管/监护重启会感知到）；这是用"最多 250ms 的
  判定延迟"换"不为瞬时队列窗口丢一次探活"，不是无界重试。
- **修复范围只到 M5 的本地 IPC**：没有改 WebSocket/Host 的 HTTP 语义，也没有
  改进推送路径的同步发送（那是 KNOWLEDGE §7 的另一条已知限制）。
- **`on_ready` 的调用方语义未变**：Host 仍然靠 `probe` 轮询 + 守护进程日志做
  监护；本修复让"ready 落盘"成为可信信号，但没有把 Host 改成等 ready 文件
  （那会改 Host 生命周期契约，超出本工单）。

---

## 8. 判据（什么样才算修好）

1. `tests/test_integration_m5.py` 整文件 **100 次文件级运行 0 失败**（§5）；
2. 全量 `1089 passed, 3 skipped` + `ruff check .` 干净（§6）；
3. 三条负控各自能打红对应的确定性用例（§4）；
4. 失败现场自带 `reason/errno/attempts/endpoint_exists`，下一次再出问题不必靠猜。