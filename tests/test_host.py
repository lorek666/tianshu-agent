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

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
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


def _start_bridge(root: Path, *, token: str = "tok") -> tuple[host.UiBridge, int]:
    config = host.BridgeConfig(
        data_root=root, ui_dir=_ui_dir(root), token=token, port=0
    )
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
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "raise SystemExit(9)"])
    try:
        supervisor.ensure()  # 探活成功就不该 spawn
        assert supervisor._process is None
    finally:
        supervisor.stop()
        assert fake_daemon.shutdown_reason in ("", "user")


def test_supervisor_shutdown_asks_daemon_to_exit(root: Path, fake_daemon) -> None:
    supervisor = host.DaemonSupervisor(root, [sys.executable, "-c", "pass"])
    supervisor.stop(reason="user", timeout=2)
    assert fake_daemon.shutdown_reason == "user"


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
