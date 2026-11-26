"""M5 Host 的行为测试：单实例、token、静态资源、SSE 桥、监护。

这里**不启动真 daemon**：用一个进程内的假 Daemon（`ipc.Server` + 十几行 handler）
把 Host 的边界测清楚。真正的 Host↔Daemon 端到端在 `tests/test_integration_m5.py`
（daemon 分支合并后补），这样两个 PR 各自能独立跑绿，而不是互相等。

重点覆盖的不是"正常路径能跑"，而是三类真会出事的地方：

1. **没 token 也能读**——本地 HTTP 一旦裸奔，同机任何程序/网页都能读任务、批审批。
2. **DNS 重绑定**——只绑回环挡不住 `evil.example` 解析到 127.0.0.1，Host 头必须校验。
3. **关 UI ≠ 停任务**——Host 的职责是桥，不是所有者；断开订阅者不能影响 Daemon。
"""

from __future__ import annotations

import http.client
import json
import multiprocessing
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from tianshu import cli, host, ipc

# --------------------------------------------------------------------------- 夹具


def _short_root() -> Path:
    base = None if os.name == "nt" else "/tmp"
    return Path(tempfile.mkdtemp(prefix="tshost-", dir=base))


@pytest.fixture()
def root():
    path = _short_root()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeDaemon:
    """最小 Daemon：hello / state / echo / subscribe / shutdown。"""

    def __init__(self, data_root: Path, *, events: list[dict] | None = None) -> None:
        self.data_root = data_root
        self.server = ipc.Server(data_root)
        self.server.start()
        self._stop = threading.Event()
        self.shutdown_reason = ""
        self.connections: list[ipc.ServerConnection] = []
        self._threads: list[threading.Thread] = []
        self._events = events or []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self.server.accept()
            except (OSError, ipc.IpcError):
                if self._stop.is_set():
                    return
                continue
            self.connections.append(conn)
            worker = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(worker)
            worker.start()

    def _serve(self, conn: ipc.ServerConnection) -> None:
        while not self._stop.is_set():
            try:
                message = conn.recv(timeout=0.2)
            except ipc.ProtocolError:
                return
            if message is None:
                if self._stop.is_set():
                    return
                continue
            request_id = str(message.get("id") or "")
            method = str(message.get("method") or "")
            params = message.get("params") or {}
            try:
                if method == "hello":
                    conn.send(
                        ipc.ok(
                            request_id,
                            {
                                "protocol": ipc.negotiate(params),
                                "daemon_version": "fake",
                                "instance_id": "fake-instance",
                                "data_root": str(self.data_root),
                                "event_seq_head": len(self._events),
                                "started_at": 1,
                            },
                        )
                    )
                elif method == "state":
                    conn.send(
                        ipc.ok(
                            request_id,
                            {
                                "tasks": [],
                                "runs": [],
                                "approvals": [],
                                "event_seq_head": len(self._events),
                                "daemon": {"version": "fake", "started_at": 1},
                            },
                        )
                    )
                elif method == "echo":
                    conn.send(ipc.ok(request_id, {"echo": params}))
                elif method == "subscribe":
                    since = int(params.get("since_seq") or 0)
                    conn.send(ipc.ok(request_id, {"subscribed": True, "from_seq": since}))
                    for event in self._events:
                        if int(event["seq"]) >= since:
                            conn.send({"push": "event", "event": event})
                elif method == "shutdown":
                    self.shutdown_reason = str(params.get("reason") or "")
                    conn.send(ipc.ok(request_id, {"ok": True}))
                else:
                    conn.send(ipc.fail(request_id, "unknown_method", f"没有方法 {method}"))
            except (OSError, ipc.IpcError):
                return

    def stop(self) -> None:
        self._stop.set()
        for worker in self._threads:
            worker.join(timeout=1)
        for conn in self.connections:
            conn.close()
        self.server.close()


