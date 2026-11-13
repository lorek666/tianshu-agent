"""长驻守护进程：任务状态、事件日志、AgentLoop 驱动、审批与恢复。

M5 的退出条件只有一句话：**关掉界面任务继续跑，重启后恢复**。这个模块就是那句话
的实现。四条必须同时成立的性质：

1. **单实例靠"端点能不能连上"，不靠 PID 文件。** PID 会复用，锁文件会残留；
   `ipc.probe()` 真的连一次才回答"这条端点后面有没有活着的监听者"。锁
   （`run/daemon.lock`，JSON：pid / started_at / instance_id）只是给"谁先抢到"
   一个 `O_CREAT|O_EXCL` 的线性化点：抢到之后再探一次活，发现别人已经 bind
   就退让（抛 `ipc.IpcError`，由 Host 决定复用）。陈旧锁（连不上）可以接管。
2. **启动顺序不能换**：锁 → 恢复扫描 → `ipc.Server.start()` → accept → 驱动线程。
   先 bind 会让人以为"能连上就是能干活"，而恢复还没做完（契约 §4.1）。
3. **驱动线程与控制线程各持一份 SQLite 连接。** SQLite 连接默认禁止跨线程
   使用，共享一条连接只会得到偶发的 `ProgrammingError`。两套 `Stack` 指向同一个
   WAL 库，各自在各自线程里用，写事务靠 `BEGIN IMMEDIATE` + `busy_timeout` 串行化。
4. **shutdown 5 秒内走完**：停 accept → 停驱动 → 关连接 → 关 listener →
   释放锁 → 关两条日志连接。任何一步卡住都不许拖住进程退出：线程都是 daemon
   线程，`serve()` 的 finally 一定会跑到"释放锁"这一步，否则下次就起不来。

## 恢复语义（§5）——保守优先

启动扫描只处理**非终态**的 Run。保守体现在三处：

- `waiting_approval` 一律不动：自动 resume 等于绕过审批（§5 明写）。
  （它的作业在停下时已经被 `Worker` park 成 `waiting`，`claim_next` 不会认领它，
  所以"不动"是结构性成立，不是靠自觉。）
- `running` / `reconciling` / `cancelling` 里有"有 intent 无 result"的调用时，交给
  既有 `recovery.recover_run` 核对。核对结论只可能来自写前证据；确认不了就是
  `unresolved`，**不许当成功**。
- 确认不了时把 Run 落到 `reconciling` 并**把死 owner 的作业 park 掉**。只改状态
  不够：崩溃留下的作业还是 `claimed`，驱动线程的 `claim_next` 会把它当"可接管"
  捡起来，然后 `resume_run` 把 reconciling 改成 running——人还没看，AI 就继续
  写了。这条是"不许把不确定当成功"能不能落地的分界线。

## 事件推送：控制面不能被一个慢订阅者拖死

`subscribe {since_seq}` 只登记游标；历史补读与增量推送统一由主循环每轮从全库
`seq` 读 (`read_after`) 并逐连接入队。**主循环不写 socket**：每条连接有一个
有界发送队列 + 独立发送线程（`_ClientConnection`），队列满 / 发送超期 ⇒ 断开
该订阅者并记 `slow_subscriber_dropped`，其他连接与请求处理完全不受影响。
订阅者重连带 `since_seq` 补读，审计事实仍在事件日志里，不依赖这条连接。

`ipc.ServerConnection` 的 `send` 不是线程安全的；现在每条连接只有它自己的发送
线程会调用它，不存在并发写同一帧的问题（此前靠一把锁，代价是主循环会被锁在
一次阻塞写上）。

## 不做什么

- 不为 daemon 生命周期新增事件类型：C3 是封闭契约，退出原因只进 stderr 日志。
- 不在驱动线程里重新判定策略/写文件：那是 `AgentLoop` + `Dispatcher` 的事。
"""

from __future__ import annotations

import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ipc, metrics
from .control import DAEMON_VERSION, ControlError, ControlPlane, event_view
from .errors import TianshuError
from .events import EventLog
from .jobs import JobLost, JobStore, JobUnavailable, renew_claim
from .leases import DEFAULT_TTL_MS, HEARTBEAT_MS, LeaseStore, ProcessIdentity
from .loop import AgentLoop, Limits, Stack, build_stack
from .model import Model
from .recovery import recover_run
from .tasks import (
    RUN_CANCELLING,
    RUN_FENCED,
    RUN_RECONCILING,
    RUN_WAITING_APPROVAL,
    TERMINAL_STATES,
)
from .worker import HeartbeatLoop, Worker

#: 单实例锁文件名（与端点同目录）。内容只有三样：谁、什么时候、哪个实例——
#: 用来回答"这个锁是不是上一次崩溃留下的"，不做身份认证。
LOCK_NAME = "daemon.lock"

#: 判断"陈旧锁"之前的宽限：另一个 daemon 可能刚建好锁、还没 bind。
#: 这段窗口里我们**不**接管，只是等；锁创建者的 `started_at` 一旦超过它，
#: 就说明它没能活到 bind（崩溃/被拖住），可以接管。
LOCK_GRACE_MS = 2_000

#: 抢锁的最多尝试次数（含等宽限窗与陈旧接管）。防止两个进程互相删对方锁时
#: 无限打转——宁可明确失败，也不在启动路径上永远转。
LOCK_MAX_ATTEMPTS = 80

#: 主循环轮询间隔：请求队列、事件推送、停止标志都靠它推进。20ms 对本地 UI 足够，
#: 又不会让空转吃掉一个核。
MAIN_POLL_S = 0.02

#: 每轮主循环最多处理多少个请求。一个客户端狂刷请求不该把推送饿死。
REQUESTS_PER_ROUND = 64

#: 一次读多少条事件用于广播/补读。分批是为了让 shutdown 标志有机会被看见。
PUSH_BATCH = 256

#: 每条连接的**有界**发送队列深度。推送是广播，慢订阅者既不能把主循环堵住，
#: 也不能让内存无界增长：队列一满就判定这条连接跟不上，断开并记指标。
#: 订阅者重连时按 `since_seq` 游标补读，审计事实不丢。
SEND_QUEUE_MAX = 1024

#: 队列积压超过这个比例就停止向该连接入队（背压），但仍给它一个期限追上来。
#: 直接"满了就断"会把只是短暂落后的健康客户端误杀；"永远等"则会把广播变成
#: 无界内存。折中是：超过 watermark 后只在 `SUBSCRIBER_BACKLOG_STALL_S` 内等待。
SUBSCRIBER_BACKLOG_WATERMARK = 0.75

#: 发送线程被单帧写阻塞超过这么久 ⇒ 慢订阅者（对端不读 socket）。
SEND_STALL_S = 5.0

#: 队列一直高于 watermark 超过这么久 ⇒ 慢订阅者（入队快于发送）。
SUBSCRIBER_BACKLOG_STALL_S = 5.0

#: 取消计时的兜底：超过它还没到终态就记 `null`（unknown），不虚构数字。
CANCEL_STOP_TIMEOUT_S = 60.0

#: 关闭时给所有连接的总排空预算（把已入队的响应/推送尽量写出去）。
CONNECTION_FLUSH_TIMEOUT_S = 1.0

#: 哪些断开原因算"慢订阅者"（记 `slow_subscriber_dropped`）。
#: 正常关 UI / 客户端 EOF / 服务端主动关闭都不该算进这个指标——否则它会变成
#: 一个只会涨、看不出问题的计数器。
_SLOW_DROP_REASONS = frozenset({"queue_full", "send_stall", "backlog_stall"})

#: 关闭时各线程 join 的上限。契约 §4.3：5 秒内退出。
JOIN_TIMEOUT_S = 5.0

