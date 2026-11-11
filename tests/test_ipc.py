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

import errno
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from multiprocessing.connection import Client as RawClient
from pathlib import Path

import pytest
from support import HangupPeer

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


def _accept_retrying(server: ipc.Server, *, attempts: int = 20) -> ipc.ServerConnection:
    """accept 一次；握手阶段的偶发失败按"可重试"处理，最多试 `attempts` 次。

    为什么测试也要重试：`ipc.Server.accept()` 的契约写的就是"握手阶段失败一律
    可重试"（生产调用方——daemon 的 accept 循环——正是这么写的）。Windows 命名
    管道上，accept 偶尔会先拿到一条握手失败或根本没有认证的连接（探活、半开、
    上一实例的残留句柄都可能），此时真实客户端仍在队列里等着。测试若只 accept
    一次，就会把一次正常的"可重试"当成产品故障——那是测试写得比调用方苛刻。
    """
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return server.accept()
        except ipc.ProtocolError as exc:
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"连续 {attempts} 次 accept 都在握手阶段失败：{last}")


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
    conn = _accept_retrying(server)
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 命名管道没有'只 listen 不握手'的裸句柄（见 tests/support.HangupPeer）",
)
def test_handshake_eof_becomes_connection_closed(root: Path) -> None:
    """对端在认证握手里关连接 → 必须是 `ConnectionClosed`，不能漏出裸 `EOFError`。

    实测（CI macOS，`test_event_stream_ends_when_daemon_goes_away`）：被测用例
    自己关掉 daemon 之后，harness 收尾又补了一次 `request_shutdown()`；端点还在、
    accept 已经停了，`answer_challenge` 抛 `EOFError`。`EOFError` 不是 `OSError`，
    而 `request_shutdown()` 只捕 `(IpcError, OSError)` —— 裸的 stdlib 异常直接
    把整条测试打崩（`1 failed, 1529 passed`）。

    服务端侧 `Server.accept()` 早就把同一种情况映射成可重试的 `ProtocolError`，
    客户端这里必须对齐。**也不许当可重试错误**：重试只会把"已经走了"拖成整个
    timeout（这条用例用 < 0.5s 的上界把"没有白等"钉住）。
    """
    with HangupPeer(root):
        client = ipc.Client(root, timeout=2.0)
        started = time.monotonic()
        with pytest.raises(ipc.ConnectionClosed) as caught:
            client.connect(timeout=2.0)
        elapsed = time.monotonic() - started
        client.close()
    assert isinstance(caught.value, ipc.IpcError), "必须是 ipc 自己的异常类型"
    assert "已关闭" in str(caught.value), f"错误信息看不出是握手期关闭：{caught.value}"
    assert elapsed < 0.5, f"握手期 EOF 被当成可重试错误，白等了 {elapsed:.2f}s"

    # 负控：活的服务端必须照常连上——修法不能把"能连"也一起误杀。
    live_root = _short_root()
    server = EchoServer(live_root)
    try:
        live = ipc.Client(live_root, timeout=2.0)
        live.connect(timeout=2.0)
        try:
            assert live.call("echo", {"ok": True})["echo"] == {"ok": True}
        finally:
            live.close()
    finally:
        server.stop()
        shutil.rmtree(live_root, ignore_errors=True)


