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

## 事件推送

`subscribe {since_seq}` 先补历史（`seq >= since_seq`）再推增量。补洞游标是全库
`seq`，由主循环每轮从**自己的** `EventLog` 连接读 (`read_after`) 并广播。
推送和响应共用一条连接，`ServerConnection` 不保证线程安全，所以每条连接有自己的
发送锁（`ipc.py` 的注释已经写明这一点）。

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

from . import ipc
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

#: 一次读多少条事件用于广播/补历史。分批是为了让 shutdown 标志有机会被看见。
PUSH_BATCH = 256
HISTORY_BATCH = 256

#: `subscribe` 一次调用最多补多少批历史；剩下的交给增量推送（`last_sent_seq`
#: 已经被推进，下一轮会接着补）。这样"历史很长"也不会卡住主循环。
HISTORY_BATCHES_PER_REQUEST = 20

#: 关闭时各线程 join 的上限。契约 §4.3：5 秒内退出。
JOIN_TIMEOUT_S = 5.0

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
    """服务端视角的一条客户端连接。

    `ipc.ServerConnection.send` 不是线程安全的：主循环回响应、广播推送、补历史
    三条路径都会写同一帧。每条连接一把发送锁是 `ipc.py` 注释里点名要求的。
    """

    __slots__ = ("raw", "send_lock", "subscribed", "last_sent_seq", "hello", "closed")

    def __init__(self, raw: ipc.ServerConnection) -> None:
        self.raw = raw
        self.send_lock = threading.Lock()
        self.subscribed = False
        self.last_sent_seq = 0
        self.hello = False
        self.closed = False

    def send(self, message: dict[str, Any]) -> bool:
        with self.send_lock:
            if self.closed:
                return False
            try:
                self.raw.send(message)
            except (OSError, ValueError, ipc.IpcError):
                # 客户端消失是常态（关 UI、刷新页面），不是服务端故障。
                self.closed = True
                return False
            return True

    def close(self) -> None:
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
            self.raw.close()


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

        self.lock: DaemonLock | None = None
        self.server: ipc.Server | None = None
        self.plane: ControlPlane | None = None
        self.control_log: EventLog | None = None
        self.driver_log: EventLog | None = None
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

    def _setup(self) -> None:
        # 两条**各自线程专用**的连接。指向同一个 WAL 库：驱动线程写事件，
        # 控制线程读事件/写审批，各写各的事务。
        self.control_log = EventLog(
            self.root / "events.db", blob_dir=self.root / "artifacts" / "blobs"
        )
        self.control_stack = build_stack(self.control_log, data_root=self.root)
        # 这一条**必须** cross_thread=True：连接在 serve 线程建、由驱动线程
        # 独占使用（`EventLog` 的开关注释说的正是这个场景）。控制面那条不加：
        # 它建在主线程、用也在主线程，关掉保护等于把真实错误藏起来。
        self.driver_log = EventLog(
            self.root / "events.db",
            blob_dir=self.root / "artifacts" / "blobs",
            cross_thread=True,
        )
        self.driver_stack = build_stack(self.driver_log, data_root=self.root)

        # §4.1/§5：恢复扫描必须在 bind 之前。此时没有客户端、没有驱动线程，
        # 只有控制线程这一条连接，是最干净的窗口。
        self._recover_startup()

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
        self.server.start()

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="tianshu-daemon-accept", daemon=True
        )
        self._accept_thread.start()
        self._driver_thread = threading.Thread(
            target=self._driver_loop, name="tianshu-daemon-driver", daemon=True
        )
        self._driver_thread.start()

    # ------------------------------------------------------------------ 恢复

    def _recover_startup(self) -> None:
        """§5 的三件事里前两件：恢复扫描 + 需要时的重新排队。

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
        rows = tasks.conn.execute(
            "SELECT run_id FROM runs WHERE state IN"
            " ('running','waiting_approval','reconciling','cancelling')"
            " ORDER BY created_at"
        ).fetchall()
        for row in rows:
            run = tasks.get_run(row["run_id"])
            if run is None or run.state in TERMINAL_STATES:
                continue
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
        assert self.server is not None
        while not self.stop.is_set():
            try:
                raw = self.server.accept()
            except (ipc.IpcError, OSError) as exc:
                if self.stop.is_set():
                    return
                # 认证失败 / 握手前断开（`ipc.probe()` 的探活连接就是这样）都是
                # "这条连接不算数"，不是"服务端该关"。
                _log(f"接受连接失败（继续监听）：{exc}")
                continue
            conn = _ClientConnection(raw)
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
                continue
            self.requests.put((conn, message))
        conn.close()

    # ------------------------------------------------------------------ 主循环

    def _main_loop(self) -> None:
        while not self.stop.is_set():
            handled = self._drain_requests()
            self._push_events()
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
        conn.send(ipc.ok(request_id, result))

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
        since = params.get("since_seq", 0)
        if isinstance(since, bool) or not isinstance(since, int):
            conn.send(ipc.fail(request_id, "bad_params", "since_seq 必须是整数"))
            return
        if since < 0:
            since = 0
        # 先回响应（客户端据此知道 from_seq），再补历史：顺序反了客户端会把
        # 响应前面的推送当成"不属于这次订阅"。
        if not conn.send(ipc.ok(request_id, {"subscribed": True, "from_seq": since})):
            return
        conn.subscribed = True
        conn.last_sent_seq = since - 1
        assert self.plane is not None
        cursor = since - 1
        for _ in range(HISTORY_BATCHES_PER_REQUEST):
            events = self.plane.event_log.read_after(cursor, limit=HISTORY_BATCH)
            if not events:
                return
            for event in events:
                if not conn.send(_push_frame(event)):
                    return
                cursor = int(event.seq)
                conn.last_sent_seq = cursor
            if len(events) < HISTORY_BATCH:
                return
            if self.stop.is_set():
                return

    def _push_events(self) -> None:
        assert self.plane is not None
        with self._conns_lock:
            subscribers = [
                conn for conn in self._conns if conn.subscribed and not conn.closed
            ]
        if not subscribers:
            return
        # 从最落后的一条游标开始读：新订阅者可能刚补完历史，老订阅者可能已经
        # 追到 head。一次读一段，落后的那些在后续轮次继续追。
        floor = min(conn.last_sent_seq for conn in subscribers)
        events = self.plane.event_log.read_after(floor, limit=PUSH_BATCH)
        for event in events:
            frame = _push_frame(event)
            for conn in subscribers:
                if int(event.seq) <= conn.last_sent_seq:
                    continue
                if conn.send(frame):
                    conn.last_sent_seq = int(event.seq)

    def _reap_connections(self) -> None:
        with self._conns_lock:
            self._conns = [conn for conn in self._conns if not conn.closed]

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

        # 3+4. 关连接、关 listener。`ipc.Server.close()` 内部先关已接受的连接
        #      再关 listener，正是为了唤醒阻塞在 recv 上的读线程。
        if self.server is not None:
            self.server.close()
            self.server = None
        if accept is not None:
            accept.join(1.0)
        for reader in self._reader_threads:
            reader.join(0.5)
        self._reader_threads.clear()
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            conn.close()

        # 5. 释放锁：必须在关连接之后、关库之前。留着锁会挡住下一次启动。
        if self.lock is not None:
            _unlink_lock(self.lock)
            self.lock.released = True
            self.lock = None

        # 6. 关两条日志连接（各自线程的）。驱动线程没退时保留它的连接：
        #    从别的线程关掉正在被使用的连接只会得到一个没人看得到的
        #    ProgrammingError，还可能截断一次写入。
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

    `on_ready` 在**端点已经能连之后、主循环开始之前**调用一次：Host 用它来把
    "daemon 就绪"这件事变成可等待的，而不是盲目重试连接。
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