#: 等 accept 线程进入 `accept()` 的上限。它只是"线程被调度起来"，
#: 正常是微秒级；给 10s 是为了慢 runner/高负载，不是拿它当重试间隔。
ACCEPT_READY_TIMEOUT_S = 10.0

#: 自连探测的总预算：在端点已经 listen、accept 线程已就位的前提下，一次
#: 完整认证握手正常也是毫秒级；预算只吸收调度抖动。
SELF_CONNECT_BUDGET_S = 10.0

#: 单次自连握手的线程等待上限（`multiprocessing.connection` 的客户端握手
#: 没有超时参数，用旁路线程兜底，避免启动路径被一次卡住的握手拖死）。
SELF_CONNECT_TIMEOUT_S = 1.0

#: 驱动线程连续失败多少次就判定"循环已经废了"并停止 daemon。
#: 比无限刷日志好：UI 至少能看到守护进程退出（Host 会按 §4.3 重启）。
DRIVER_MAX_CONSECUTIVE_FAILURES = 10


def _log(message: str) -> None:
    """日志只进 stderr（Host 重定向到 `logs/daemon.log`）。

    刻意不写事件：C3 的事件类型是封闭集合，daemon 生命周期事件没有登记，
    新增一个会让 schema 校验和回放同时变红。
    """
    print(f"[tianshu-daemon] {message}", file=sys.stderr, flush=True)


def lock_path(data_root: Path) -> Path:
    return ipc.run_dir(Path(data_root)) / LOCK_NAME


# --------------------------------------------------------------------------- 锁


class DaemonLock:
    """一个已持有的单实例锁。只描述"我拿到了"，不描述"别人也在"。"""

    def __init__(self, path: Path, instance_id: str, started_at: int) -> None:
        self.path = path
        self.instance_id = instance_id
        self.started_at = started_at
        self.pid = os.getpid()
        self.released = False

    def payload(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "instance_id": self.instance_id,
            "daemon_version": DAEMON_VERSION,
        }


def _read_lock(path: Path) -> dict[str, Any] | None:
    """读锁文件。坏文件（半截/非 JSON）当"陈旧"处理：它已经不能提供任何保证。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_lock(path: Path, payload: dict[str, Any]) -> None:
    """`O_CREAT|O_EXCL` 写锁：两个实例同时启动时只有一个能创建成功。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _lock_is_fresh(payload: dict[str, Any] | None, path: Path, now_ms: int) -> bool:
    """锁是不是"刚刚建好、正在 bind"的那个。

    优先看锁里的 `started_at`（跨机器/跨时钟都只是同机比较）；读不到就退回
    文件 mtime。两个都读不到时按"陈旧"处理——留着一个谁都不认识的锁没有意义。
    `now < started_at`（时钟回拨/文件来自未来）按新鲜处理，保守地等一会而不是
    立刻接管。
    """
    started: int | None = None
    if payload is not None:
        raw = payload.get("started_at")
        if isinstance(raw, int) and not isinstance(raw, bool):
            started = raw
    if started is None:
        try:
            started = int(path.stat().st_mtime * 1000)
        except OSError:
            return False
    return now_ms - started < LOCK_GRACE_MS


def _unlink_lock(lock: DaemonLock) -> None:
    """只删属于本实例的锁。

    另一个 daemon 接管后会把锁写成它自己的；无条件删文件等于把别人的单实例
    保护拆掉——下一次就有两个实例同时 bind。
    """
    payload = _read_lock(lock.path)
    if payload is not None and payload.get("instance_id") != lock.instance_id:
        return
    try:
        lock.path.unlink()
    except OSError:
        pass


def acquire_lock(data_root: Path) -> DaemonLock:
    """拿到单实例锁；拿不到抛 `ipc.IpcError`（Host 据此复用已有 daemon）。

    判定顺序（§4.4）：
    - 锁文件存在且**端点能连上** → 有活的 daemon，拒绝；
    - 连不上 → 可能是残留（崩溃）或**刚建好还没 bind**：前者过了宽限窗就接管，
      后者继续等；两种都不会误删一个活着的实例的锁。
    - 新建成功后**再探一次**：极小窗口里别人已经 bind 就退让并复用。
    """
    root = Path(data_root)
    run_dir = ipc.run_dir(root)
    run_dir.mkdir(parents=True, exist_ok=True)
    # 先把认证密钥落好：Server 构造时要用它，两个进程同时首次启动由它内部的
    # O_EXCL 保证只有一个能创建。
    ipc.load_authkey(root)
    path = lock_path(root)
    instance_id = f"daemon_{uuid.uuid4().hex[:12]}"
    started_at = int(time.time() * 1000)
    payload = {
        "pid": os.getpid(),
        "started_at": started_at,
        "instance_id": instance_id,
        "daemon_version": DAEMON_VERSION,
    }

    for _ in range(LOCK_MAX_ATTEMPTS):
        try:
            _write_lock(path, payload)
            break
        except FileExistsError:
            if ipc.probe(root):
                raise ipc.IpcError(
                    f"同一个 data_root 上已有活着的守护进程：{root}"
                ) from None
            existing = _read_lock(path)
            if _lock_is_fresh(existing, path, int(time.time() * 1000)):
                # 另一个 daemon 刚开始启动（或刚崩溃，还没过宽限窗）：等。
                time.sleep(0.05)
                continue
            # 陈旧锁：再确认一次端点确实连不上，然后接管。
            if ipc.probe(root):
                raise ipc.IpcError(f"端点已被占用：{root}") from None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ipc.IpcError(f"无法清理陈旧锁 {path}：{exc}") from exc
            continue
    else:
        raise ipc.IpcError(f"抢不到单实例锁（重试 {LOCK_MAX_ATTEMPTS} 次）：{path}")

    lock = DaemonLock(path, instance_id, started_at)
    if ipc.probe(root):
        # 竞态窗口：对方没经过这把锁就 bind 了（旧版本/锁被手删）。
        # 退让并复用——继续启动只会两个实例抢同一个库。
        _unlink_lock(lock)
        raise ipc.IpcError(f"端点已被占用（抢锁后探测）：{root}")
    return lock


# --------------------------------------------------------------------------- 连接