@pytest.fixture()
def fake_daemon(root: Path):
    events = [
        {
            "seq": index,
            "type": "run.state",
            "run_id": "r1",
            "task_id": "t1",
            "ts": 100 + index,
            "actor_kind": "core",
            "payload": {"state": f"s{index}"},
        }
        for index in (1, 2, 3)
    ]
    daemon = FakeDaemon(root, events=events)
    try:
        yield daemon
    finally:
        daemon.stop()


def _ui_dir(root: Path, *, index_html: str | None = None) -> Path:
    ui = root / "ui"
    (ui / "assets").mkdir(parents=True, exist_ok=True)
    html = index_html or (
        '<!doctype html><html><head><meta name="tianshu-token" '
        'content="__TIANSHU_TOKEN__"></head><body>天枢</body></html>'
    )
    (ui / "index.html").write_bytes(html.encode("utf-8"))
    (ui / "assets" / "app.js").write_bytes(b"console.log('hi')")
    return ui


def _start_bridge(
    root: Path,
    *,
    token: str = "tok",
    release: host.HostReleaseIdentity | None = None,
) -> tuple[host.UiBridge, int]:
    config_options = {
        "data_root": root,
        "ui_dir": _ui_dir(root),
        "token": token,
        "port": 0,
    }
    if release is not None:
        config_options["release"] = release
    config = host.BridgeConfig(**config_options)
    bridge = host.UiBridge(config)
    bridge.serve_background()
    return bridge, bridge.port


def _http(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: bytes | None = None,
    host_header: str | None = None,
    timeout: float = 3.0,
) -> tuple[int, bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        header = [f"{method} {path} HTTP/1.1", f"Host: {host_header or f'127.0.0.1:{port}'}"]
        if token is not None:
            header.append(f"X-Tianshu-Token: {token}")
        if body is not None:
            header.append(f"Content-Length: {len(body)}")
        header.append("Connection: close")
        raw = b""
        try:
            sock.sendall(("\r\n".join(header) + "\r\n\r\n").encode("ascii") + (body or b""))
        except (BrokenPipeError, ConnectionResetError):
            # 超限请求时服务端会先回 413 再断开；客户端可能在 sendall 中途
            # 看到 EPIPE。响应已经写进接收缓冲，继续读即可。
            pass
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                break
            if not chunk:
                break
            raw += chunk
    head, _, payload = raw.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b"\r\n")[0].split(b" ")[1])
    except (IndexError, ValueError):
        raise AssertionError(f"HTTP 响应无法解析：{raw[:200]!r}") from None
    return status, payload


def _rpc(port: int, method: str, params: dict, *, token: str = "tok") -> tuple[int, dict]:
    body = json.dumps({"method": method, "params": params}).encode("utf-8")
    status, payload = _http(port, "POST", "/rpc", token=token, body=body)
    return status, json.loads(payload.decode("utf-8"))


@pytest.mark.parametrize(
    "method,path,body,length",
    [
        ("POST", "/rpc", b"[]", None),
        ("POST", "/rpc", b"null", None),
        ("POST", "/rpc", b'"text"', None),
        ("POST", "/rpc", b"42", None),
        ("POST", "/rpc", b"{}", "not-a-number"),
        ("GET", "/events?since_seq=not-a-number", None, None),
    ],
)
def test_malformed_bridge_inputs_return_bad_params(root, method, path, body, length):
    """真实 HTTP 输入错误应有可识别的 400，不能裸异常断线。"""
    bridge, port = _start_bridge(root)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"X-Tianshu-Token": "tok"}
    if length is not None:
        headers["Content-Length"] = length
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert payload["ok"] is False
        assert payload["error"]["code"] == "bad_params"
    finally:
        connection.close()
        bridge.stop()


def _deep_json_body() -> bytes:
    body = (
        b'{"method":"echo","params":{"x":'
        + b"[" * 10_000
        + b"0"
        + b"]" * 10_000
        + b"}}"
    )
    assert len(body) < host.MAX_RPC_BODY
    return body


