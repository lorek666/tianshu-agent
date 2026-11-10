"""M5 IPC 传输层的行为测试。

这些测试**不启动真正的 daemon**：`ipc.py` 只负责"把一条 JSON 消息可靠地送过去
并拿回一条响应"，所以测试里用一个十几行的回声服务器就够了。真正的业务语义
（谁该被拒绝、审批怎么落定）在 `tests/test_daemon.py` / `tests/test_control.py`。

重点覆盖三件在真机上最容易出问题、而不是"看起来能跑"的事：

1. **推送与响应共用一条连接时不串号**——`call()` 必须跨过路上的推送继续找自己的
   响应，否则 UI 的 SSE 与 RPC 一并发时就会偶发解析错乱。
2. **客户端断开不能让服务端倒下**——UI 关窗口/刷新是常态，不是异常。
3. **存活判定必须真的连一次**——`probe()` 若只看 socket 文件存在，会被
   `kill -9` 留下的僵尸 socket 骗过去。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from multiprocessing.connection import Client as RawClient
from pathlib import Path

import pytest

from tianshu import ipc

# --------------------------------------------------------------------------- 夹具


def _short_root() -> Path:
    """unix socket 路径上限是 104 字节，pytest 的 tmp_path 在 macOS 上可能超。

    生产路径（`~/Library/Application Support/AgentOS/run/agent.sock`）不会超，
    但测试必须用短目录，否则会得到一句完全看不出原因的 "AF_UNIX path too long"。
    """
    base = None if os.name == "nt" else "/tmp"
    return Path(tempfile.mkdtemp(prefix="tsipc-", dir=base))


@pytest.fixture()
def root():
    path = _short_root()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class EchoServer:
    """最小回声服务器：实现 hello / echo / subscribe / 未知方法。"""

    def __init__(self, root: Path):
        self.server = ipc.Server(root)
        self.server.start()
        self._stop = threading.Event()
        self.connections: list[ipc.ServerConnection] = []
        self._handlers: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self.server.accept()
            except (OSError, ipc.IpcError):
                # 认证失败/握手前断开都是"这条连接不算数"，不是"服务端该关"。
                if self._stop.is_set():
                    return
                continue
            self.connections.append(conn)
            handler = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._handlers.append(handler)
            handler.start()

    def _serve(self, conn: ipc.ServerConnection) -> None:
        while not self._stop.is_set():
            try:
                # 带超时的读：没有超时的话关闭连接会让 close() 卡在连接内部的
                # 锁上（另一条线程正阻塞在 recv）。daemon 的读线程也是这么写的。
                message = conn.recv(timeout=0.2)
            except ipc.ProtocolError:
                break
            if message is None:
                if self._stop.is_set() or conn.eof:
                    # 101 次里 99 次是超时（继续等）；断线只发生一次，但只判
                    # None、不判 eof 的写法会让这一轮循环以 100% CPU 空转。
                    break
                continue
            request_id = str(message.get("id") or "")
            method = str(message.get("method") or "")
            params = message.get("params") or {}
            try:
                if method == "hello":
                    protocol = ipc.negotiate(params)
                    conn.send(ipc.ok(request_id, {"protocol": protocol, "daemon_version": "test"}))
                elif method == "echo":
                    conn.send(ipc.ok(request_id, {"echo": params}))
                elif method == "subscribe":
                    conn.send(ipc.ok(request_id, {"subscribed": True, "from_seq": 1}))
                    conn.send(ipc.event_push(1, "run.started", "r1", "t1", 1, {"x": 1}))
                elif method == "push_first":
                    # 先推一条，再回响应：模拟真实 daemon 在派发事件的同时回 RPC。
                    conn.send(ipc.event_push(7, "model.intent", "r1", "t1", 7, {}))
                    conn.send(ipc.ok(request_id, {"after_push": True}))
                else:
                    conn.send(ipc.fail(request_id, "not_found", f"没有方法 {method}"))
            except (OSError, ipc.IpcError):
                break

    def stop(self) -> None:
        self._stop.set()
        for handler in self._handlers:
            handler.join(timeout=1)
        for conn in self.connections:
            conn.close()
        self.server.close()


@pytest.fixture()
def echo(root):
    server = EchoServer(root)
    try:
        yield server
    finally:
        server.stop()


# --------------------------------------------------------------------------- 路径与认证


def test_endpoint_path_is_platform_native(root: Path) -> None:
    endpoint = ipc.endpoint_path(root)
    if os.name == "nt":
        assert endpoint.startswith(r"\\.\pipe\AgentOS-")
    else:
        assert endpoint.endswith("/run/agent.sock")


def test_endpoint_name_is_stable_per_data_root(root: Path) -> None:
    assert ipc.endpoint_path(root) == ipc.endpoint_path(root)


def test_authkey_is_private_and_stable(root: Path) -> None:
    first = ipc.load_authkey(root)
    assert len(first) == 32
    assert ipc.load_authkey(root) == first, "每次连都换 key 等于每次都把已连上的踢掉"
    if os.name != "nt":
        assert oct(ipc.authkey_path(root).stat().st_mode & 0o777) == "0o600"
        assert oct(ipc.run_dir(root).stat().st_mode & 0o777) == "0o700"


@pytest.mark.skipif(os.name == "nt", reason="Windows 端点不是文件路径，没有长度上限")
def test_too_long_endpoint_is_refused_with_reason() -> None:
    deep = Path("/tmp") / ("x" * 40) / ("y" * 40) / ("z" * 40)
    with pytest.raises(ipc.ProtocolError) as excinfo:
        ipc.endpoint_path(deep)
    assert "路径太长" in str(excinfo.value)
    assert "--data-root" in str(excinfo.value)


# --------------------------------------------------------------------------- 编解码与协商


def test_negotiate_picks_highest_overlap() -> None:
    assert ipc.negotiate({"protocol_min": 1, "protocol_max": 1}) == ipc.PROTOCOL_MAX
    assert ipc.negotiate({"protocol_min": 0, "protocol_max": 99}) == ipc.PROTOCOL_MAX


def test_negotiate_refuses_disjoint_range() -> None:
    with pytest.raises(ipc.ProtocolError) as excinfo:
        ipc.negotiate({"protocol_min": 99, "protocol_max": 100})
    assert "不交叠" in str(excinfo.value)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"protocol_min": "1"},
        {"protocol_min": 2, "protocol_max": 1},
    ],
)
def test_negotiate_rejects_malformed(params: dict) -> None:
    with pytest.raises(ipc.ProtocolError):
        ipc.negotiate(params)


def test_encode_refuses_oversized_message() -> None:
    with pytest.raises(ipc.ProtocolError):
        ipc.encode({"id": "x", "method": "echo", "params": {"blob": "a" * ipc.MAX_MESSAGE_BYTES}})


def test_decode_refuses_non_object() -> None:
    with pytest.raises(ipc.ProtocolError):
        ipc.decode(b"[1,2,3]")


# --------------------------------------------------------------------------- 真实往返


def test_call_roundtrip(echo, root: Path) -> None:
    with ipc.Client(root) as client:
        assert client.welcome["protocol"] == ipc.PROTOCOL_MAX
        assert client.call("echo", {"n": 1}) == {"echo": {"n": 1}}


def test_large_payload_roundtrip(echo, root: Path) -> None:
    blob = "国" * 60_000  # 180KB UTF-8：超过任何 socket 的原子写/单次读缓冲
    with ipc.Client(root) as client:
        assert client.call("echo", {"blob": blob})["echo"]["blob"] == blob


def test_remote_error_exposes_code(echo, root: Path) -> None:
    with ipc.Client(root) as client:
        with pytest.raises(ipc.RemoteError) as excinfo:
            client.call("nope")
    assert excinfo.value.code == "not_found"
    assert "没有方法 nope" in excinfo.value.message


def test_push_does_not_corrupt_response(echo, root: Path) -> None:
    """推送先到、响应后到时，`call()` 必须仍然拿到自己的响应。"""
    with ipc.Client(root) as client:
        assert client.call("push_first") == {"after_push": True}
        event = client.next_event(timeout=1)
    assert event is not None and event["push"] == "event"
    assert event["event"]["seq"] == 7


def test_subscribe_then_event(echo, root: Path) -> None:
    with ipc.Client(root) as client:
        assert client.subscribe(1)["from_seq"] == 1
        event = client.next_event(timeout=1)
    assert event is not None
    assert event["event"]["type"] == "run.started"


def test_client_disconnect_does_not_kill_server(echo, root: Path) -> None:
    client = ipc.Client(root)
    client.connect()
    client.close()
    # 服务端在下一个连接上必须还能正常服务。
    with ipc.Client(root) as second:
        assert second.call("echo", {"ok": True}) == {"echo": {"ok": True}}


def _handshaken_pair(root: Path, server: ipc.Server) -> tuple[ipc.ServerConnection, object]:
    """做一次只有传输层握手的连接，返回 (服务端那端, 客户端 raw 连接)。"""
    family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
    holder: list[object] = []

    def connect() -> None:
        try:
            holder.append(
                RawClient(
                    ipc.endpoint_path(root),
                    family=family,
                    authkey=ipc.load_authkey(root),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 断言里要看具体错误
            holder.append(exc)

    connector = threading.Thread(target=connect, daemon=True)
    connector.start()
    conn = server.accept()
    connector.join(timeout=5)
    assert holder and not isinstance(holder[0], Exception), f"握手没有完成：{holder}"
    return conn, holder[0]


def test_peer_close_is_distinguishable_from_timeout(root: Path) -> None:
    """断线必须能从"超时"里区分出来，否则读循环会 100% CPU 空转。

    实测过的真 Bug（不是推理）：演示里客户端 `client.close()` 之后，daemon
    的读线程就在 `poll()` 立刻可读 → `recv_bytes()` 立刻 EOF 之间打转，把
    一整个核烤满，直到进程被杀。根因是 `recv()` 对"超时"和"断线"都只返回
    None；修法是断线时置 `eof`，读循环据此退出。见 `docs/M5-desktop.md §10`。
    """
    server = ipc.Server(root)
    server.start()
    try:
        conn, raw_client = _handshaken_pair(root, server)
        assert not conn.eof, "刚握完手就报 eof"
        started = time.monotonic()
        assert conn.recv(timeout=0.1) is None
        assert not conn.eof, "超时被误判成断线——读循环会被提前打断"
        assert time.monotonic() - started >= 0.09, "带超时的读没有真的等"

        raw_client.close()
        started = time.monotonic()
        assert conn.recv(timeout=1.0) is None
        assert time.monotonic() - started < 0.5, "断线后没有立刻返回（等到超时了）"
        assert conn.eof, "对端断开后只返回了 None，没有留下可判别的 eof"
        # 断开是终态：之后的调用必须立刻返回，不能每轮都重新 poll 一次。
        started = time.monotonic()
        assert conn.recv(timeout=1.0) is None
        assert time.monotonic() - started < 0.1
    finally:
        server.close()


def test_subscription_drop_raises_instead_of_returning_none(
    echo: EchoServer, root: Path
) -> None:
    """订阅连接断开必须是异常，不能伪装成"这一刻没事件"。"""
    client = ipc.Client(root)
    client.connect()
    try:
        assert client.subscribe(1)["from_seq"] == 1
        assert client.next_event(timeout=1) is not None
        echo.stop()
        with pytest.raises(ipc.ConnectionClosed):
            client.next_event(timeout=1)
    finally:
        client.close()


def test_connect_to_missing_daemon_fails_fast(root: Path) -> None:
    with pytest.raises(ipc.ConnectionClosed):
        ipc.Client(root, timeout=0.2).connect()


def test_wrong_authkey_is_rejected(echo, root: Path) -> None:
    """另一个 App 的 bug 拿错 key 时，连接必须失败而不是"就当没认证"。"""
    # 握手在 RawClient 构造里就完成：服务端用它自己的 authkey 校验不过来，
    # 客户端会收到 "digest sent was rejected"。
    with pytest.raises(Exception) as excinfo:
        RawClient(
            ipc.endpoint_path(root),
            family="AF_PIPE" if os.name == "nt" else "AF_UNIX",
            authkey=b"x" * 32,
        )
    assert "auth" in str(excinfo.value).lower() or "Authentication" in type(
        excinfo.value
    ).__name__


# --------------------------------------------------------------------------- 存活判定


def test_probe_distinguishes_alive_from_dead(root: Path) -> None:
    assert ipc.probe(root, timeout=0.2) is False, "没有 daemon 时装作活着会锁死第二次启动"
    server = ipc.Server(root)
    server.start()
    try:
        assert ipc.probe(root) is True
    finally:
        server.close()
    assert ipc.probe(root, timeout=0.2) is False


@pytest.mark.skipif(os.name == "nt", reason="stale socket 文件是 POSIX 概念")
def test_stale_endpoint_is_replaced_on_start(root: Path) -> None:
    """上次 kill -9 留下的 socket 文件不该让下一次启动失败。"""
    endpoint = ipc.endpoint_path(root)
    ipc.run_dir(root).mkdir(parents=True, exist_ok=True)
    Path(endpoint).write_bytes(b"")
    server = ipc.Server(root)
    server.start()
    try:
        assert ipc.probe(root)
    finally:
        server.close()


def test_second_server_on_same_root_is_refused(root: Path) -> None:
    """同一 data_root 上第二个监听者必须起不来（否则两个进程抢同一个库）。"""
    first = ipc.Server(root)
    first.start()
    try:
        second = ipc.Server(root)
        with pytest.raises(ipc.IpcError) as excinfo:
            second.start()
        assert "已被占用" in str(excinfo.value)
    finally:
        first.close()


def test_server_close_unblocks_reader(root: Path) -> None:
    """daemon 退出时，阻塞在 recv 的连接线程必须能醒——否则 shutdown 会卡住。"""
    server = ipc.Server(root)
    server.start()
    family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
    connected: list[object] = []

    def connect() -> None:
        try:
            # 只做传输层握手，不发 hello：本测试要验证的是"关闭服务端时读线程
            # 会不会卡死"，不是 RPC 层的事。
            connected.append(
                RawClient(
                    ipc.endpoint_path(root),
                    family=family,
                    authkey=ipc.load_authkey(root),
                )
            )
        except Exception as exc:  # noqa: BLE001
            connected.append(exc)

    # 握手需要两端同时在场：客户端会等服务端 accept 后才完成挑战-应答，
    # 所以必须先起连接线程，再 accept。
    connector = threading.Thread(target=connect, daemon=True)
    connector.start()
    conn = server.accept()
    connector.join(timeout=5)
    assert connected and not isinstance(connected[0], Exception), (
        f"握手没有完成：{connected}"
    )
    raw_client = connected[0]

    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            # 读线程必须带超时：阻塞在 recv 上时，另一条线程的 close() 会卡在
            # 连接对象内部的锁上——这是 multiprocessing.connection 的既有行为，
            # 也是 daemon 关闭路径必须"轮询 + 标志位"而不是"就地 close"的原因。
            conn.recv(timeout=0.2)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.05)
    stop.set()
    server.close()
    raw_client.close()
    thread.join(timeout=3)
    assert not thread.is_alive(), "关闭服务端后读取线程没有退出"


def test_server_close_unblocks_accept_thread(root: Path) -> None:
    """关闭服务端必须让阻塞在 accept() 的线程退出，并且端点真的消失。

    回归（Windows 实测）：accept 阻塞在 `ConnectNamedPipe` 的 overlapped I/O 上，
    从别的线程关句柄**不会取消**挂起的 I/O——线程永远卡住、管道名一直存在，
    下一个实例于是被拒（"端点已被占用"），重启/接管类测试全挂。POSIX 上关
    listener 就能唤醒，所以这条测试在 macOS/Linux 上一直是绿的，只有 Windows 红。
    """
    server = ipc.Server(root)
    server.start()
    exited = threading.Event()

    def accept_loop() -> None:
        try:
            server.accept()
        except (ipc.IpcError, OSError):
            pass
        finally:
            exited.set()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    time.sleep(0.05)  # 让线程真的进到 accept 里再关
    server.close()
    assert exited.wait(5.0), "close() 之后 accept 线程没有退出"
    assert not ipc.probe(root, timeout=0.5), "关闭后端点还能连上（下一个实例会被拒）"