class _ClientConnection:
    """服务端视角的一条客户端连接：**有界发送队列 + 独立发送线程**。

    为什么不再是"主循环里同步 send"：推送是广播，而 `ServerConnection.send` 是
    阻塞写。一个订阅之后不再读 socket 的客户端，会把主循环卡在 `send()` 上，
    于是状态查询 / 审批 / 取消全部排队——控制面被一个 UI 拖死。现在主循环只做
    "把帧放进有界队列"，实际写 socket 的是每条连接自己的发送线程。

    队列满 / 发送超期 ⇒ `drop(reason)`：标记断开、abort 连接唤醒阻塞中的
    发送线程、记 `slow_subscriber_dropped`（由 daemon 在回收连接时记）。
    其他连接与请求处理不受影响；订阅者重连后按自己的 `since_seq` 游标补读。

    顺序性：同一连接的所有帧都从同一个队列按入队顺序写出，所以"subscribe 的
    响应先于历史/增量推送"仍然成立；每条连接只有一个写线程，`send_lock` 不再
    需要（`ipc.ServerConnection.send` 不是线程安全的，所以只允许发送线程调用）。
    """

    __slots__ = (
        "raw",
        "name",
        "subscribed",
        "last_sent_seq",
        "hello",
        "closed",
        "_outbox",
        "_queue_max",
        "_lock",
        "_sending_since",
        "_backpressure_since",
        "_drop_reason",
        "_recorded_drop",
        "_sender",
        "_sent_frames",
    )

    def __init__(
        self,
        raw: ipc.ServerConnection,
        *,
        name: str = "",
        queue_max: int | None = None,
    ) -> None:
        self.raw = raw
        self.name = name or f"conn-{id(self):x}"
        self.subscribed = False
        self.last_sent_seq = 0
        self.hello = False
        self.closed = False
        self._queue_max = max(1, int(SEND_QUEUE_MAX if queue_max is None else queue_max))
        self._outbox: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._queue_max)
        self._lock = threading.Lock()
        self._sending_since: float | None = None
        self._backpressure_since: float | None = None
        self._drop_reason: str | None = None
        self._recorded_drop = False
        self._sent_frames = 0
        self._sender = threading.Thread(
            target=self._send_loop,
            name=f"tianshu-daemon-sender-{self.name}",
            daemon=True,
        )
        self._sender.start()

    # -- 入队 / 断开 --------------------------------------------------------

    @property
    def queue_max(self) -> int:
        return self._queue_max

    @property
    def high_watermark(self) -> int:
        """超过它就先不入队，给发送线程追赶的机会。"""
        return max(1, int(self._queue_max * SUBSCRIBER_BACKLOG_WATERMARK))

    @property
    def drop_reason(self) -> str | None:
        return self._drop_reason

    @property
    def sent_frames(self) -> int:
        return self._sent_frames

    @property
    def drop_recorded(self) -> bool:
        return self._recorded_drop

    def mark_drop_recorded(self) -> None:
        self._recorded_drop = True

    def pending(self) -> int:
        return self._outbox.qsize()

    def send(self, message: dict[str, Any]) -> bool:
        """把一帧放进本连接队列。**不阻塞调用方**；False = 这条连接不可用了。"""
        with self._lock:
            if self.closed:
                return False
        try:
            self._outbox.put_nowait(message)
            return True
        except queue.Full:
            self.drop("queue_full")
            return False

    def drop(self, reason: str) -> bool:
        """标记断开并中断阻塞中的发送。返回是否是本次断开（幂等）。"""
        with self._lock:
            if self.closed:
                return False
            self.closed = True
            self._drop_reason = reason
        # abort 才是关键：发送线程可能正阻塞在一帧写不出去的 send 上，只标记
        # closed 不会让它醒；不醒就永远占着一个线程（长驻进程里是慢性泄漏）。
        try:
            self.raw.abort()
        except (OSError, ValueError, ipc.IpcError):  # pragma: no cover - 已关
            pass
        return True

    def close(self) -> None:
        self.drop("closed")

    def note_backpressure(self, now: float | None = None) -> float:
        """记"从什么时候开始积压"，返回已积压秒数。"""
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._backpressure_since is None:
                self._backpressure_since = moment
                return 0.0
            return max(0.0, moment - self._backpressure_since)

    def clear_backpressure(self) -> None:
        with self._lock:
            self._backpressure_since = None

    def backpressured_for(self, now: float | None = None) -> float | None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._backpressure_since is None:
                return None
            return max(0.0, moment - self._backpressure_since)

    def sending_for(self, now: float | None = None) -> float | None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._sending_since is None:
                return None
            return max(0.0, moment - self._sending_since)

    def wait_idle(self, timeout: float = 1.0) -> bool:
        """等已入队的帧全部写出去（关闭前尽量送掉响应，不让客户端看到假失败）。"""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if self.closed:
                    return False
                sending = self._sending_since
            if sending is None and self._outbox.empty():
                return True
            time.sleep(0.005)
        return False

    def join_sender(self, timeout: float = 1.0) -> bool:
        self._sender.join(max(0.0, timeout))
        return not self._sender.is_alive()

    # -- 发送线程 -----------------------------------------------------------

    def _send_loop(self) -> None:
        while True:
            try:
                frame = self._outbox.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    if self.closed:
                        return
                continue
            if frame is None:  # pragma: no cover - 目前不发哨兵，留给未来 flush
                return
            with self._lock:
                if self.closed:
                    return
                self._sending_since = time.monotonic()
            try:
                self.raw.send(frame)
            except (OSError, ValueError, ipc.IpcError):
                # 对端消失是常态；对慢订阅者，这里也可能是因为 abort 被叫醒。
                self.drop("send_error")
                return
            finally:
                with self._lock:
                    self._sending_since = None
            with self._lock:
                if self.closed:
                    return
                self._sent_frames += 1


class _DriverSideChannel:
    """驱动线程用来续 claim/租约的第二条连接（与 `Worker` 的 `_SideChannel` 同构）。

    单独一份而不是复用 `Worker` 的私有类：它只在"主循环线程正在跑
    `resume_run`"这条路径上需要——模型调用是阻塞的，宿主线程阻塞期间没有别的
    线程续 deadline，默认 30s TTL 会让一个正常的长调用看起来像"已经死了"。
    """

    def __init__(self, log: EventLog, identity: ProcessIdentity) -> None:
        self._log = EventLog(
            log.db_path, blob_dir=log.blob_dir, cross_thread=True
        )
        self._leases = LeaseStore(self._log, identity=identity)
        self._claims = JobStore(self._log, identity=identity)

    def beat(self, claim: Any) -> bool:
        return renew_claim(self._leases, self._claims, claim, ttl_ms=DEFAULT_TTL_MS)

    def close(self) -> None:
        self._log.close()


# --------------------------------------------------------------------------- 服务