def test_deep_json_returns_bad_params_and_preserves_request_boundary(root, fake_daemon):
    """解析深度失败仍须完整消费该帧，下一条普通 JSON 能在原连接成功。"""
    bridge, port = _start_bridge(root)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"X-Tianshu-Token": "tok"}
    body = _deep_json_body()
    # 负控前提：输入并未超字节上限，但本运行时确实拒绝其解析深度。
    with pytest.raises(RecursionError):
        json.loads(body)
    try:
        connection.request("POST", "/rpc", body, headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert payload["ok"] is False
        assert payload["error"]["code"] == "bad_params"
        original_socket = connection.sock
        assert original_socket is not None
        ordinary = {"method": "echo", "params": {"message": "普通 JSON"}}
        connection.request("POST", "/rpc", json.dumps(ordinary).encode(), headers)
        assert connection.sock is original_socket
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["ok"] is True
        assert payload["result"]["echo"] == ordinary["params"]
    finally:
        connection.close()
        bridge.stop()


@pytest.mark.parametrize(
    "extra_headers,status,code",
    [
        ({"X-Tianshu-Token": "wrong"}, 401, "unauthorized"),
        ({"Host": "evil.example"}, 403, "forbidden"),
    ],
)
def test_deep_json_is_authenticated_before_parsing(root, extra_headers, status, code):
    bridge, port = _start_bridge(root)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request(
            "POST", "/rpc", _deep_json_body(), {"X-Tianshu-Token": "tok", **extra_headers}
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == status
        assert payload["ok"] is False
        assert payload["error"]["code"] == code
    finally:
        connection.close()
        bridge.stop()


# --------------------------------------------------------------------------- 单实例锁


def test_second_host_instance_is_refused_with_url(root: Path) -> None:
    _, port = _start_bridge(root)
    info = host.HostInfo(pid=os.getpid(), port=port, token="tok", started_at=1)
    host.write_host_info(root, info)
    try:
        with pytest.raises(host.HostError) as excinfo:
            host.acquire_host_info(root, ui_dir=root / "ui")
    finally:
        host.clear_host_info(root, pid=os.getpid())
    assert f"127.0.0.1:{port}" in str(excinfo.value)


def test_host_release_identity_round_trips_in_lock(root: Path) -> None:
    release = host.HostReleaseIdentity(
        version="0.1.0",
        commit="0123456789abcdef0123456789abcdef01234567",
        target="macos-arm64",
    )
    info = host.HostInfo(
        pid=os.getpid(),
        port=0,
        token="release_token_1234",
        started_at=1,
        ui_dir=str(root / "ui"),
        release=release,
    )

    host.write_host_info(root, info)

    assert host.read_host_info(root) == info
    payload = json.loads(host.host_lock_path(root).read_text(encoding="utf-8"))
    assert payload["release"] == {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "target": "macos-arm64",
    }


def test_host_release_identity_lock_rejects_malformed_shape(root: Path) -> None:
    path = host.host_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": 1,
                "token": "release_token_1234",
                "started_at": 1,
                "ui_dir": str(root / "ui"),
                "release": {
                    "schema": 1,
                    "product": "tianshu-agent",
                    "version": "0.1.0",
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "target": "macos-arm64",
                    "extra": True,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(host.HostError, match="Host 元数据损坏"):
        host.read_host_info(root)


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema", 2), ("product", "other-product")],
    ids=["schema", "product"],
)
def test_host_release_identity_lock_rejects_wrong_schema_or_product(
    root: Path, field: str, value: object
) -> None:
    release: dict[str, object] = {
        "schema": 1,
        "product": "tianshu-agent",
        "version": "0.1.0",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "target": "macos-arm64",
    }
    release[field] = value
    path = host.host_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": 1,
                "token": "release_token_1234",
                "started_at": 1,
                "ui_dir": str(root / "ui"),
                "release": release,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(host.HostError, match="Host 元数据损坏"):
        host.read_host_info(root)


def test_stale_host_lock_is_replaced(root: Path) -> None:
    # 端口上没人应答 = 陈旧锁；不能因为一个残留文件就永远打不开 App。
    dead_port = _free_port()
    host.write_host_info(
        root, host.HostInfo(pid=os.getpid(), port=dead_port, token="old", started_at=1)
    )
    info = host.acquire_host_info(root, ui_dir=root / "ui")
    try:
        assert info.token != "old"
        assert host.read_host_info(root).token == info.token
    finally:
        host.clear_host_info(root, pid=info.pid)


def _blocked_host_metadata_publisher(
    root: str,
    control: Connection,
) -> None:
    data_root = Path(root)
    path = host.host_lock_path(data_root)
    new = host.HostInfo(pid=4343, port=2, token="new_token", started_at=2, ui_dir="ui")
    original_write_bytes = Path.write_bytes
    original_replace = os.replace

    def intercept_write_bytes(self: Path, data: bytes) -> int:
        if self != path:
            return original_write_bytes(self, data)
        with self.open("wb") as handle:
            handle.write(data[:5])
            handle.flush()
            control.send(("blocked", "partial_write", os.getpid()))
            if control.recv() != "release":
                raise AssertionError("测试未释放半截写")
            handle.write(data[5:])
        return len(data)

    def intercept_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) != path:
            original_replace(src, dst)
            return
        control.send(("blocked", "atomic_replace", os.getpid()))
        if control.recv() != "release":
            raise AssertionError("测试未释放原子替换")
        original_replace(src, dst)

    Path.write_bytes = intercept_write_bytes
    os.replace = intercept_replace
    try:
        host.write_host_info(data_root, new)
        control.send(("done", None, os.getpid()))
    except BaseException:
        control.send(("error", traceback.format_exc(), os.getpid()))
        raise
    finally:
        Path.write_bytes = original_write_bytes
        os.replace = original_replace
        control.close()


def test_host_metadata_publication_is_atomic_for_external_readers(root: Path) -> None:
    """桌面壳不持有 Python 的元数据互斥，不能让它读到锁的半截 JSON。

    原实现直接 `Path.write_bytes()`，会先截断现锁再写。Tauri 启动时恰好并发
    读取，就可能把一次短暂发布读成“Host 元数据不合法”，直接进启动失败页。
    发布者放在 spawn 子进程，父进程像 Tauri 壳一样绕过 Python 互斥直接读。子进程
    同时截住旧实现的截断写和新实现的 rename，使新实现下锁在发布前仍完整保留旧
    字节，旧实现下则稳定暴露半截 JSON。
    """
    old = host.HostInfo(pid=4242, port=1, token="old_token", started_at=1, ui_dir="ui")
    new = host.HostInfo(pid=4343, port=2, token="new_token", started_at=2, ui_dir="ui")
    host.write_host_info(root, old)
    path = host.host_lock_path(root)

    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe()
    writer = context.Process(
        target=_blocked_host_metadata_publisher,
        args=(str(root), child_control),
    )
    writer.start()
    child_control.close()
    released = False
    try:
        assert parent_control.poll(5), "publisher did not reach publication barrier"
        ready = parent_control.recv()
        assert ready[0] == "blocked", ready
        assert ready[1] in {"partial_write", "atomic_replace"}, ready
        assert ready[2] != os.getpid(), "publisher must be a real child process"
        published = json.loads(path.read_bytes())
        assert published["pid"] == old.pid
        assert published["token"] == old.token
    finally:
        if writer.is_alive():
            parent_control.send("release")
            released = True
        writer.join(timeout=10)
        parent_control.close()
    assert not writer.is_alive(), "publisher did not exit"
    assert writer.exitcode == 0, writer.exitcode
    assert released
    assert host.read_host_info(root) == new


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_shutdown_endpoint_is_post_only_and_requires_token(root: Path) -> None:
    """关 Host 必须 POST + token。

    回归：这个端点曾经挂在 `do_GET` 上，而 `tianshu host stop` 发的是 POST——
    于是 stop 永远拿到 404，用户看到的是"Host 未在超时内退出"（实测过）。
    读请求不能有副作用：浏览器预取、链接预览、同机任意页面都不该关掉 Host。
    """
    bridge, port = _start_bridge(root)
    calls: list[str] = []

    def stop_all() -> None:
        calls.append("stop")

    bridge.install_stop_callback(stop_all)
    try:
        status, _ = _http(port, "GET", "/shutdown", token="tok")
        assert status == 405, "GET /shutdown 必须是 405（读请求不能关 Host）"
        status, _ = _http(port, "POST", "/shutdown")
        assert status == 401, "没有 token 也能关 Host？"
        status, payload = _http(port, "POST", "/shutdown", token="tok")
        assert status == 200, payload
        assert json.loads(payload)["shutting_down"] is True
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls == ["stop"], "POST /shutdown 没有触发 Host 退出回调"
    finally:
        bridge.stop()


def test_stop_host_stops_a_running_host_and_clears_info(root: Path) -> None:
    """`tianshu host stop` 端到端：真发 HTTP、Host 真退出、锁与端口都清掉。"""
    info = host.acquire_host_info(root, ui_dir=_ui_dir(root))
    bridge = host.UiBridge(
        host.BridgeConfig(data_root=root, ui_dir=Path(info.ui_dir), token=info.token, port=0)
    )
    info.port = bridge.port
    host.write_host_info(root, info)
    stopped = threading.Event()

    def stop_all() -> None:
        try:
            bridge.stop()
        finally:
            stopped.set()

    bridge.install_stop_callback(stop_all)
    bridge.serve_background()
    try:
        assert host.stop_host(root, timeout=5.0) == 0, "stop_host 没能让 Host 退出"
        # `/shutdown` 的响应先发、退出回调随后在服务器线程里跑；断言"回调会被调用"，
        # 不是"回调已经在我读到 200 的那一微秒跑完了"。CI 上这条曾偶发红
        # （`Host 的退出回调没有被调用`，docs-only PR 也红），加有界等待。
        deadline = time.monotonic() + 2
        while not stopped.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stopped.is_set(), "Host 的退出回调没有被调用"
        assert host.read_host_info(root) is None, "退出后 host.lock 没清掉"
        assert not host._http_alive(info), "Host 的端口还在响应"
    finally:
        bridge.stop()


def test_stop_host_when_not_running_is_noop(root: Path) -> None:
    assert host.stop_host(root) == 0


# --------------------------------------------------------------------------- token / Host 头


def test_state_requires_token(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _http(port, "GET", "/state")
        assert status == 401
        assert b"unauthorized" in payload
        status, payload = _http(port, "GET", "/state", token="tok")
        assert status == 200
        assert b'"tasks"' in payload
    finally:
        bridge.stop()


def test_wrong_token_is_rejected(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _http(port, "GET", "/state", token="wrong")
        assert status == 401
    finally:
        bridge.stop()


def test_session_proves_token_and_host_pid_without_touching_daemon(root: Path) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _http(port, "GET", "/session")
        assert status == 401
        status, payload = _http(port, "GET", "/session", token="tok")
        assert status == 200
        assert json.loads(payload) == {"ok": True, "pid": os.getpid()}
    finally:
        bridge.stop()


def test_session_release_identity_matches_the_host_lock(root: Path) -> None:
    release = host.HostReleaseIdentity(
        version="0.1.0",
        commit="0123456789abcdef0123456789abcdef01234567",
        target="macos-arm64",
    )
    info = host.HostInfo(
        pid=os.getpid(),
        port=0,
        token="tok",
        started_at=1,
        ui_dir=str(root / "ui"),
        release=release,
    )
    bridge = host.UiBridge(
        host.BridgeConfig(
            data_root=root,
            ui_dir=_ui_dir(root),
            token=info.token,
            port=0,
            release=info.release,
        )
    )
    info.port = bridge.port
    host.write_host_info(root, info)
    bridge.serve_background()
    try:
        status, payload = _http(bridge.port, "GET", "/session", token=info.token)
        assert status == 200, payload
        lock_payload = json.loads(host.host_lock_path(root).read_text(encoding="utf-8"))
        assert json.loads(payload) == {
            "ok": True,
            "pid": os.getpid(),
            "release": lock_payload["release"],
        }
    finally:
        bridge.stop()


def test_foreign_host_header_is_rejected(root: Path, fake_daemon) -> None:
    """DNS 重绑定：`evil.example` 解析到 127.0.0.1 时，Host 头会是 evil.example。"""
    bridge, port = _start_bridge(root)
    try:
        status, _ = _http(port, "GET", "/state", token="tok", host_header="evil.example")
        assert status == 403
        status, _ = _http(port, "GET", "/state", token="tok", host_header=f"localhost:{port}")
        assert status == 200
    finally:
        bridge.stop()


# --------------------------------------------------------------------------- 静态资源


def test_index_gets_token_injected(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, body = _http(port, "GET", "/", token="tok")
        assert status == 200
        assert b'content="tok"' in body
        assert b"__TIANSHU_TOKEN__" not in body
    finally:
        bridge.stop()


def test_index_without_placeholder_still_gets_meta(root: Path, fake_daemon) -> None:
    ui = _ui_dir(root, index_html="<html><head></head><body>hi</body></html>")
    config = host.BridgeConfig(data_root=root, ui_dir=ui, token="tok2", port=0)
    bridge = host.UiBridge(config)
    bridge.serve_background()
    try:
        status, body = _http(bridge.port, "GET", "/")
        assert status == 200
        assert b'name="tianshu-token" content="tok2"' in body
    finally:
        bridge.stop()


def test_path_traversal_is_blocked(root: Path, fake_daemon) -> None:
    secret = root / "secret.txt"
    secret.write_bytes(b"TOP-SECRET")
    bridge, port = _start_bridge(root)
    try:
        for attempt in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
            status, body = _http(port, "GET", attempt, token="tok")
            assert status == 404, attempt
            assert b"TOP-SECRET" not in body
    finally:
        bridge.stop()


def test_missing_ui_serves_actionable_fallback(root: Path, fake_daemon) -> None:
    config = host.BridgeConfig(data_root=root, ui_dir=root / "nope", token="tok", port=0)
    bridge = host.UiBridge(config)
    bridge.serve_background()
    try:
        status, body = _http(bridge.port, "GET", "/", token="tok")
        assert status == 503
        assert b"pnpm" in body
    finally:
        bridge.stop()


def test_spa_route_falls_back_to_index(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, body = _http(port, "GET", "/tasks/abc", token="tok")
        assert status == 200
        assert b"<html" in body
    finally:
        bridge.stop()


# --------------------------------------------------------------------------- RPC 桥


def test_rpc_proxies_to_daemon(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _rpc(port, "echo", {"n": 7})
        assert status == 200
        assert payload == {"ok": True, "result": {"echo": {"n": 7}}}
    finally:
        bridge.stop()


def test_rpc_unknown_method_maps_to_501(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _rpc(port, "nope", {})
        assert status == 501
        assert payload["error"]["code"] == "unknown_method"
    finally:
        bridge.stop()


def test_rpc_without_daemon_reports_unavailable(root: Path) -> None:
    """Daemon 没起来时前端要看到可区分的错误码，而不是一个 500 堆栈。"""
    bridge, port = _start_bridge(root)
    try:
        status, payload = _rpc(port, "state", {})
        assert status == 503
        assert payload["error"]["code"] == "daemon_unavailable"
    finally:
        bridge.stop()


def test_rpc_rejects_oversized_body(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, payload = _rpc(port, "echo", {"blob": "x" * (host.MAX_RPC_BODY + 10)})
        assert status == 413
        assert payload["error"]["code"] == "bad_params"
    finally:
        bridge.stop()


# --------------------------------------------------------------------------- SSE


def test_events_stream_backfills_from_cursor_and_closes_with_once(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, body = _http(port, "GET", "/events?since_seq=2&once=1", token="tok", timeout=5)
        assert status == 200
        frames = [
            json.loads(line[len("data: ") :])
            for line in body.decode("utf-8").splitlines()
            if line.startswith("data: ")
        ]
        assert [frame["seq"] for frame in frames] == [2, 3], frames
    finally:
        bridge.stop()


def test_events_stream_requires_token(root: Path, fake_daemon) -> None:
    bridge, port = _start_bridge(root)
    try:
        status, _ = _http(port, "GET", "/events?since_seq=0", timeout=3)
        assert status == 401
    finally:
        bridge.stop()


# --------------------------------------------------------------------------- 监护


def test_supervisor_reuses_already_running_daemon(root: Path, fake_daemon) -> None:
    lock = ipc.run_dir(root) / "daemon.lock"
    lock.write_bytes(b'{"pid":999999,"started_at":1,"instance_id":"fake-instance"}')
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "raise SystemExit(9)"])
    supervisor.ensure()
    assert supervisor._process is None
    assert fake_daemon.shutdown_reason == ""


def test_supervisor_shutdown_rejects_an_endpoint_that_ignores_the_request(
    root: Path, fake_daemon, monkeypatch,
) -> None:
    lock = ipc.run_dir(root) / "daemon.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(
        b'{"pid":999999,"started_at":1,"instance_id":"fake-instance"}'
    )
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "pass"])
    monkeypatch.setattr(host, "STOP_CLEANUP_TIMEOUT_S", 0.05)
    with pytest.raises(host.HostError, match="退出"):
        supervisor.stop(reason="user", timeout=2.0)
    assert fake_daemon.shutdown_reason == "user"


def test_supervisor_does_not_stop_a_replacement_daemon(root: Path, fake_daemon) -> None:
    stale = b'{"pid":42,"started_at":1,"instance_id":"daemon_stale"}'
    lock = ipc.run_dir(root) / "daemon.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(stale)
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "pass"])

    supervisor._shutdown_daemon(reason="user", timeout=0.2)

    assert fake_daemon.shutdown_reason == ""
    assert lock.read_bytes() == stale


def test_supervisor_timeout_does_not_kill_a_daemon_that_replaced_the_lock(
    root: Path, fake_daemon, monkeypatch
) -> None:
    from types import SimpleNamespace

    old = b'{"pid":4242,"started_at":1,"instance_id":"fake-instance"}'
    new = b'{"pid":4343,"started_at":2,"instance_id":"daemon_new"}'
    lock = ipc.run_dir(root) / "daemon.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(old)
    replaced = False
    clock = [0.0]

    def probe_after_replacement(*_args, **_kwargs):
        nonlocal replaced
        if not replaced:
            lock.write_bytes(new)
            replaced = True
            clock[0] = 10.0
        return ipc.ProbeResult(True, "controlled-endpoint", "still serving", None, True)

    terminated: list[int] = []
    monkeypatch.setattr(ipc, "probe_result", probe_after_replacement)
    monkeypatch.setattr(host, "time", SimpleNamespace(
        monotonic=lambda: clock[0], sleep=lambda _seconds: None,
    ))
    monkeypatch.setattr(
        host.DaemonSupervisor,
        "_terminate",
        staticmethod(terminated.append),
    )
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "pass"])

    supervisor._shutdown_daemon(reason="user", timeout=5)

    assert fake_daemon.shutdown_reason == "user"
    assert replaced
    assert terminated == []
    assert lock.read_bytes() == new


