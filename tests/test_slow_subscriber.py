"""慢订阅者隔离（PRD §10.6）：控制面不许被一个不读 socket 的客户端拖死。

修复前：`daemon._main_loop` 在单线程里同步 `_push_events()` → `conn.send()`，
一个订阅后停止读取的客户端会把派发 / 审批 / 取消 / 状态查询一起堵住。
修复后：每连接**有界发送队列 + 独立发送线程**；队列满 / 发送超期 ⇒ 断开该
订阅者并记 `slow_subscriber_dropped`，其他连接与请求处理不受影响；订阅者重连
后按 `since_seq` 游标补读，不丢审计事实。

测试用**真实 socket + 真 daemon**：慢订阅者是一个连上后真的不读的 `ipc.Client`；
"不读"的后果（socket 缓冲区写满、发送线程阻塞）是内核给的，不是 mock 出来的。
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tianshu import daemon, ipc
from tianshu import metrics as metrics_mod
from tianshu.model import ScriptedModel

#: 控制面单次查询的声明时限。修复前慢订阅者会把主循环卡在 send() 上，
#: 这个上限必然被击穿；修复后是毫秒级。
CONTROL_LATENCY_S = 0.75

#: 单条任务事件的负载大小。要大到一次写入就把 AF_UNIX 发送缓冲填满，
#: 让"不读"真的变成"发送线程阻塞"——小事件会被内核缓冲吃掉，测不出隔离。
BIG_GOAL_CHARS = 180_000


# --------------------------------------------------------------------------- 夹具


def _short_data_root(tmp_path: Path) -> tuple[Path, bool]:
    candidate = tmp_path / "data"
    try:
        ipc.endpoint_path(candidate)
        return candidate, True
    except ipc.ProtocolError:
        return Path(tempfile.mkdtemp(prefix="tss-", dir=tempfile.gettempdir())), False


@pytest.fixture()
def root(tmp_path):
    path, inside_tmp = _short_data_root(tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if not inside_tmp:
        shutil.rmtree(path, ignore_errors=True)


class Harness:
    """在子线程里跑真 daemon（模型不参与这些用例）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run, name="test-slow-sub-daemon", daemon=True
        )

    def _run(self) -> None:
        try:
            daemon.serve(
                self.root,
                model=ScriptedModel([]),
                driver_interval_s=0.02,
                on_ready=self.ready.set,
                stop_event=self.stop,
            )
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)
        finally:
            self.ready.set()

    def start(self, timeout: float = 15.0) -> Harness:
        self.thread.start()
        assert self.ready.wait(timeout), "daemon 没有就绪"
        if self.errors:
            raise self.errors[0]
        return self

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        self.stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


@pytest.fixture()
def slow_subscriber_env(monkeypatch: pytest.MonkeyPatch):
    """把"慢"的判定参数调到测试尺度（生产默认 5s，这里 0.4s）。

    被判定为慢的依据不变：发送超期 / 积压超期；变的只是阈值。
    """
    monkeypatch.setattr(daemon, "SUBSCRIBER_BACKLOG_STALL_S", 0.4)
    monkeypatch.setattr(daemon, "SEND_STALL_S", 5.0)
    monkeypatch.setenv(metrics_mod.ENV_METRICS_INTERVAL, "0.05")
    yield


# --------------------------------------------------------------------------- 辅助


def _connect(root: Path, *, timeout: float = 5.0) -> ipc.Client:
    client = ipc.Client(root, timeout=timeout)
    client.connect()
    return client