class _Daemon:
    """`serve()` 的实现体。所有字段都在 `serve()` 的 finally 里清理。"""

    def __init__(
        self,
        data_root: Path,
        *,
        model: Model,
        limits: Limits | None,
        driver_interval_s: float,
        stop_event: threading.Event,
        on_ready: Callable[[], None] | None,
    ) -> None:
        self.root = Path(data_root)
        self.model = model
        self.limits = limits or Limits()
        self.driver_interval_s = max(0.005, float(driver_interval_s))
        self.stop = stop_event
        self.on_ready = on_ready
        self.stop_reason = ""

        #: 指标采样器（PRD §10.6）。写失败不影响业务（见 metrics 模块的失败容忍）。
        self.metrics: metrics.MetricsRecorder | None = None
        #: "从 serve() 进来到端点可服务"的计时起点。
        #: 用 `perf_counter` 而不是 `monotonic`：Windows 的 `time.monotonic()`
        #: 走 `GetTickCount64`（约 15.6ms 分辨率），热启动常在一个 tick 内完成，
        #: 会被记成 0.0——那不是"伪造的 0"，但没法与伪造区分；`perf_counter`
        #: 在 Windows 走 QPC，能给出真实亚毫秒耗时。
        self._boot_perf = time.perf_counter()
        #: `run.cancel` 之后等待终态的计时表：run_id -> 收到取消的时刻。
        self._cancel_watch: dict[str, float] = {}
        self._conn_counter = 0

        self.lock: DaemonLock | None = None
        self.server: ipc.Server | None = None
        self.plane: ControlPlane | None = None
        self.control_log: EventLog | None = None
        self.driver_log: EventLog | None = None
        #: 指标采样线程专用连接（同一条纪律：建在一处、由采样线程独占）。
        self.metrics_log: EventLog | None = None
        self.control_stack: Stack | None = None
        self.driver_stack: Stack | None = None
        self.side: _DriverSideChannel | None = None

        self.requests: queue.Queue[tuple[_ClientConnection, dict[str, Any]]] = (
            queue.Queue()
        )
        self.resume_requests: queue.Queue[str] = queue.Queue()
        self._conns: list[_ClientConnection] = []
        self._conns_lock = threading.Lock()
        self._accept_thread: threading.Thread | None = None
        #: accept 线程异常退出的原因。非空 ⇒ daemon 不可能是"健康但没接连接"，
        #: 必须在退出路径上把原因交给 Host（不许静默变僵尸）。
        self.accept_error: str | None = None
        self._reader_threads: list[threading.Thread] = []
        self._driver_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 启动

    def run(self) -> None:
        self.lock = acquire_lock(self.root)
        try:
            self._setup()
            if self.on_ready is not None:
                self.on_ready()
            self._main_loop()
        finally:
            self._shutdown()
        if self.accept_error is not None:
            # 带着原因退出：进程退出码非 0，Host 的监护据此重启并打到日志。
            # 以前这里只是"日志里少接几条连接"，UI 上表现为"任务不动"。
            raise ipc.IpcError(f"accept 线程不可用：{self.accept_error}")

    def _setup(self) -> None:
        # 冷启动 = 这个数据根此前没有事件库（第一次跑，或数据根被清过）。
        # 必须在 EventLog 建库**之前**判断——建完看就永远是"热"的。
        cold_start = not (self.root / "events.db").exists()
        assert self.lock is not None
        self.metrics = metrics.MetricsRecorder(
            self.root, instance_id=self.lock.instance_id
        )

        # 两条**各自线程专用**的连接。指向同一个 WAL 库：驱动线程写事件，
        # 控制线程读事件/写审批，各写各的事务。
        self.control_log = EventLog(
            self.root / "events.db", blob_dir=self.root / "artifacts" / "blobs"
        )
        self.control_stack = build_stack(self.control_log, data_root=self.root)
        # 这一条**必须** cross_thread=True：连接在 serve 线程建、由驱动线程
        # 独占使用（`EventLog` 的开关注释说的正是这个场景）。控制面那条不加：
        # 它建在主线程、用也在主线程，关掉保护等于把真实错误藏起来。
        # 不加这一行，第一条作业就会 ProgrammingError，表现是"任务永远 created"。
        self.driver_log = EventLog(
            self.root / "events.db",
            blob_dir=self.root / "artifacts" / "blobs",
            cross_thread=True,
        )
        # 驱动栈会真的执行工具 → 只有它需要 MCP。配置来自进程级
        # `TIANSHU_MCP_CONFIG`（装机 = 用户信任决定）；没配置时 clients=None，
        # 工具面与旧版逐字节相同，一个外部进程都不起。
        from .mcp import config_from_env

        self._mcp_servers = config_from_env()
        self.driver_stack = build_stack(
            self.driver_log,
            data_root=self.root,
            mcp_servers=self._mcp_servers,
        )

        # §4.1/§5：恢复扫描必须在 bind 之前。此时没有客户端、没有驱动线程，
        # 只有控制线程这一条连接，是最干净的窗口。
        recover_started = time.monotonic()
        runs_scanned = self._recover_startup()
        assert self.metrics is not None
        self.metrics.record_duration_ms(
            metrics.RESTART_RECOVER_MS,
            (time.monotonic() - recover_started) * 1000.0,
            cold_start=cold_start,
            runs_scanned=runs_scanned,
        )

        assert self.control_stack is not None
        self.plane = ControlPlane(
            self.control_stack,
            model=self.model,
            limits=self.limits,
            resume_requests=self.resume_requests,
        )
        assert self.lock is not None
        self.plane.instance_id = self.lock.instance_id
        self.plane.started_at = self.lock.started_at

        self.server = ipc.Server(self.root)
        # `start()` 同步完成 bind+listen（issue #63 的根因不在这一步——
        # 见 `_await_endpoint_ready`）；返回时内核已经在替我们排队连接。
        self.server.start()

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="tianshu-daemon-accept", daemon=True
        )
        self._accept_thread.start()
        # on_ready 之前必须证明"listen + accept + 应用层握手"整条路都通。
        # 顺序即契约：就绪标记一旦落盘，Host/测试就不该再被端点问题绊倒。
        self._await_endpoint_ready()
        assert self.metrics is not None and self.plane is not None
        self.metrics.record_duration_ms(
            metrics.STARTUP_READY_MS,
            (time.perf_counter() - self._boot_perf) * 1000.0,
            cold_start=cold_start,
        )
        # 资源采样放在"端点可服务"之后：启动耗时单独记，采样只回答"跑起来之后
        # 的占用趋势"，混在一起会让 startup_ready_ms 被采样线程的启动噪声污染。
        # 采样线程不能借用 control_log 的 SQLite 连接（那条连接建在 serve 线程，
        # 跨线程读会 ProgrammingError，指标只会得到一堆 unknown）。给它一条
        # cross_thread 专用连接，与驱动线程的做法同一条纪律。
        self.metrics_log = EventLog(
            self.root / "events.db",
            blob_dir=self.root / "artifacts" / "blobs",
            cross_thread=True,
        )
        self.metrics.start_sampler(seq_head=self.metrics_log.seq_head)
        self._driver_thread = threading.Thread(
            target=self._driver_loop, name="tianshu-daemon-driver", daemon=True
        )
        self._driver_thread.start()

    # ------------------------------------------------------------------ 就绪

    def _await_endpoint_ready(self) -> None:
        """证明端点真的能用，然后才允许返回给 `on_ready`。

        只证明"端点文件存在"不够，两件事要分开证：

        1. **accept 线程真的进到 `accept()` 了**（`Server.wait_accepting`）。
           线程 `start()` 过不等于被调度过；高负载下这个窗口足以让探活连接
           排队，而 `probe` 在队列满时会直接被拒。
        2. **一次完整认证握手能走完**（`ipc.self_connect`）。`probe()` 只做
           connect，内核可以在没人 accept 时就把连接排进队列；challenge/response
           两侧都活，才证明 accept 循环在应用层也是通的。

        任何一步失败都抛 `IpcError`：宁可启动失败（Host 看到非 0 退出 + 日志），
        也不要留下一个"标记了 ready 但连不上"的守护进程——那正是 issue #63
        的故障形态，排查成本比启动失败高得多。
        """
        assert self.server is not None
        if not self.server.wait_accepting(ACCEPT_READY_TIMEOUT_S):
            raise ipc.IpcError(
                "accept 线程没有在 "
                f"{ACCEPT_READY_TIMEOUT_S:.0f}s 内进入 accept()，拒绝宣告就绪"
            )
        deadline = time.monotonic() + SELF_CONNECT_BUDGET_S
        last_reason = "（尚未尝试）"
        while True:
            if ipc.self_connect(self.root, timeout=SELF_CONNECT_TIMEOUT_S):
                return
            result = ipc.probe_result(self.root, timeout=0.2)
            last_reason = f"{result.reason}（attempts={result.attempts}）"
            if time.monotonic() >= deadline:
                raise ipc.IpcError(f"自连探测失败，端点不可用：{last_reason}")
            # accept 可能刚被调度、或上一次探活连接正被处理；短等再试。
            self.stop.wait(0.01)
            if self.stop.is_set():
                raise ipc.IpcError(f"停止请求先到，端点未就绪：{last_reason}")

    # ------------------------------------------------------------------ 恢复

    def _recover_startup(self) -> int:
        """§5 的三件事里前两件：恢复扫描 + 需要时的重新排队。

        返回扫描到的非终态 Run 数（指标 `restart_recover_ms.runs_scanned` 用；
        "扫描了 0 条"和"扫描慢"是两件不同的事）。

        保守口径写在这里，因为它比任何一行代码都重要：
        - `waiting_approval`：**不动**。自动续跑等于绕过审批。
        - `running` 且没有"有 intent 无 result"：不动，驱动线程会认领死掉的
          作业继续跑（这是正常的"重启后继续"，不是恢复裁决）。
        - `reconciling` / `cancelling` / `running` 且有未配对 intent：交给
          `recover_run`。核对成功就保留原状态（running 由驱动线程接手）；
          核对不了就落 `reconciling` 并 park 作业，留给人。
        - `queued` / `created`：不动，驱动线程认领。
        """
        assert self.control_stack is not None and self.control_log is not None
        tasks = self.control_stack.tasks
        scanned = 0
        rows = tasks.conn.execute(
            "SELECT run_id FROM runs WHERE state IN"
            " ('running','waiting_approval','reconciling','cancelling')"
            " ORDER BY created_at"
        ).fetchall()
        for row in rows:
            run = tasks.get_run(row["run_id"])
            if run is None or run.state in TERMINAL_STATES:
                continue
            scanned += 1
            if run.state == RUN_WAITING_APPROVAL:
                _log(f"恢复到 waiting_approval 的 Run 保持等待审批：{run.run_id}")
                continue
            unpaired = _unpaired_intents(self.control_log, run.run_id)
            # `cancelling` 也要走一遍：它可能是"取消正在确认时进程死了"留下的
            # 半截状态（正常情况下 request_cancel 与 finish 同一个事务提交，
            # 不该被持久观察到）。既有 intent 就核对；核对完也不留在
            # cancelling（那是没人会再推进的状态），而是交给人。
            needs_recovery = (
                run.state in (RUN_RECONCILING, RUN_CANCELLING) or bool(unpaired)
            )
            if not needs_recovery:
                continue
            self._recover_run(run.run_id, unpaired)
        return scanned

    def _recover_run(self, run_id: str, unpaired: list[str]) -> None:
        assert self.control_stack is not None and self.control_log is not None
        tasks = self.control_stack.tasks
        run = tasks.get_run(run_id)
        if run is None:
            return
        task = tasks.get_task(run.task_id)
        if task is None:
            _log(f"恢复跳过：Run {run_id} 的 Task 不存在")
            return
        writable = [binding for binding in task.workspaces if binding.can_write]
        pool = writable or list(task.workspaces)
        try:
            if not pool:
                raise TianshuError("任务没有绑定任何目录，无法核实副作用")
            workspace = Path(pool[0].root_path)
            report = recover_run(
                self.control_log,
                run_id,
                workspace,
                self.control_stack.snapshots,
                leases=self.control_stack.leases,
                memory=self.control_stack.memory,
                # 恢复扫描发生在 bind 之前（见 `_setup`），此刻没有驱动线程、
                # 没有客户端，守卫必然空闲——所以这里可以和派发争同一把锁，
                # 不会自锁（复审 中-1）。
                data_root=self.root,
            )
        except Exception as exc:  # noqa: BLE001 - 恢复失败不等于可以继续
            # 连"能不能核实"都没算出来：这是最不能当成功的一种。落 reconciling、
            # park 作业、留给人；绝不让驱动线程替它继续。
            _log(
                f"恢复失败，Run {run_id} 转入 reconciling 等人核查："
                f"{type(exc).__name__}: {exc}"
            )
            self._hold_for_human(run_id, reason="recovery_failed")
            return

        if report.ok:
            _log(
                f"恢复完成：Run {run_id}（applied={len(report.applied)},"
                f" none_applied={len(report.none_applied)}）"
            )
            # `reconciling` / `cancelling` 的 Run 即使核对清楚了也不自动续跑：
            # 前者是**主动停下**等人核查的，后者是取消没确认完；继续与否由人
            # 按 resume/cancel 决定（CLI 的提示也是这么写的）。
            if run.state in (RUN_RECONCILING, RUN_CANCELLING):
                self._hold_for_human(run_id, reason="recovered_but_held")
            return

        _log(
            f"恢复无法确认，Run {run_id} 保持 reconciling："
            f"unresolved={report.unresolved} problems={report.problems}"
        )
        self._hold_for_human(run_id, reason="recovery_unresolved")

    def _hold_for_human(self, run_id: str, *, reason: str) -> None:
        """把 Run 落到 reconciling，并把"死 owner"留下的作业 park 掉。

        只改 Run 状态是不够的：崩溃留下的作业还是 `claimed`，驱动线程的
        `claim_next` 会把它当"可接管"捡走，接着 `resume_run` 又把 reconciling
        改成 running。人还没看，AI 就继续写了——那就等于把 unknown 洗成成功。
        """
        assert self.control_stack is not None
        tasks = self.control_stack.tasks
        run = tasks.get_run(run_id)
        if run is not None and run.state != RUN_RECONCILING:
            try:
                tasks.transition(run_id, RUN_RECONCILING, reason=reason)
            except TianshuError as exc:
                _log(f"无法把 Run {run_id} 转入 reconciling：{exc}")
        self._park_dead_job(run_id, reason=RUN_RECONCILING)

    def _park_dead_job(self, run_id: str, *, reason: str) -> None:
        """把一条不该被自动认领的作业挂起（等外部事件叫醒）。"""
        assert self.control_stack is not None
        claims = self.control_stack.claims
        if claims is None:
            return
        job = claims.job_for_run(run_id)
        if job is None or job.state in ("waiting", "done", "cancelled"):
            return
        try:
            claim = claims.claim(job.job_id)
        except (JobLost, JobUnavailable) as exc:
            # 还由活着的 owner 持有：启动期几乎不可能（单实例锁保证），
            # 真发生了也绝不能抢——那会把别人正在跑的 Run 打成 reconcile。
            _log(f"作业 {job.job_id} 仍有活着的主人，未 park：{exc}")
            return
        try:
            claims.park(claim, reason=reason)
        except TianshuError as exc:
            _log(f"park 作业 {job.job_id} 失败：{exc}")

    # ------------------------------------------------------------------ 监听

    def _accept_loop(self) -> None:
        """接受连接并交给读线程。**本线程异常退出 = daemon 必须退出**。

        循环里只有两类失败：握手阶段失败（探活连接/错 authkey，正常现象，记一行
        日志继续等）与监听端点不可用（listener 关了 / fd 失效，致命）。后者以前
        也会被 `continue` 吞掉，于是进程活着、端点不接活、UI 上"任务不动"——
        比崩溃更难查。现在致命路径一律 `_fail_accept`：置停止标志，退出时把
        原因交给 Host。
        """
        assert self.server is not None
        try:
            while not self.stop.is_set():
                try:
                    raw = self.server.accept()
                except ipc.ProtocolError as exc:
                    if self.stop.is_set():
                        return
                    # 认证失败 / 握手前断开（`ipc.probe()` 的探活连接就是这样）
                    # 都是"这条连接不算数"，不是"服务端该关"。
                    _log(f"接受连接失败（继续监听）：{exc}")
                    continue
                except (ipc.IpcError, OSError) as exc:
                    if self.stop.is_set():
                        return
                    self._fail_accept(f"监听端点不可用：{type(exc).__name__}: {exc}")
                    return
                if self.stop.is_set():
                    # 退出路径上 close() 会"敲一下"唤醒挂起的 accept（Windows）；
                    # 这条连接只用于唤醒，不能当正式客户端处理。
                    raw.close()
                    return
                self._conn_counter += 1
                conn = _ClientConnection(raw, name=f"conn-{self._conn_counter}")
                with self._conns_lock:
                    self._conns.append(conn)
                reader = threading.Thread(
                    target=self._read_loop,
                    args=(conn,),
                    name="tianshu-daemon-read",
                    daemon=True,
                )
                self._reader_threads.append(reader)
                reader.start()
        except BaseException as exc:  # noqa: BLE001 - 线程异常退出必须留下原因
            self._fail_accept(
                f"accept 线程异常退出：{type(exc).__name__}: {exc}"
            )
        finally:
            # 只有 stop 已置位才是正常退出（shutdown / signal）。
            # 其它任何退出都不许静默——那是"进程活着、端点不接活"。
            if not self.stop.is_set():
                self._fail_accept("accept 线程退出了，但没有收到停止请求")

    def _fail_accept(self, reason: str) -> None:
        """accept 发不了连接了：记下原因并让 daemon 退出。

        `_shutdown` 会照常清理；`run()` 在清理后把 `accept_error` 抛给调用方，
        进程以非 0 退出，Host 的监护因此会重启并留下日志。只打一行日志然后
        `continue` 的话，外部看到的是"守护进程在、但任务永远不动"。
        """
        if self.accept_error is None:
            self.accept_error = reason
            _log(f"{reason}；daemon 停止（不再假装还能接连接）")
        self.stop.set()

    def _read_loop(self, conn: _ClientConnection) -> None:
        while not self.stop.is_set() and not conn.closed:
            try:
                # 带超时的读：没有超时的话主循环关连接会让读线程卡在 recv 内部，
                # shutdown 只能等到超时（`ipc.py` 的注释解释了为什么不能就地 close）。
                message = conn.raw.recv(timeout=0.2)
            except ipc.ProtocolError as exc:
                _log(f"协议错误，断开连接：{exc}")
                break
            except OSError:
                # 主循环关闭连接时 poll() 会抛 OSError；这也是退出信号之一。
                break
            if message is None:
                if self.stop.is_set():
                    break
                if conn.raw.eof:
                    # `None` 有两种含义：这一刻没消息（超时，继续等），或者
                    # 对端已经断开（永远等不到了）。只判 None、不判 eof 的话，
                    # 断线后 poll 会立刻报可读、recv 立刻 EOF，读线程以 100%
                    # CPU 空转——演示里关掉浏览器就能复现。
                    break
                continue
            self.requests.put((conn, message))
        conn.close()

    # ------------------------------------------------------------------ 主循环

    def _main_loop(self) -> None:
        while not self.stop.is_set():
            handled = self._drain_requests()
            self._push_events()
            self._check_slow_subscribers()
            self._check_cancel_metrics()
            self._reap_connections()
            if self.stop.is_set():
                break
            if not handled:
                self.stop.wait(MAIN_POLL_S)

    def _drain_requests(self) -> bool:
        handled = False
        for _ in range(REQUESTS_PER_ROUND):
            try:
                conn, message = self.requests.get_nowait()
            except queue.Empty:
                break
            handled = True
            try:
                self._handle(conn, message)
            except Exception as exc:  # noqa: BLE001 - 单条请求不能带走整个 daemon
                _log(f"处理请求时发生未预期异常：{type(exc).__name__}: {exc}")
        return handled

    def _handle(self, conn: _ClientConnection, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, str):
            request_id = ""
        method = message.get("method")
        if not isinstance(method, str) or not method:
            conn.send(ipc.fail(request_id, "bad_params", "缺少 method"))
            return
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            conn.send(ipc.fail(request_id, "bad_params", "params 必须是对象"))
            return

        if method == "hello":
            self._handle_hello(conn, request_id, params)
            return
        if not conn.hello:
            # §3.3：未完成 hello 的连接只能调 hello。
            conn.send(
                ipc.fail(
                    request_id,
                    "protocol_mismatch",
                    "必须先完成 hello 握手",
                )
            )
            return
        if method == "subscribe":
            self._handle_subscribe(conn, request_id, params)
            return
        if method == "shutdown":
            reason = params.get("reason")
            self.stop_reason = reason if isinstance(reason, str) and reason else "user"
            # 先回响应再停：客户端要能区分"服务端接受了退出"和"连接被掐断"。
            conn.send(ipc.ok(request_id, {"ok": True}))
            _log(f"收到 shutdown（reason={self.stop_reason}）")
            self.stop.set()
            return

        # `run.cancel` 的两个计时点：ack（控制面确认）与 stop（Run 终态）。
        # 取消在安全点确认，所以 ack 与 stop 可能差很远；两者分开记，
        # 否则"取消了但没停"会被平均成一个看不出问题的数字。
        cancel_run_id = ""
        cancel_started: float | None = None
        if method == "run.cancel":
            candidate = params.get("run_id")
            if isinstance(candidate, str) and candidate:
                cancel_run_id = candidate
                cancel_started = time.monotonic()

        assert self.plane is not None
        try:
            result = self.plane.handle(method, params)
        except ControlError as exc:
            conn.send(ipc.fail(request_id, exc.code, exc.message, exc.details))
            return
        except Exception as exc:  # noqa: BLE001 - 我们自己的 bug，别让客户端猜
            _log(f"{method} 内部错误：{type(exc).__name__}: {exc}")
            conn.send(ipc.fail(request_id, "internal", f"{type(exc).__name__}: {exc}"))
            return
        if conn.send(ipc.ok(request_id, result)) and cancel_started is not None:
            self._note_cancel_ack(cancel_run_id, cancel_started)

    def _note_cancel_ack(self, run_id: str, started: float) -> None:
        if self.metrics is None:
            return
        self.metrics.record_duration_ms(
            metrics.CANCEL_ACK_MS, (time.monotonic() - started) * 1000.0, run_id=run_id
        )
        self._cancel_watch[run_id] = started

    def _check_cancel_metrics(self) -> None:
        """把"取消之后多久真的停下"补齐；超时就记 unknown，不写假数字。"""
        if not self._cancel_watch or self.control_stack is None:
            return
        now = time.monotonic()
        for run_id, started in list(self._cancel_watch.items()):
            run = self.control_stack.tasks.get_run(run_id)
            terminal = (
                run is None
                or run.state in TERMINAL_STATES
                or run.state == RUN_FENCED
            )
            if terminal:
                value: float | None = (now - started) * 1000.0
                state = "missing" if run is None else run.state
                self._cancel_watch.pop(run_id, None)
            elif now - started > CANCEL_STOP_TIMEOUT_S:
                value = None
                state = "timeout"
                self._cancel_watch.pop(run_id, None)
            else:
                continue
            if self.metrics is not None:
                self.metrics.record_duration_ms(
                    metrics.CANCEL_STOP_MS, value, run_id=run_id, state=state
                )

    def _handle_hello(
        self, conn: _ClientConnection, request_id: str, params: dict[str, Any]
    ) -> None:
        if conn.hello:
            conn.send(ipc.fail(request_id, "protocol_mismatch", "hello 只能调用一次"))
            return
        try:
            protocol = ipc.negotiate(params)
        except ipc.ProtocolError as exc:
            conn.send(ipc.fail(request_id, "protocol_mismatch", str(exc)))
            # 版本不交叠：明确断开，不做"尽力兼容"（§3.3）。
            conn.close()
            return
        conn.hello = True
        client = params.get("client")
        conn.raw.client = {
            "client": client if isinstance(client, str) else "",
            "pid": params.get("pid"),
            "protocol": protocol,
        }
        assert self.plane is not None and self.lock is not None
        conn.send(
            ipc.ok(
                request_id,
                {
                    "protocol": protocol,
                    "daemon_version": DAEMON_VERSION,
                    "instance_id": self.lock.instance_id,
                    "event_seq_head": self.plane.event_seq_head(),
                    "data_root": str(self.root),
                    "started_at": self.lock.started_at,
                },
            )
        )

    def _handle_subscribe(
        self, conn: _ClientConnection, request_id: str, params: dict[str, Any]
    ) -> None:
        """登记订阅。历史补读**不在这个请求里做**。

        以前这里同步送最多 20×256 条历史，整条主循环被一次订阅卡住；而且一旦
        入队快于发送，就会把队列打满。现在只回响应并记下游标，历史与增量都由
        `_push_events` 从最小游标处分批追——天然有背压（队列高水位时跳过该连接，
        下一轮继续），也天然可被慢订阅者 watchdog 处理。
        """
        since = params.get("since_seq", 0)
        if isinstance(since, bool) or not isinstance(since, int):
            conn.send(ipc.fail(request_id, "bad_params", "since_seq 必须是整数"))
            return
        if since < 0:
            since = 0
        # 先回响应（客户端据此知道 from_seq），推送只会排在它后面（同一发送队列，
        # 顺序由入队顺序保证），不会出现"响应前先来推送"。
        if not conn.send(ipc.ok(request_id, {"subscribed": True, "from_seq": since})):
            return
        conn.subscribed = True
        conn.last_sent_seq = since - 1

    def _push_events(self) -> None:
        """把新事件分批广播给所有订阅者。

        推送路径**只入队、不写 socket**：慢连接在 `conn.send()` 返回 False 或
        达到高水位时被跳过（事件仍留在日志里，下一轮/重连补读），不会拖住
        其他连接与请求处理。
        """
        assert self.plane is not None
        with self._conns_lock:
            subscribers = [
                conn for conn in self._conns if conn.subscribed and not conn.closed
            ]
        if not subscribers:
            return
        # 从最落后的一条游标开始读：新订阅者可能刚登记、老订阅者可能已经追平。
        floor = min(conn.last_sent_seq for conn in subscribers)
        events = self.plane.event_log.read_after(floor, limit=PUSH_BATCH)
        if not events:
            return
        now = time.monotonic()
        for event in events:
            seq = int(event.seq)
            frame = _push_frame(event)
            for conn in subscribers:
                if seq <= conn.last_sent_seq:
                    continue
                if conn.closed:
                    continue
                if conn.pending() >= conn.high_watermark:
                    # 背压：这条连接暂时追不上。事件不丢，游标也不前进；
                    # 持续追不上超过 SUBSCRIBER_BACKLOG_STALL_S 会被 watchdog 断开。
                    conn.note_backpressure(now)
                    continue
                if conn.send(frame):
                    conn.last_sent_seq = seq
                    conn.clear_backpressure()

    def _check_slow_subscribers(self) -> None:
        """慢订阅者 watchdog：发送超期 / 积压超期 ⇒ 断开并记指标。

        只在主循环里判、只做"标记断开 + abort"，真正 kill 阻塞写的是
        `_ClientConnection.drop()`；断开后 `_reap_connections` 记
        `slow_subscriber_dropped`。其他连接完全不受影响。
        """
        with self._conns_lock:
            active = [conn for conn in self._conns if not conn.closed]
        if not active:
            return
        now = time.monotonic()
        for conn in active:
            sending = conn.sending_for(now)
            if sending is not None and sending > SEND_STALL_S:
                conn.drop("send_stall")
                continue
            backlog = conn.backpressured_for(now)
            if backlog is not None and backlog > SUBSCRIBER_BACKLOG_STALL_S:
                conn.drop("backlog_stall")

    def _reap_connections(self) -> None:
        with self._conns_lock:
            dropped: list[_ClientConnection] = []
            live: list[_ClientConnection] = []
            for conn in self._conns:
                (dropped if conn.closed else live).append(conn)
            self._conns = live
        for conn in dropped:
            self._record_drop(conn)
            # 发送线程可能还卡在一次写里（abort 已尽量叫醒它）；join 只是尽量，
            # 不让回收路径无限等待。
            conn.join_sender(0.2)
        # 读线程同样要回收：否则每关一次 UI/每刷新一次页面，就多留一个
        # Thread 对象在列表里，长驻进程里是慢性泄漏。
        self._reader_threads = [t for t in self._reader_threads if t.is_alive()]

    def _record_drop(self, conn: _ClientConnection) -> None:
        """给一次断开补指标（只对"慢订阅者"类断开计数；正常关 UI 不算）。"""
        if conn.drop_recorded:
            return
        reason = conn.drop_reason or "unknown"
        if reason not in _SLOW_DROP_REASONS:
            return
        conn.mark_drop_recorded()
        if self.metrics is not None:
            self.metrics.counter(
                metrics.SLOW_SUBSCRIBER_DROPPED,
                1,
                reason=reason,
                subscribed=conn.subscribed,
                last_sent_seq=conn.last_sent_seq,
                sent_frames=conn.sent_frames,
            )

    # ------------------------------------------------------------------ 驱动

    def _driver_loop(self) -> None:
        assert self.driver_stack is not None and self.driver_log is not None
        worker = Worker(self.driver_stack, model=self.model, limits=self.limits)
        engine = AgentLoop(
            tasks=self.driver_stack.tasks,
            leases=self.driver_stack.leases,
            claims=self.driver_stack.claims,
            dispatcher=self.driver_stack.dispatcher,
            approvals=self.driver_stack.approvals,
            snapshots=self.driver_stack.snapshots,
            model=self.model,
            limits=self.limits,
            # 同 Worker：驱动循环必须复用装配里的投影，否则守护进程会开
            # 第二个记忆投影（详见 worker.py 同处注释）。
            memory=self.driver_stack.memory,
            # 语义摘要记录（PRD §7.1）：同一个存储；开关默认关。
            summaries=self.driver_stack.summaries,
            # 审批/显式续跑与普通 Worker 共用预算：人工等待不重置限额，
            # 未设限时也必须记录续跑的真实模型调用。
            budget=self.driver_stack.budget,
        )
        consecutive_failures = 0
        try:
            while not self.stop.is_set():
                worked = False
                # 先消费"审批已落定 / 显式 resume"的请求：它们不走作业轮询，
                # 因为 waiting 作业按设计不会被 claim_next 认领。
                while not self.stop.is_set():
                    try:
                        run_id = self.resume_requests.get_nowait()
                    except queue.Empty:
                        break
                    self._resume_one(engine, run_id)
                    worked = True
                try:
                    worked = worker.run_once() or worked
                    consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001 - 循环不能因为单个作业退出
                    consecutive_failures += 1
                    _log(f"驱动循环异常：{type(exc).__name__}: {exc}")
                    # 只打日志的话 UI 只会看到"任务不动"，比崩溃更难查。
                    # 连续失败且期间没有任何成功驱动，说明循环已经废了：
                    # 明确停下，让 Host 的监护去重启（§4.3 的指数退避路径）。
                    if consecutive_failures >= DRIVER_MAX_CONSECUTIVE_FAILURES:
                        _log(
                            "驱动线程连续失败"
                            f" {consecutive_failures} 次且没有成功驱动过任何作业，"
                            "daemon 停止"
                        )
                        self.stop.set()
                        break
                if not worked:
                    self.stop.wait(self.driver_interval_s)
        finally:
            worker.close()
            if self.side is not None:
                self.side.close()
                self.side = None

    def _resume_one(self, engine: AgentLoop, run_id: str) -> None:
        """续跑一个 Run 的请求。异常记日志后**丢弃**，不做无限重试。

        流程与 `Worker._drive` 对齐：先拿到 claim（等待审批的作业是
        `waiting`，显式 resume 就是"人可以继续了"那个外部事件），再带上心跳
        调 `resume_run`，最后像 worker 一样结算作业——不结算会留下一条
        `claimed` 的作业永不再被认领。
        """
        assert self.driver_stack is not None
        try:
            tasks = self.driver_stack.tasks
            claims = self.driver_stack.claims
            run = tasks.get_run(run_id)
            if run is None or run.state in TERMINAL_STATES or run.state == RUN_FENCED:
                return
            claim = engine.current_claim(run_id)
            if claim is None:
                job = claims.job_for_run(run_id) if claims is not None else None
                if job is not None and job.state not in ("done", "cancelled"):
                    claim = claims.claim(job.job_id)
            try:
                if claim is None:
                    engine.resume_run(run_id)
                else:
                    channel = self._side_channel()
                    with HeartbeatLoop(
                        lambda: channel.beat(claim),
                        interval_ms=HEARTBEAT_MS,
                    ) as heartbeat:
                        engine.resume_run(run_id)
                    if not heartbeat.stopped_promptly:
                        _log(f"续跑 {run_id} 时心跳线程未如期退出")
                    if heartbeat.error is not None:
                        _log(f"续跑 {run_id} 时心跳失效：{heartbeat.error}")
            finally:
                if claim is not None:
                    self._settle_job(claim)
        except Exception as exc:  # noqa: BLE001 - 一个请求失败不该带走驱动线程
            _log(f"续跑 {run_id} 失败，丢弃该请求：{type(exc).__name__}: {exc}")
        finally:
            assert self.plane is not None
            self.plane.resume_drained(run_id)

    def _side_channel(self) -> _DriverSideChannel:
        if self.side is None:
            assert self.driver_log is not None and self.driver_stack is not None
            identity = self.driver_stack.claims.identity
            self.side = _DriverSideChannel(self.driver_log, identity)
        return self.side

    def _settle_job(self, claim: Any) -> None:
        """把作业状态与 Run 状态对齐（只在还持有 claim 时动）。"""
        assert self.driver_stack is not None
        claims = self.driver_stack.claims
        tasks = self.driver_stack.tasks
        job = claims.get(claim.job_id)
        if job is None or job.state != "claimed":
            return
        run = tasks.get_run(claim.run_id)
        state = run.state if run is not None else RUN_FENCED
        try:
            if state in TERMINAL_STATES:
                outcome = (run.outcome if run is not None else None) or "failed"
                claims.finish(claim, outcome=outcome)
            elif state == RUN_FENCED:
                claims.finish(claim, outcome="failed")
            else:
                claims.park(claim, reason=state)
        except TianshuError as exc:
            _log(f"续跑结算未生效：{exc}")

    # ------------------------------------------------------------------ 关闭

    def _shutdown(self) -> None:
        """契约 §4.3 的顺序：停 accept → 停驱动 → 关连接 → 关 listener →
        释放锁 → 关两条日志连接。每一步都有超时，`serve()` 必须在 5 秒内返回。
        """
        reason = self.stop_reason or "signal"
        if not self.stop.is_set():
            _log(f"daemon 退出（reason={reason}）")
            self.stop.set()
        else:
            _log(f"daemon 退出（reason={reason}）")

        # 1. 停 accept：标志已置；accept 线程阻塞在 listener.accept() 上，
        #    靠下一步关 listener 唤醒。先给它一小段时间自然退出。
        accept = self._accept_thread
        if accept is not None:
            accept.join(0.2)

        # 2. 停驱动：给它 5s。模型调用可能还没返回，join 超时也不强杀线程
        #    （daemon 线程随进程退出；硬杀会留下半条事务）。
        driver = self._driver_thread
        driver_stopped = True
        if driver is not None:
            driver.join(JOIN_TIMEOUT_S)
            driver_stopped = not driver.is_alive()
            if not driver_stopped:
                _log("驱动线程未在 5s 内退出；保留其连接，交给进程退出回收")

        # 3. 关连接：先把每条连接已入队的帧排空（通知 shutdown 的响应也在里面），
        #    再断开。发送是异步的，不排空会让客户端看到"请求被接受、响应没回来"。
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        flush_deadline = time.monotonic() + CONNECTION_FLUSH_TIMEOUT_S
        for conn in conns:
            remaining = max(0.0, flush_deadline - time.monotonic())
            if remaining > 0 and not conn.closed:
                conn.wait_idle(min(remaining, 0.3))
            conn.close()
            conn.join_sender(0.2)
        # 4. 关 listener。`ipc.Server.close()` 内部会再关一次已接受的连接（幂等），
        #    并且它才是唤醒阻塞在 accept() 的那一步。
        if self.server is not None:
            self.server.close()
            self.server = None
        if accept is not None:
            accept.join(1.0)
        for reader in self._reader_threads:
            reader.join(0.5)
        self._reader_threads.clear()

        # 5. 释放锁：必须在关连接之后、关库之前。留着锁会挡住下一次启动。
        if self.lock is not None:
            _unlink_lock(self.lock)
            self.lock.released = True
            self.lock = None

        # 6. 关两条日志连接（各自线程的）。驱动线程没退时保留它的连接：
        #    从别的线程关掉正在被使用的连接只会得到一个没人看得到的
        #    ProgrammingError，还可能截断一次写入。
        # 指标采样线程在读事件库的 seq head，必须在线程退出后再关连接。
        if self.metrics is not None:
            self.metrics.stop_sampler()
        if self.metrics_log is not None:
            self.metrics_log.close()
            self.metrics_log = None
        if self.plane is not None:
            self.plane.close()
            self.plane = None
        elif self.control_log is not None:
            # 装配中途失败（还没建出 ControlPlane）：控制连接要自己关。
            self.control_log.close()
        if driver_stopped and self.driver_log is not None:
            self.driver_log.close()
        self.driver_log = None
        self.control_log = None
        if self.metrics is not None:
            self.metrics.close()
            self.metrics = None