def test_handshake_eof_is_mapped_to_connection_closed_everywhere(
    root: Path, monkeypatch
) -> None:
    """跨平台把映射钉死在 `_Socket()` 这一个接缝上（Windows 也跑）。

    真实触发（裸 socket 只 listen 不握手）在 Windows 命名管道上造不出来，
    所以这里直接让传输层抛 `EOFError`——测的是"握手期 EOF 必须变成
    `ConnectionClosed`"，不是"stdlib 会抛 EOFError"（后者由上面的真 socket
    用例证明）。
    """

    def hangup(*_args: object, **_kwargs: object) -> None:
        raise EOFError

    monkeypatch.setattr(ipc, "_Socket", hangup)
    client = ipc.Client(root, timeout=1.0)
    with pytest.raises(ipc.ConnectionClosed) as caught:
        client.connect(timeout=1.0)
    client.close()
    assert isinstance(caught.value, ipc.IpcError)
    assert "已关闭" in str(caught.value), f"错误信息看不出是握手期关闭：{caught.value}"


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
def test_start_returns_with_listening_endpoint_even_without_accept_loop(
    root: Path,
) -> None:
    """`start()` 返回时端点必须真的在 listen：**没有人 accept 也连得上**。

    这是 issue #63 的 backlog 回归。`multiprocessing.connection.Listener` 默认
    `backlog=1`：第一次探活连接（连上就关、不做认证握手）会占满那条队列，
    紧接着的第二次 `connect` 在 macOS 上直接 `ECONNREFUSED`。症状就是
    "端点文件存在、进程活着、probe 连不上"——正是 #63 的断言。

    这里刻意**不起 accept 线程**：连续 8 次探活必须全部成功；队列容量必须
    经得起"探活连接自己留下的死连接"，而不是靠 accept 线程立刻来救。
    """
    server = ipc.Server(root)
    server.start()
    try:
        results = [ipc.probe(root, timeout=0.2) for _ in range(8)]
        assert results == [True] * 8, f"背靠背探活出现失败（backlog 太小）：{results}"
        detail = ipc.probe_result(root, timeout=0.2)
        assert detail.ok is True
        assert detail.attempts == 1
    finally:
        server.close()


@pytest.mark.skipif(os.name == "nt", reason="命名管道没有 ENOENT/ECONNREFUSED 分层")
def test_probe_result_separates_missing_from_not_listening(root: Path) -> None:
    """失败原因要能按 errno 分支：不存在 vs 存在但没人监听，处置完全不同。"""
    missing = ipc.probe_result(root, retry_ms=0)
    assert missing.ok is False
    assert missing.endpoint_exists is False
    assert missing.attempts == 1, "端点不存在时不该重试（会把没有 daemon 变慢）"
    assert "不存在" in missing.reason

    endpoint = ipc.endpoint_path(root)
    ipc.run_dir(root).mkdir(parents=True, exist_ok=True)
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(endpoint)  # bind 了但没 listen：connect 会 ECONNREFUSED
    try:
        not_listening = ipc.probe_result(root, retry_ms=0)
        assert not_listening.ok is False
        assert not_listening.endpoint_exists is True
        assert not_listening.errno == errno.ECONNREFUSED
        assert "无人监听" in not_listening.reason
    finally:
        bound.close()
        Path(endpoint).unlink(missing_ok=True)