def _wait_until(predicate, *, timeout: float = 10.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def _timed(client: ipc.Client, method: str, params: dict | None = None) -> float:
    started = time.monotonic()
    client.call(method, params or {}, timeout=5.0)
    return time.monotonic() - started


def _create_big_task(client: ipc.Client, index: int) -> str:
    goal = "x" * BIG_GOAL_CHARS + f"#{index}"
    return client.call("task.create", {"goal": goal}, timeout=10.0)["task"]["task_id"]


def _drops(root: Path) -> list[dict]:
    return [
        record
        for record in metrics_mod.read_metrics(root)
        if record.get("metric") == metrics_mod.SLOW_SUBSCRIBER_DROPPED
    ]


def _wait_drop(root: Path, *, timeout: float = 8.0) -> dict:
    return _wait_until(lambda: _drops(root), timeout=timeout)[0]


def _collect_until(client: ipc.Client, head: int, *, timeout: float = 15.0) -> list[int]:
    seqs: list[int] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (not seqs or seqs[-1] < head):
        try:
            message = client.next_event(timeout=0.2)
        except ipc.ConnectionClosed:
            break
        if message is None:
            continue
        seqs.append(int(message["event"]["seq"]))
    return seqs


# --------------------------------------------------------------------------- 单测：队列满


class _BlockingRaw:
    """替身：send 永远阻塞。用来确定性地制造"队列满"。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.aborted = threading.Event()
        self.release = threading.Event()

    def send(self, message) -> None:  # noqa: ANN001
        self.started.set()
        self.release.wait(30.0)

    def abort(self) -> None:
        self.aborted.set()
        self.release.set()

    def close(self) -> None:
        pass


def test_connection_queue_is_bounded_and_full_means_drop() -> None:
    """队列真的**有界**：发送线程卡住时，第 N+1 帧必须触发断开，不是无限吃内存。"""
    raw = _BlockingRaw()
    conn = daemon._ClientConnection(raw, name="unit", queue_max=1)  # noqa: SLF001
    try:
        assert conn.send({"seq": 1}) is True
        assert raw.started.wait(5.0), "发送线程没有开始写"
        assert conn.sending_for() is not None
        assert conn.send({"seq": 2}) is True  # 占住唯一队列位
        assert conn.send({"seq": 3}) is False  # 队列满 → 落在调用方看到的 False
        assert conn.drop_reason == "queue_full"
        assert raw.aborted.wait(2.0), "判定断开后必须叫醒阻塞中的发送"
    finally:
        conn.drop("cleanup")
        conn.join_sender(2.0)


# --------------------------------------------------------------------------- 积压超期


def test_slow_subscriber_does_not_block_control_plane(
    root: Path, slow_subscriber_env
) -> None:
    """核心回归：一个订阅后不读的客户端，不能拖慢状态/审批查询。"""
    harness = Harness(root).start()
    slow = _connect(root)
    control = _connect(root)
    try:
        assert slow.subscribe(0)["from_seq"] == 0
        # 只订阅、不读。之后它收到的所有推送都堵在 socket/发送队列里。
        for index in range(8):
            _create_big_task(control, index)
            latency = _timed(control, "state")
            assert latency < CONTROL_LATENCY_S, (
                f"慢订阅者把控制面拖住了：state 用了 {latency:.2f}s"
            )
            assert _timed(control, "approvals.list") < CONTROL_LATENCY_S

        drop = _wait_drop(root)
        assert drop["reason"] in {"backlog_stall", "queue_full", "send_stall"}
        assert drop["value"] >= 1
        assert drop["subscribed"] is True
        assert drop["last_sent_seq"] >= 0

        # 慢订阅者这一侧最终看到断线（它没读到的帧仍在日志里）。
        with pytest.raises(ipc.ConnectionClosed):
            _drain_until_closed(slow)

        # 其他连接完全不受影响。
        assert _timed(control, "state") < CONTROL_LATENCY_S

        # 重连按游标补齐：从 0 开始必须拿到完整、连续的事件序列，不丢审计事实。
        resumed = _connect(root)
        try:
            head = resumed.welcome["event_seq_head"]
            resumed.subscribe(0)
            seqs = _collect_until(resumed, head)
            assert seqs == list(range(1, head + 1)), (
                f"重连补读有缺口：收到 {len(seqs)} 条，head={head}"
            )
        finally:
            resumed.close()
    finally:
        slow.close()
        control.close()
        harness.stop_and_wait()


def _drain_until_closed(client: ipc.Client, *, timeout: float = 3.0) -> None:
    """读到断线为止（慢订阅者被 abort 后，客户端应看到 ConnectionClosed）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client.next_event(timeout=0.2)  # 断线时抛 ConnectionClosed，交给调用方
    raise AssertionError("慢订阅者没有被断开")


# --------------------------------------------------------------------------- 发送超期


def test_send_stall_drop_and_reconnect_from_cursor(root: Path, monkeypatch) -> None:
    """发送线程被**单帧写**阻塞超期 ⇒ 断开；重连从客户端游标补读，不重不漏。"""
    monkeypatch.setattr(daemon, "SEND_QUEUE_MAX", 64)
    monkeypatch.setattr(daemon, "SUBSCRIBER_BACKLOG_WATERMARK", 0.5)
    monkeypatch.setattr(daemon, "SEND_STALL_S", 0.4)
    monkeypatch.setattr(daemon, "SUBSCRIBER_BACKLOG_STALL_S", 5.0)
    harness = Harness(root).start()
    slow = _connect(root)
    control = _connect(root)
    try:
        slow.subscribe(0)
        # 先读一小段，记下客户端真实游标。
        head_before = slow.welcome["event_seq_head"]
        seqs_before = _collect_until(slow, head_before)
        cursor = seqs_before[-1] if seqs_before else 0
        assert cursor >= 0

        # 之后不再读：大事件把 socket 写满，发送线程阻塞超过 0.4s。
        for index in range(4):
            _create_big_task(control, index)
        drop = _wait_drop(root)
        assert drop["reason"] == "send_stall", drop
        assert drop["subscribed"] is True

        assert _timed(control, "state") < CONTROL_LATENCY_S

        resumed = _connect(root)
        try:
            head = resumed.welcome["event_seq_head"]
            assert head > cursor, "游标之后必须有新事件，否则这条用例证明不了补读"
            resumed.subscribe(cursor + 1)
            seqs = _collect_until(resumed, head)
            assert seqs == list(range(cursor + 1, head + 1)), (
                f"从游标 {cursor + 1} 补读不连续：{seqs[:5]}…{seqs[-5:]}"
            )
        finally:
            resumed.close()
    finally:
        slow.close()
        control.close()
        harness.stop_and_wait()


# --------------------------------------------------------------------------- 另一个订阅者


def test_other_subscriber_keeps_receiving_while_one_is_dropped(
    root: Path, slow_subscriber_env
) -> None:
    """一个慢订阅者被断开时，另一个正常订阅者的事件流必须连续、不丢。"""
    harness = Harness(root).start()
    slow = _connect(root)
    fast = _connect(root)
    control = _connect(root)
    collected: list[int] = []
    errors: list[BaseException] = []
    stop_reader = threading.Event()

    def reader() -> None:
        try:
            while not stop_reader.is_set():
                message = fast.next_event(timeout=0.05)
                if message is not None:
                    collected.append(int(message["event"]["seq"]))
        except BaseException as exc:  # noqa: BLE001 - 交回主线程断言
            errors.append(exc)

    try:
        slow.subscribe(0)
        fast.subscribe(0)
        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        try:
            for index in range(8):
                _create_big_task(control, index)
            _wait_drop(root)
            head = control.call("state", {})["event_seq_head"]
            _wait_until(lambda: collected and collected[-1] >= head, timeout=10.0)
        finally:
            stop_reader.set()
            reader_thread.join(3.0)

        assert not errors, errors
        assert collected == list(range(1, head + 1)), (
            f"健康订阅者的事件流有缺口：{len(collected)} 条 / head={head}"
        )
    finally:
        slow.close()
        fast.close()
        control.close()
        harness.stop_and_wait()