# --------------------------------------------------------------------------- 辅助


def _unpaired_intents(log: EventLog, run_id: str) -> list[str]:
    """有 intent、没有 result 的调用 id（按事件顺序）。"""
    events = log.read_run(run_id)
    intents = {
        event.payload.get("call_id")
        for event in events
        if event.type == "tool.intent" and event.payload.get("call_id")
    }
    results = {
        event.payload.get("call_id")
        for event in events
        if event.type == "tool.result"
    }
    return sorted(str(call_id) for call_id in intents - results)


def _push_frame(event: Any) -> dict[str, Any]:
    view = event_view(event)
    return ipc.event_push(
        view["seq"],
        view["type"],
        view["run_id"],
        view["task_id"],
        view["ts"],
        view["payload"],
        view["actor_kind"],
    )


def _install_signal_handlers(
    stop: threading.Event,
) -> dict[int, Any]:
    """把 SIGTERM/SIGINT 统一到停止标志。

    `signal.signal` 只能在主线程调用。`serve()` 也可能被测试/嵌入方放到子线程
    里跑——那种情况下由调用方用 `stop_event` 控制退出，这里明确跳过，而不是
    抛 `ValueError` 把守护进程搞死在启动路径上。
    """
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        # 信号处理器里只置标志：日志/数据库都可能在别的线程里持着锁。
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):
            continue
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError, TypeError):
            continue