def test_supervisor_without_daemon_identity_does_not_stop_an_unknown_instance(
    root: Path, fake_daemon
) -> None:
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "pass"])

    supervisor._shutdown_daemon(reason="user", timeout=0.2)

    assert fake_daemon.shutdown_reason == ""


def test_supervisor_reports_failed_spawn(root: Path) -> None:
    supervisor = host.DaemonSupervisor(
        root, [sys.executable, "-c", "raise SystemExit(7)"], start_timeout_s=2
    )
    with pytest.raises(host.HostError):
        supervisor.ensure()




# --------------------------------------------------------------------------- CLI 接线


def test_cli_host_status_reports_not_running(root: Path, capsys) -> None:
    assert cli.main(["host", "status", "--data-root", str(root)]) == 1
    assert "没有在运行" in capsys.readouterr().out


def test_cli_host_stop_is_noop_when_not_running(root: Path, capsys) -> None:
    assert cli.main(["host", "stop", "--data-root", str(root)]) == 0
    assert "Host" in capsys.readouterr().out


def test_cli_host_forwards_release_identity(monkeypatch, root: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run_host(data_root: Path, **kwargs):
        captured["data_root"] = data_root
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(host, "run_host", fake_run_host)
    code = cli.main(
        [
            "host",
            "--data-root",
            str(root),
            "--release-version",
            "0.1.0",
            "--release-commit",
            "0123456789abcdef0123456789abcdef01234567",
            "--release-target",
            "macos-arm64",
        ]
    )

    assert code == 0
    assert captured["release"] == host.HostReleaseIdentity(
        version="0.1.0",
        commit="0123456789abcdef0123456789abcdef01234567",
        target="macos-arm64",
    )


def test_cli_host_status_reports_running_host(root: Path) -> None:
    _, port = _start_bridge(root)
    host.write_host_info(
        root, host.HostInfo(pid=os.getpid(), port=port, token="tok", started_at=1)
    )
    try:
        assert cli.main(["host", "status", "--data-root", str(root)]) == 0
    finally:
        host.clear_host_info(root, pid=os.getpid())


def test_empty_token_meta_is_replaced_not_duplicated(root: Path, fake_daemon) -> None:
    """`ui/index.html` 的形态：一个 content 为空的 meta。必须原地替换。

    回归点：若 Host 另插一个新的 meta，`querySelector` 拿到的是**第一个**（空值），
    前端就永远认为"没被 Host 加载"，表现为白屏 + 无法连接——这条是真实联调时
    会踩到的，不是假想。
    """
    ui = _ui_dir(
        root,
        index_html=(
            '<!doctype html><html><head><meta charset="utf-8" />'
            '<meta name="tianshu-token" content="" /></head><body>app</body></html>'
        ),
    )
    config = host.BridgeConfig(data_root=root, ui_dir=ui, token="real-token", port=0)
    bridge = host.UiBridge(config)
    bridge.serve_background()
    try:
        status, body = _http(bridge.port, "GET", "/", token="real-token")
    finally:
        bridge.stop()
    text = body.decode("utf-8")
    assert status == 200
    assert text.count('name="tianshu-token"') == 1, text
    assert 'content="real-token"' in text


def test_token_injection_handles_placeholder_meta_and_missing_head() -> None:
    token = "t0k"
    placeholder = b'<html><head><meta name="tianshu-token" content="__TIANSHU_TOKEN__"></head>'
    assert b'content="t0k"' in host._inject_token(placeholder, token)
    empty = b'<html><head><meta name="tianshu-token" content=""></head>'
    replaced = host._inject_token(empty, token)
    assert replaced.count(b'name="tianshu-token"') == 1
    bare = b"<body>app</body>"
    injected = host._inject_token(bare, token)
    assert b'name="tianshu-token"' in injected


REPO_UI_DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"


@pytest.mark.skipif(
    not (REPO_UI_DIST / "index.html").is_file(),
    reason="仓库里没有 ui/dist（先 `pnpm --dir ui run build`）",
)
def test_real_ui_dist_is_served_and_get_token(root: Path, fake_daemon) -> None:
    """用**真的**构建产物验证 Host：注入 token 后页面能自己找到它。

    这条测试的价值在于"真 UI 的 index.html 形态"——单测里手写 HTML 永远正确，
    联调时才暴露空 meta 的问题。
    """
    config = host.BridgeConfig(data_root=root, ui_dir=REPO_UI_DIST, token="tok-real", port=0)
    bridge = host.UiBridge(config)
    bridge.serve_background()
    try:
        status, body = _http(bridge.port, "GET", "/", token="tok-real")
        text = body.decode("utf-8")
        assert status == 200
        assert text.count('name="tianshu-token"') == 1, text
        assert 'content="tok-real"' in text
        match = re.search(r'src="\./(assets/[^"]+)"', text)
        assert match is not None, text
        asset_status, asset = _http(bridge.port, "GET", "/" + match.group(1))
        assert asset_status == 200
        assert len(asset) > 1000
    finally:
        bridge.stop()