@pytest.mark.skipif(os.name == "nt", reason="重试语义只在 POSIX 探活实现上成立")
def test_probe_result_retries_transient_errors_deterministically(
    root: Path, monkeypatch
) -> None:
    """重试逻辑的跨平台确定性证据：第一次 ECONNREFUSED、第二次连通。

    不依赖真实 backlog 语义（那是平台相关的：macOS 满队列立即拒绝，Linux 只是
    排队/阻塞）。直接把 `_probe_once` 换成"第一次拒绝、第二次成功"，钉住：
    1. `retry_ms>0` 会把瞬时失败等回来，且 `attempts>=2`；
    2. `retry_ms=0` 一次都不重试（负控锚点）；
    3. `ENOENT`（端点真的不存在）不重试——否则"没有 daemon"会被预算拖慢。
    """
    runs = root / "run"
    runs.mkdir(parents=True, exist_ok=True)
    endpoint = ipc.endpoint_path(root)
    Path(endpoint).write_bytes(b"")  # 让 endpoint_exists=True，与真实 socket 文件同形

    calls: list[str] = []

    def flaky(data_root: Path, timeout: float):
        calls.append("probe")
        if len(calls) == 1:
            return (
                False, errno.ECONNREFUSED,
                "端点文件存在但无人监听（连接被拒）", endpoint,
            )
        return True, None, "已连接", endpoint

    monkeypatch.setattr(ipc, "_probe_once", flaky)
    result = ipc.probe_result(root, retry_ms=500)
    assert result.ok is True, result
    assert result.attempts == 2, result
    assert len(calls) == 2

    calls.clear()
    no_retry = ipc.probe_result(root, retry_ms=0)
    assert no_retry.ok is False and no_retry.attempts == 1, no_retry
    assert len(calls) == 1, "retry_ms=0 不许重试"

    calls.clear()

    def missing(data_root: Path, timeout: float):
        calls.append("probe")
        return False, errno.ENOENT, "端点不存在（没有守护进程，或进程已退出）", endpoint

    monkeypatch.setattr(ipc, "_probe_once", missing)
    gone = ipc.probe_result(root, retry_ms=500)
    assert gone.ok is False and gone.attempts == 1, gone
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix socket 的排队语义；Windows 走命名管道")
def test_probe_retries_when_endpoint_exists_but_accept_is_behind(
    root: Path, monkeypatch
) -> None:
    """端点存在但队列被占满：`probe` 要在短预算内重试，而不是立刻判死。

    窗口来源不是网络，是本地 accept 队列：探活连接自己会占掉队首，服务端要
    先失败一次握手才能腾出位置。`retry_ms=0` 时必须立刻报 False（负控锚点）；
    有人在 50ms 后 accept 时，默认的短暂重试必须把它等回来。
    """
    if sys.platform != "darwin":
        pytest.skip(
            "AF_UNIX 队列满时'立即拒绝'是 macOS 语义；Linux 的 backlog 只是提示，"
            "同样连接会排队。重试逻辑见 "
            "test_probe_result_retries_transient_errors_deterministically"
        )
    monkeypatch.setattr(ipc, "LISTEN_BACKLOG", 1)
    server = ipc.Server(root)
    server.start()
    drained = threading.Event()
    try:
        endpoint = ipc.endpoint_path(root)
        filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        filler.settimeout(0.5)
        filler.connect(endpoint)  # 唯一队列位被"连了就关"的探活连接占满
        filler.close()
        # 负控锚点：**不重试**时必须立刻报 False（不能假装活着）。
        # 用 retry_ms=0 而不是 probe()：后者的短暂重试会把下面的人为 accept
        # 也等进来，测不到"第一次被拒"。
        assert ipc.probe_result(root, timeout=0.2, retry_ms=0).ok is False, (
            "队列满时探活必须如实失败（返回 True 才是撒谎）"
        )

        def accept_soon() -> None:
            time.sleep(0.05)
            try:
                server.accept()
            except (ipc.IpcError, OSError):
                pass
            drained.set()

        threading.Thread(target=accept_soon, daemon=True).start()
        result = ipc.probe_result(root, timeout=0.2, retry_ms=500)
        assert drained.wait(2.0)
        assert result.ok is True, f"没有等到 accept 腾出队列：{result}"
        assert result.attempts >= 2, "第一次被队列满拒绝后应当重试过"
    finally:
        server.close()


def test_self_connect_requires_a_live_accept_loop(root: Path) -> None:
    """自连探测要**走完认证握手**：只 bind/listen 不够，accept 循环必须活着。

    daemon 用它在 `on_ready` 之前证明"listen + accept + 应用层握手"整条路通；
    这也是 `probe()`（只 connect）与"真的能服务"之间的差别。
    """
    server = ipc.Server(root)
    server.start()
    try:
        assert ipc.self_connect(root, timeout=0.3) is False, (
            "没有 accept 线程时不应能完成握手"
        )
        stop = threading.Event()

        def accept_loop() -> None:
            while not stop.is_set():
                try:
                    conn = server.accept()
                except (ipc.IpcError, OSError):
                    if stop.is_set():
                        return
                    continue
                conn.close()

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        try:
            assert server.wait_accepting(2.0) is True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not ipc.self_connect(
                root, timeout=0.5
            ):
                time.sleep(0.01)
            assert ipc.self_connect(root, timeout=1.0) is True
        finally:
            stop.set()
            thread.join(2)
    finally:
        server.close()


def test_server_exposes_accepting_state(root: Path) -> None:
    """`on_ready` 的判据之一：accept 线程**真的站在 accept() 里**（不是 start 过）。"""
    server = ipc.Server(root)
    server.start()
    try:
        assert server.accepting is False
        assert server.wait_accepting(0.1) is False
        stop = threading.Event()

        def accept_loop() -> None:
            while not stop.is_set():
                try:
                    conn = server.accept()
                except (ipc.IpcError, OSError):
                    return
                conn.close()

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        try:
            assert server.wait_accepting(5.0) is True
            assert server.accepting is True
        finally:
            stop.set()
            thread.join(2)
    finally:
        server.close()


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
    conn = _accept_retrying(server)
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