def serve(
    data_root: Path,
    *,
    model: Model,
    limits: Limits | None = None,
    driver_interval_s: float = 0.05,
    on_ready: Callable[[], None] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """阻塞地跑守护进程，直到 shutdown RPC / SIGTERM / SIGINT / `stop_event`。

    `on_ready` 在**端点已经 bind+listen、accept 线程进入循环、并且自己完整
    握手连上自己一次之后**调用一次：Host 用它来把"daemon 就绪"这件事变成
    可等待的，而不是盲目重试连接。就绪标记必须晚于"真的能连"——提前落盘会让
    调用方撞上还没开始 accept 的连接队列（issue #63）。
    """
    root = Path(data_root)
    stop = stop_event if stop_event is not None else threading.Event()
    if stop.is_set():
        return
    previous = _install_signal_handlers(stop)
    daemon = _Daemon(
        root,
        model=model,
        limits=limits,
        driver_interval_s=driver_interval_s,
        stop_event=stop,
        on_ready=on_ready,
    )
    try:
        daemon.run()
    finally:
        _restore_signal_handlers(previous)


def request_shutdown(
    data_root: Path, *, reason: str = "user", timeout: float = 5.0
) -> bool:
    """请守护进程退出。返回是否把请求送到了（`False` = 连不上/没答）。"""
    budget = max(0.1, float(timeout))
    deadline = time.monotonic() + budget
    client = ipc.Client(Path(data_root), timeout=budget)
    try:
        remaining = max(0.05, deadline - time.monotonic())
        client.connect(timeout=remaining)
        remaining = max(0.05, deadline - time.monotonic())
        client.call("shutdown", {"reason": reason}, timeout=remaining)
        return True
    except (ipc.IpcError, OSError) as exc:
        _log(f"请求退出失败：{exc}")
        return False
    finally:
        client.close()


__all__ = [
    "LOCK_GRACE_MS",
    "LOCK_NAME",
    "DaemonLock",
    "acquire_lock",
    "lock_path",
    "request_shutdown",
    "serve",
]
