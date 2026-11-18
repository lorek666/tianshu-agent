"""W9 网络深度判据：连接期单次 DNS 解析与 Socket IP 钉死 + C2 v2.0 分页与二进制编码。

覆盖契约（`docs/CONTRACT-PHASE2-G3-2026-11-18.md` §3、`docs/network-g2.md` §4.1/§7.1、
`docs/PRD-v1.md` §5.2.3 与红线 8）：

1. 域名解析命中回环 / 私网 / CGNAT / 链路本地云元数据 / IPv6 ULA / IPv4-mapped IPv6
   或「公网 + 内网」混合 RRset → 在 `socket.connect` 前整体 fail-closed，
   `reason == "net.private_address_blocked"`，本地监听 `0` 连接，账本按已知 `0` 字节
   结清，且不自动重试（`attempts == 1`）；
2. 真 TOCTOU DNS Rebinding 防护证明：每跳仅调用一次 `_resolve_host`，`_PinnedHTTPConnection`
   直接向首次校验通过的数值 `sockaddr` 发起连接，`http.client` 零次调用 OS `getaddrinfo`，
   且 `Host` 请求头保留原始域名；
3. 同源 302 重定向跳重绑拦截：第 0 跳解析公网返回 `302 /secret`，第 1 跳重解析翻转为
   `127.0.0.1` / `169.254.169.254` → 第 1 跳连接前拦截；
4. `_PinnedHTTPSConnection` 保留 `CERT_REQUIRED` + `check_hostname=True`，向
   `wrap_socket` 传入原始域名 `server_hostname`（绝非数值 IP），证书不匹配关闭底层套接字；
5. 字面量 IP 规则：白名单 `127.0.0.1:<port>` 零 DNS 解析正常工作，而 `169.254.169.254`、
   `0.0.0.0`、`224.0.0.1`、`fe80::1` 等危险字面量与十进制/十六进制变形主机名一律拦截；
6. 红线 8 升版（`network.fetch` `2.0` + `read_content` `2.0`）：`offset`/`length`/`max_bytes`
   分页与预算预占收紧、`utf-8`/`utf-8-replace`/`latin-1`/`base64` 多页无损往返还原原始
   二进制字节、以及 `1.0` 请求/旧审批跨版本失效回归。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from httpserver import NetFixture
from support import approve_pending, engine_for

import tianshu.net as net_mod
from tianshu.contents import (
    CONTENT_ENCODINGS,
    ContentDigestMismatch,
    ContentError,
    ContentNotVisible,
    make_ref,
    parse_ref,
)
from tianshu.dispatcher import build_request
from tianshu.events import EventLog, canonical_bytes, sha256_hex
from tianshu.loop import Limits, build_stack, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.net import (
    NetError,
    NetLimits,
    _PinnedEndpoint,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    canonical_target,
    fetch,
)
from tianshu.permissions import ACTION_FETCH, ActionRequest, HumanIdentity
from tianshu.resources import NetworkTarget
from tianshu.tasks import RUN_RUNNING
from tianshu.tools import SPECS, tool_manifest

V1_NETWORK_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "minLength": 1, "maxLength": 2048},
    },
    "required": ["url"],
    "additionalProperties": False,
}


def _stack(tmp_path: Path, *, net_limits: NetLimits | None = None):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    kwargs: dict[str, Any] = {"data_root": tmp_path / "data"}
    if net_limits is not None:
        kwargs["net_limits"] = net_limits
    stack = build_stack(log, **kwargs)
    return log, stack


def _drive(stack: Any, engine: Any, task: Any, *, out_name: str = "out.md", rounds: int = 8):
    result = engine.start(task.task_id, out_name=out_name)
    while result.state == "waiting_approval" and rounds > 0:
        rounds -= 1
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    return result


def _tool_results(log: EventLog, run_id: str) -> dict[str, dict[str, Any]]:
    return {
        e.payload["call_id"]: e.payload
        for e in log.read_run(run_id, verify=False)
        if e.type == "tool.result"
    }


class _HeaderRecordingServer:
    """记录请求路径与原始 `Host` 请求头的本机 HTTP 服务。"""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append((self.path, self.headers.get("Host", "")))
                status, hdrs, body = outer.routes.get(
                    self.path, (404, {"Content-Type": "text/plain"}, b"not found")
                )
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class _AcceptCounter:
    """监听一个本机 TCP 端口并统计收到的连接数（证明被拦截请求零次触达目标）。"""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self.accepted = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            conn.close()

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=2.0)


# ---------------------------------------------------------------- 1 DNS 私有地址与混合 RRset 阻断


@pytest.mark.parametrize(
    ("family", "resolved_addrs"),
    [
        (socket.AF_INET, ["127.0.0.1"]),
        (socket.AF_INET, ["10.0.0.1"]),
        (socket.AF_INET, ["172.16.5.4"]),
        (socket.AF_INET, ["192.168.1.10"]),
        (socket.AF_INET, ["169.254.169.254"]),
        (socket.AF_INET, ["100.64.0.1"]),
        (socket.AF_INET, ["192.88.99.1"]),
        (socket.AF_INET, ["0.0.0.0"]),
        (socket.AF_INET, ["224.0.0.1"]),
        (socket.AF_INET6, ["::1"]),
        (socket.AF_INET6, ["fc00::1"]),
        (socket.AF_INET6, ["fec0::1"]),
        (socket.AF_INET6, ["fe80::1"]),
        (socket.AF_INET6, ["fe80::1%lo0"]),
        (socket.AF_INET6, ["100::1"]),
        (socket.AF_INET6, ["2001:20::1"]),
        (socket.AF_INET6, ["2001:db8::1"]),
        (socket.AF_INET6, ["3fff::1"]),
        (socket.AF_INET6, ["5f00::1"]),
        (socket.AF_INET6, ["::ffff:127.0.0.1"]),
        (socket.AF_INET6, ["::ffff:169.254.169.254"]),
        (socket.AF_INET6, ["::ffff:0:127.0.0.1"]),
        (socket.AF_INET6, ["::127.0.0.1"]),
        (socket.AF_INET6, ["2002:7f00:0001::1"]),
        (socket.AF_INET6, ["64:ff9b::127.0.0.1"]),
        (socket.AF_INET6, ["64:ff9b:1::1"]),
        # 混合 RRset：第一条是合法公网 IP，第二条是 127.0.0.1 -> 必须整体 fail-closed！
        (socket.AF_INET, ["93.184.216.34", "127.0.0.1"]),
    ],
)
def test_dns_private_or_mixed_rrset_blocks_before_socket_connect_and_settles_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: int,
    resolved_addrs: list[str],
) -> None:
    guard_listener = _AcceptCounter()
    connect_calls: list[Any] = []
    resolve_calls: list[tuple[str, int]] = []
    real_sock_connect = socket.socket.connect

    def spy_connect(self: socket.socket, address: Any) -> Any:
        connect_calls.append(address)
        return real_sock_connect(self, address)

    def fake_resolve(host: str, port: int) -> list[tuple[Any, ...]]:
        resolve_calls.append((host, port))
        out: list[tuple[Any, ...]] = []
        for addr in resolved_addrs:
            af = socket.AF_INET6 if ":" in addr else family
            sockaddr: tuple[Any, ...] = (
                (addr, port, 0, 0) if af == socket.AF_INET6 else (addr, port)
            )
            out.append((af, socket.SOCK_STREAM, 0, "", sockaddr))
        return out

    monkeypatch.setattr(socket.socket, "connect", spy_connect)
    monkeypatch.setattr(net_mod, "_resolve_host", fake_resolve)

    limits = NetLimits(max_bytes=4096, connect_attempts=3, min_interval_s=0)
    log, stack = _stack(tmp_path, net_limits=limits)
    try:
        origin = f"network://http/rebind.evil.test:{guard_listener.port}"
        url = f"{origin}/secret"
        task = stack.tasks.create_task("防 SSRF", outbound_allow=[origin])
        run_limits = Limits(
            max_turns=6, max_total_tokens=20_000, budget_limits={"bytes": 4096}
        )
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "network.fetch", {"url": url})],
                ),
                ModelResponse("end_turn", content="已如实拦截"),
            ]
        )
        res = _drive(stack, engine_for(stack, model, limits=run_limits), task)
        payload = _tool_results(log, res.run_id)["c1"]
        assert payload["status"] == "failed", payload
        assert payload["effect_state"] == "none_applied"
        assert payload["output"]["reason"] == "net.private_address_blocked"
        assert "ref" not in payload["output"]

        # 关键负控证明：0 次 socket.connect、0 次监听命中、仅解析 1 次（绝不重试）、已知 0 字节结清
        assert connect_calls == []
        assert guard_listener.accepted == 0
        assert resolve_calls == [("rebind.evil.test", guard_listener.port)]

        usage = stack.budget.usage(f"run:{res.run_id}")
        assert usage["settled"].get("bytes", 0) == 0
        assert usage["reserved"].get("bytes", 0) == 0
        unsettled_net = [
            r
            for r in stack.budget.reconcile(f"run:{res.run_id}")
            if r.get("call_id") == "c1"
        ]
        assert unsettled_net == []
    finally:
        log.close()
        guard_listener.close()


# ---------------------------------------------------------------- 2 真 TOCTOU DNS Rebinding 证明


def test_pinned_http_connection_prevents_toctou_dns_rebinding_and_preserves_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使第二次 DNS 查询会翻转成 127.0.0.1，连接器也只用首次钉死的数值 sockaddr，绝不二次解析。"""
    public_srv = _HeaderRecordingServer(
        {"/data": (200, {"Content-Type": "text/plain; charset=utf-8"}, b"safe-public-body")}
    )
    secret_listener = _AcceptCounter()
    resolve_calls: list[tuple[str, int]] = []
    connected_targets: list[Any] = []
    real_sock_connect = socket.socket.connect

    def rebinding_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        resolve_calls.append((host, port))
        if len(resolve_calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]
        # 若发生二次解析（TOCTOU），这里会返回本机敏感端口
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", secret_listener.port))]

    def forbid_os_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"http.client 绝不能再次调用 socket.getaddrinfo: {args} {kwargs}")

    def route_public_ip_to_test_server(self: socket.socket, address: Any) -> Any:
        connected_targets.append(address)
        assert address == ("93.184.216.34", public_srv.port)
        return real_sock_connect(self, ("127.0.0.1", public_srv.port))

    monkeypatch.setattr(socket, "getaddrinfo", forbid_os_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", route_public_ip_to_test_server)

    try:
        target = canonical_target(f"network://http/rebind.ok.test:{public_srv.port}/data")
        fetched = fetch(
            target,
            limits=NetLimits(connect_attempts=2, min_interval_s=0),
            resolver=rebinding_resolver,
        )
        assert fetched.status == 200
        assert fetched.body == b"safe-public-body"
        assert len(resolve_calls) == 1, "单跳最多允许一次 DNS 解析"
        assert connected_targets == [("93.184.216.34", public_srv.port)]
        assert secret_listener.accepted == 0
        # Host 请求头必须保留原始域名与非默认端口，不能退化成 93.184.216.34
        assert public_srv.requests == [("/data", f"rebind.ok.test:{public_srv.port}")]
    finally:
        public_srv.close()
        secret_listener.close()


def test_direct_pinned_connection_opens_numeric_sockaddr_with_zero_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不打 socket.connect 补丁：验证 `_PinnedHTTPConnection` 禁掉 `getaddrinfo` 仍能建连。"""
    srv = _HeaderRecordingServer(
        {"/hello": (200, {"Content-Type": "text/plain"}, b"pinned-ok")}
    )

    def forbid_getaddrinfo(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("禁止调用 socket.getaddrinfo")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_getaddrinfo)
    try:
        conn = _PinnedHTTPConnection(
            "origin.example.test",
            None,
            endpoints=(
                _PinnedEndpoint(
                    socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", srv.port)
                ),
            ),
            timeout=3.0,
        )
        try:
            conn.connect()
            conn.request("GET", "/hello")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b"pinned-ok"
        finally:
            conn.close()
        assert srv.requests == [("/hello", "origin.example.test")]
    finally:
        srv.close()


# ---------------------------------------------------------------- 3 重定向跳 DNS 重绑拦截


@pytest.mark.parametrize("hop1_ip", ["127.0.0.1", "169.254.169.254"])
def test_redirect_hop_dns_rebinding_to_private_ip_is_blocked_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hop1_ip: str
) -> None:
    hop0_srv = _HeaderRecordingServer(
        {"/start": (302, {"Location": "/secret"}, b"")}
    )
    secret_listener = _AcceptCounter()
    resolve_calls: list[tuple[str, int]] = []
    connected_targets: list[Any] = []
    real_sock_connect = socket.socket.connect

    def hop_rebinding_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
        resolve_calls.append((host, port))
        if len(resolve_calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (hop1_ip, secret_listener.port))]

    def route_hop0_only(self: socket.socket, address: Any) -> Any:
        connected_targets.append(address)
        if address == ("93.184.216.34", hop0_srv.port):
            return real_sock_connect(self, ("127.0.0.1", hop0_srv.port))
        return real_sock_connect(self, address)

    monkeypatch.setattr(net_mod, "_resolve_host", hop_rebinding_resolver)
    monkeypatch.setattr(socket.socket, "connect", route_hop0_only)

    log, stack = _stack(
        tmp_path, net_limits=NetLimits(connect_attempts=3, min_interval_s=0)
    )
    try:
        origin = f"network://http/rebind.evil.test:{hop0_srv.port}"
        start_url = f"{origin}/start"
        task = stack.tasks.create_task("防跳后重绑", outbound_allow=[origin])
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("c1", "network.fetch", {"url": start_url})],
                ),
                ModelResponse("end_turn", content="已阻断第 1 跳重绑"),
            ]
        )
        res = _drive(stack, engine_for(stack, model), task)
        payload = _tool_results(log, res.run_id)["c1"]
        assert payload["status"] == "failed", payload
        assert payload["effect_state"] == "none_applied"
        assert payload["output"]["reason"] == "net.private_address_blocked"

        # Hop 0 建连 1 次，Hop 1 在 connect() 之前被拒，secret_listener 命中为 0，且绝不重试
        assert len(resolve_calls) == 2
        assert connected_targets == [("93.184.216.34", hop0_srv.port)]
        assert secret_listener.accepted == 0
    finally:
        log.close()
        hop0_srv.close()
        secret_listener.close()


# ---------------------------------------------------------------- 4 HTTPS TLS SNI 与主机名校验保持


def test_pinned_https_connection_preserves_tls_sni_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = int(listener.getsockname()[1])

    accepted: list[socket.socket] = []

    def accept_once() -> None:
        while True:
            try:
                peer, _ = listener.accept()
            except OSError:
                return
            accepted.append(peer)

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()

    def forbid_getaddrinfo(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("HTTPS 钉死连接禁止二次调用 getaddrinfo")

    monkeypatch.setattr(socket, "getaddrinfo", forbid_getaddrinfo)

    wrapped_calls: list[tuple[str | None, int, bool]] = []

    class _FakeWrappedSocket:
        def __init__(self, raw: socket.socket) -> None:
            self._raw = raw

        def close(self) -> None:
            self._raw.close()

    ctx = ssl.create_default_context()

    def spy_wrap_socket(
        raw_sock: socket.socket, *, server_hostname: str | None = None
    ) -> Any:
        wrapped_calls.append((server_hostname, int(ctx.verify_mode), bool(ctx.check_hostname)))
        return _FakeWrappedSocket(raw_sock)

    monkeypatch.setattr(ctx, "wrap_socket", spy_wrap_socket)

    try:
        conn = _PinnedHTTPSConnection(
            "tls.example.test",
            443,
            endpoints=(
                _PinnedEndpoint(socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", port)),
            ),
            timeout=3.0,
            context=ctx,
        )
        try:
            conn.connect()
        finally:
            conn.close()

        assert wrapped_calls == [
            ("tls.example.test", int(ssl.CERT_REQUIRED), True)
        ], "wrap_socket 必须收到原始域名 server_hostname 且开启 CERT_REQUIRED + check_hostname"
    finally:
        for peer in accepted:
            peer.close()
        listener.close()
        t.join(timeout=2.0)


def test_https_cert_mismatch_closes_raw_socket_and_maps_to_net_tls_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _AcceptCounter()
    closed_count = {"n": 0}
    real_close = socket.socket.close

    def tracking_close(self: socket.socket) -> None:
        closed_count["n"] += 1
        real_close(self)

    class _RejectingSSLContext(ssl.SSLContext):
        def wrap_socket(self, sock: socket.socket, *args: Any, **kwargs: Any) -> ssl.SSLSocket:
            assert kwargs.get("server_hostname") == "badcert.example.test"
            raise ssl.SSLCertVerificationError("certificate verify failed: hostname mismatch")

    def fake_default_context() -> ssl.SSLContext:
        ctx = _RejectingSSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    real_sock_connect = socket.socket.connect

    def route_to_listener(self: socket.socket, address: Any) -> Any:
        assert address == ("93.184.216.34", 443)
        return real_sock_connect(self, ("127.0.0.1", listener.port))

    resolve_calls = {"n": 0}

    def single_resolve(host: str, port: int) -> list[tuple[Any, ...]]:
        resolve_calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]

    monkeypatch.setattr(ssl, "create_default_context", fake_default_context)
    monkeypatch.setattr(socket.socket, "connect", route_to_listener)
    monkeypatch.setattr(socket.socket, "close", tracking_close)

    try:
        target = canonical_target("network://https/badcert.example.test/secret")
        with pytest.raises(NetError) as excinfo:
            fetch(
                target,
                limits=NetLimits(connect_attempts=3, min_interval_s=0),
                resolver=single_resolve,
            )
        assert excinfo.value.reason == "net.tls"
        assert resolve_calls["n"] == 1, "TLS 握手/证书失败不得自动重试"
        assert closed_count["n"] >= 1, "wrap_socket 抛错时必须立即关闭底层 TCP 套接字"
    finally:
        listener.close()


# ---------------------------------------------------------------- 5 字面量 IP 与主机变形规则


def test_literal_loopback_allowlisted_skips_dns_while_metadata_and_unspecified_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = NetFixture().route("/ok", body="loopback-fixture-ok").start()

    def forbid_resolve(host: str, port: int) -> list[tuple[Any, ...]]:
        raise AssertionError(f"字面量 IP 不应触发 DNS 解析：{host}:{port}")

    monkeypatch.setattr(net_mod, "_resolve_host", forbid_resolve)

    log, stack = _stack(tmp_path, net_limits=NetLimits(min_interval_s=0))
    try:
        # 1) 显式白名单的 127.0.0.1:<port> 夹具零 DNS 解析正常工作
        ok_task = stack.tasks.create_task("本机夹具", outbound_allow=[fixture.origin_uri])
        ok_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall("c_ok", "network.fetch", {"url": fixture.url("/ok")})
                    ],
                ),
                ModelResponse("end_turn", content="ok"),
            ]
        )
        ok_res = _drive(stack, engine_for(stack, ok_model), ok_task)
        ok_out = _tool_results(log, ok_res.run_id)["c_ok"]["output"]
        assert ok_out["summary"] == "loopback-fixture-ok"

        # 2) 即使人把危险字面量写进白名单并批准，执行侧也恒定阻断
        for idx, bad_origin, bad_url in [
            (
                1,
                "network://http/169.254.169.254",
                "network://http/169.254.169.254/latest/meta-data/",
            ),
            (2, "network://http/0.0.0.0", "network://http/0.0.0.0/"),
            (3, "network://http/224.0.0.1", "network://http/224.0.0.1/"),
            (4, "network://http/255.255.255.255", "network://http/255.255.255.255/"),
        ]:
            bad_task = stack.tasks.create_task(
                f"危险字面量-{idx}", outbound_allow=[bad_origin]
            )
            bad_model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                f"c_bad_{idx}", "network.fetch", {"url": bad_url}
                            )
                        ],
                    ),
                    ModelResponse("end_turn", content="blocked"),
                ]
            )
            bad_res = _drive(stack, engine_for(stack, bad_model), bad_task)
            bad_payload = _tool_results(log, bad_res.run_id)[f"c_bad_{idx}"]
            assert bad_payload["status"] == "failed", bad_payload
            assert bad_payload["output"]["reason"] == "net.private_address_blocked"

        # 3) IPv6 站点本地与 NAT64 / SIIT / IPv4-compatible 内嵌云元数据等危险字面量全部阻断
        for ipv6_bad in (
            "::",
            "fec0::1",
            "fe80::1",
            "fe80::1%lo0",
            "ff02::1",
            "::ffff:169.254.169.254",
            "::ffff:0.0.0.0",
            "::ffff:0:169.254.169.254",
            "::169.254.169.254",
            "2002:a9fe:a9fe::1",
            "64:ff9b::169.254.169.254",
            "64:ff9b:1::169.254.169.254",
            "64:ff9b:1:fffe::a9fe:a9fe",
            "64:ff9b:1:fffe:00a9:fea9:fe00:0000",
            "64:ff9b:1:ffa9:00fe:a9fe:0000:0000",
            "64:ff9b:1:a9fe:00a9:fe00:0000:0000",
            "64:ff9b:1::",
        ):
            target = NetworkTarget(transport="http", host=ipv6_bad, port=8080, path="/")
            with pytest.raises(NetError) as excinfo:
                fetch(target, limits=NetLimits(connect_attempts=1, min_interval_s=0))
            assert excinfo.value.reason == "net.private_address_blocked", ipv6_bad

        # 显式 `::1` 字面量仍被当作合法回环字面量钉死（不会误拆为 IPv4 0.0.0.1）
        loopback_v6 = NetworkTarget(transport="http", host="::1", port=8080, path="/")
        pinned_v6 = net_mod._resolve_and_pin(loopback_v6)
        assert len(pinned_v6) == 1 and pinned_v6[0].sockaddr[:2] == ("::1", 8080)
    finally:
        log.close()
        fixture.stop()


@pytest.mark.parametrize("sneaky_host", ["2130706433", "0x7f000001", "0177.0.0.1"])
def test_non_canonical_numeric_hosts_go_through_dns_gate_and_are_blocked(
    sneaky_host: str,
) -> None:
    """十进制整数 / 十六进制 / 八进制变形不被当作合法字面量 IP 豁免，解析到 127.0.0.1 即拦截。"""
    target = canonical_target(f"network://http/{sneaky_host}/")
    with pytest.raises(NetError) as excinfo:
        fetch(
            target,
            limits=NetLimits(connect_attempts=1, min_interval_s=0),
            resolver=lambda _h, p: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", p))],
        )
    assert excinfo.value.reason == "net.private_address_blocked"


# ---------------------------------------------------------------- 6 v2.0 分页、编码往返与红线 8


def test_c2_v2_manifest_and_red_line_8_rejects_v1_request_and_approval(
    tmp_path: Path,
) -> None:
    """`network.fetch` 与 `read_content` 同步升为 `2.0`，旧 `1.0` 请求与旧批准一律失效。"""
    assert SPECS["network.fetch"].version == "2.0"
    assert SPECS["read_content"].version == "2.0"

    manifest_by_name = {entry["name"]: entry for entry in tool_manifest()}
    fetch_props = manifest_by_name["network.fetch"]["input_schema"]["properties"]
    assert set(fetch_props) == {"url", "offset", "length", "max_bytes"}
    assert fetch_props["offset"] == {"type": "integer", "minimum": 0}
    assert fetch_props["length"] == {"type": "integer", "minimum": 1, "maximum": 65536}
    assert fetch_props["max_bytes"] == {"type": "integer", "minimum": 1, "maximum": 1048576}

    read_props = manifest_by_name["read_content"]["input_schema"]["properties"]
    assert read_props["encoding"] == {
        "type": "string",
        "enum": list(CONTENT_ENCODINGS),
    }

    fixture = NetFixture().route("/doc", body="v2").start()
    log, stack = _stack(tmp_path, net_limits=NetLimits(min_interval_s=0))
    try:
        url = fixture.url("/doc")
        task = stack.tasks.create_task("红线8", outbound_allow=[fixture.origin_uri])
        run_obj = stack.tasks.start_run(task.task_id)
        job = stack.claims.enqueue(run_obj.run_id)
        claim = stack.claims.claim(job.job_id)
        stack.tasks.transition(run_obj.run_id, RUN_RUNNING, claim=claim)

        req_v2 = build_request(
            task=task,
            run=stack.tasks.get_run(run_obj.run_id),
            claim=claim,
            call_id="c_v2",
            tool_id="network.fetch",
            target=url,
            args={"url": url},
        )
        assert req_v2.tool_version == "2.0"

        # 构造一张历史 1.0 请求及已批准记录
        req_v1 = ActionRequest(
            request_id="req_legacy_v1",
            task_id=task.task_id,
            run_id=run_obj.run_id,
            job_id=claim.job_id,
            claim_epoch=claim.claim_epoch,
            owner_instance_id=claim.owner_instance_id,
            goal_revision=run_obj.goal_revision,
            lease_epoch=run_obj.lease_epoch,
            call_id="c_v1",
            attempt_id=1,
            tool_id="network.fetch",
            tool_version="1.0",
            schema_hash=sha256_hex(canonical_bytes(V1_NETWORK_FETCH_SCHEMA)),
            description_hash=req_v2.description_hash,
            action=ACTION_FETCH,
            resources=(url,),
            args_hash=sha256_hex(canonical_bytes({"url": url})),
            preconditions={},
        )
        # 1) 旧 1.0 请求提交给当前 2.0 派发器 -> dispatch.not_admitted 拒绝
        dec_v1 = stack.dispatcher.prepare(req_v1, claim, args={"url": url})
        assert dec_v1.is_rejected
        assert dec_v1.rule_id == "dispatch.not_admitted"
        assert fixture.hit_count == 0

        # 2) 即便库中已存在针对相同 URL 的 1.0 已批准审批，2.0 请求也绝不复用它
        pol_decision = stack.dispatcher._policy.classify(
            ACTION_FETCH, [url], [], {}, task_id=task.task_id, tool_id="network.fetch"
        )
        old_approval = stack.approvals.request(req_v1, pol_decision)
        stack.approvals.decide(
            old_approval.approval_id,
            decision="approved",
            actor=HumanIdentity.local_cli(),
        )
        assert stack.approvals.find_for(req_v2) is None
        dec_v2 = stack.dispatcher.prepare(req_v2, claim, args={"url": url})
        assert dec_v2.is_waiting, "2.0 必须申请全新审批，不能借用 1.0 批准"
    finally:
        log.close()
        fixture.stop()


def test_network_fetch_v2_max_bytes_budget_and_pagination_handoff_to_read_content(
    tmp_path: Path,
) -> None:
    """`max_bytes` 收紧预检与预占上界（不超过系统上限），`next_offset` 衔接 `read_content`。"""
    full_text = "".join(f"第{i:03d}段：天枢网络分页校验🌍\n" for i in range(60))
    raw_bytes = full_text.encode()
    fixture = (
        NetFixture()
        .route("/article", body=raw_bytes)
        .route("/capped", body=b"A" * 5000)
        .start()
    )
    # 系统上限默认 1 MiB，Run 字节预算仅 4096 B：
    # - 不带 max_bytes 时需预占 1 MiB -> prepare() 直接拒发且不弹批准；
    # - 带 max_bytes=2048 时预占 2048 B <= 4096 B -> 正常审批、抓取并封顶在 2048 B。
    log, stack = _stack(tmp_path, net_limits=NetLimits(min_interval_s=0))
    try:
        task = stack.tasks.create_task("预算与分页", outbound_allow=[fixture.origin_uri])
        run_limits = Limits(
            max_turns=8, max_total_tokens=40_000, budget_limits={"bytes": 4096}
        )
        cap_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "c_too_big",
                            "network.fetch",
                            {"url": fixture.url("/capped")},
                        )
                    ],
                ),
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "c_fit",
                            "network.fetch",
                            {
                                "url": fixture.url("/capped"),
                                "max_bytes": 2048,
                                "offset": 10,
                                "length": 100,
                            },
                        )
                    ],
                ),
                ModelResponse("end_turn", content="done"),
            ]
        )
        cap_res = _drive(stack, engine_for(stack, cap_model, limits=run_limits), task)
        cap_results = _tool_results(log, cap_res.run_id)
        assert cap_results["c_too_big"]["status"] == "rejected"
        assert cap_results["c_too_big"]["output"]["rule_id"] == "dispatch.budget_refused"

        fit_out = cap_results["c_fit"]["output"]
        assert fit_out["bytes"] == 2048
        assert fit_out["truncated"] is True
        assert fit_out["offset"] == 10
        assert fit_out["page_bytes"] == 100
        assert fit_out["next_offset"] == 110
        assert fit_out["summary"] == "A" * 100
        usage = stack.budget.usage(f"run:{cap_res.run_id}")
        assert usage["settled"]["bytes"] == 2048

        # 请求传入更大的 max_bytes 时仍被系统 NetLimits.max_bytes 硬封顶
        tight_log, tight_stack = _stack(
            tmp_path / "tight", net_limits=NetLimits(max_bytes=512, min_interval_s=0)
        )
        try:
            tight_task = tight_stack.tasks.create_task(
                "系统上限封顶", outbound_allow=[fixture.origin_uri]
            )
            tight_model = ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "c_clamp",
                                "network.fetch",
                                {"url": fixture.url("/capped"), "max_bytes": 65536},
                            )
                        ],
                    ),
                    ModelResponse("end_turn", content="done"),
                ]
            )
            tight_res = _drive(
                tight_stack, engine_for(tight_stack, tight_model), tight_task
            )
            tight_out = _tool_results(tight_log, tight_res.run_id)["c_clamp"]["output"]
            assert tight_out["bytes"] == 512
            assert tight_out["truncated"] is True
        finally:
            tight_log.close()

        # 验证从首屏 summary (length=73，落在多字节字符内) 离线续读完整 UTF-8 原文
        class _PagingFetchModel(ScriptedModel):
            def __init__(self) -> None:
                super().__init__([])
                self.parts: list[str] = []
                self.ref = ""
                self.next_off: int | None = None
                self.step = 0

            def complete(self, messages: Any, tools: Any) -> ModelResponse:
                self.calls += 1
                if self.step == 0:
                    self.step = 1
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "f0",
                                "network.fetch",
                                {"url": fixture.url("/article"), "offset": 0, "length": 73},
                            )
                        ],
                    )
                last_tool = json.loads(messages[-1]["content"])
                if self.step == 1:
                    self.parts.append(last_tool["summary"])
                    self.ref = last_tool["ref"]
                    self.next_off = last_tool["next_offset"]
                    assert last_tool["offset"] == 0
                    assert 0 < last_tool["page_bytes"] <= 73
                    assert last_tool["next_offset"] == last_tool["page_bytes"]
                    self.step = 2
                else:
                    self.parts.append(last_tool["data"])
                    self.next_off = last_tool["next_offset"]

                if self.next_off is not None:
                    call_idx = self.step
                    self.step += 1
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                f"r{call_idx}",
                                "read_content",
                                {"ref": self.ref, "offset": self.next_off, "length": 503},
                            )
                        ],
                    )
                return ModelResponse("end_turn", content="全文拼装完成")

        pager = _PagingFetchModel()
        page_task = stack.tasks.create_task("首屏+离线翻页", outbound_allow=[fixture.origin_uri])
        page_res = _drive(
            stack,
            engine_for(stack, pager, limits=Limits(max_turns=12, max_total_tokens=50_000)),
            page_task,
        )
        assert page_res.outcome in {"succeeded", "noop"}
        assert "".join(pager.parts) == full_text
        # 全程对 /article 只发过 1 次 HTTP 请求！
        assert fixture.paths().count("/article") == 1
    finally:
        log.close()
        fixture.stop()


def test_read_content_v2_encodings_reconstruct_exact_binary_and_non_utf8_payloads(
    tmp_path: Path,
) -> None:
    """`utf-8` / `utf-8-replace` / `latin-1` / `base64` 四模式分页往返与可见性/哈希核验。"""
    # 构造含 PNG 文件头、NUL、全部 0x00..0xFF 字节与夹杂中文 UTF-8 的混合二进制载荷
    binary_payload = (
        b"\x89PNG\r\n\x1a\n\x00\xff\xfe\x80"
        + "天枢二进制分页".encode()
        + bytes(range(256)) * 4
        + b"\xc3\x28\xe4\xb8\xadTail"
    )
    fixture = NetFixture().route(
        "/bin", body=binary_payload, content_type="application/octet-stream"
    ).start()
    log, stack = _stack(tmp_path, net_limits=NetLimits(min_interval_s=0))
    try:
        # 端到端通过 AgentLoop 用 base64 多页（奇数页长 331，非 3 倍数）完整读回二进制内容
        class _Base64LoopModel(ScriptedModel):
            def __init__(self) -> None:
                super().__init__([])
                self.step = 0
                self.ref = ""
                self.next_off: int | None = 0
                self.chunks: list[bytes] = []

            def complete(self, messages: Any, tools: Any) -> ModelResponse:
                self.calls += 1
                if self.step == 0:
                    self.step = 1
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "fetch_bin",
                                "network.fetch",
                                {"url": fixture.url("/bin"), "length": 64},
                            )
                        ],
                    )
                last = json.loads(messages[-1]["content"])
                if self.step == 1:
                    self.ref = last["ref"]
                    self.next_off = 0
                    self.step = 2
                    # 先试一次默认 utf-8：二进制必须被拒绝
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[ToolCall("read_utf8_fail", "read_content", {"ref": self.ref})],
                    )
                if self.step == 2:
                    assert "ContentError" in last.get("error", "")
                    self.step = 3
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "b64_0",
                                "read_content",
                                {
                                    "ref": self.ref,
                                    "offset": 0,
                                    "length": 331,
                                    "encoding": "base64",
                                },
                            )
                        ],
                    )
                assert last["encoding"] == "base64"
                raw_chunk = base64.b64decode(last["data"].encode("ascii"), validate=True)
                assert len(raw_chunk) == last["bytes"]
                if last["next_offset"] is not None:
                    assert last["offset"] + last["bytes"] == last["next_offset"]
                self.chunks.append(raw_chunk)
                self.next_off = last["next_offset"]
                if self.next_off is not None:
                    cid = f"b64_{self.step}"
                    self.step += 1
                    return ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                cid,
                                "read_content",
                                {
                                    "ref": self.ref,
                                    "offset": self.next_off,
                                    "length": 331,
                                    "encoding": "base64",
                                },
                            )
                        ],
                    )
                return ModelResponse("end_turn", content="二进制还原完成")

        b64_model = _Base64LoopModel()
        task = stack.tasks.create_task("二进制读取", outbound_allow=[fixture.origin_uri])
        res = _drive(
            stack,
            engine_for(stack, b64_model, limits=Limits(max_turns=14, max_total_tokens=50_000)),
            task,
        )
        assert res.outcome in {"succeeded", "noop"}
        assert b"".join(b64_model.chunks) == binary_payload

        # 验证 latin-1 单字节无损多页往返（使用 read_page 入口）
        latin_parts: list[str] = []
        off: int | None = 0
        while off is not None:
            page = stack.contents.read_page(
                b64_model.ref,
                task_id=task.task_id,
                offset=off,
                length=777,
                encoding="latin-1",
            )
            payload_dict = page.to_payload()
            assert payload_dict["encoding"] == "latin-1"
            if payload_dict["next_offset"] is not None:
                assert payload_dict["offset"] + payload_dict["bytes"] == payload_dict["next_offset"]
            latin_parts.append(page.data)
            off = page.next_offset
        assert "".join(latin_parts).encode("latin-1") == binary_payload

        # 验证 utf-8-replace：遇到跨页多字节字符自动退回整字边界，坏字节替换为 U+FFFD，绝不死循环
        mixed_utf8 = b"\xff" + ("中文🚀" * 40).encode("utf-8") + b"\xfeEnd"
        mixed_digest = hashlib.sha256(mixed_utf8).hexdigest()
        blob_path = stack.snapshots.blob_dir / mixed_digest
        blob_path.write_bytes(mixed_utf8)
        stack.contents.record(mixed_digest, mixed_utf8, task_id=task.task_id, run_id=res.run_id)
        mixed_ref = make_ref(mixed_digest)

        rep_parts: list[str] = []
        off = 0
        while off is not None:
            page = stack.contents.read_page(
                mixed_ref,
                task_id=task.task_id,
                offset=off,
                length=19,  # 素数页长，确保频繁切中 3 字节汉字与 4 字节 emoji 内部
                encoding="utf-8-replace",
            )
            pdict = page.to_payload()
            assert pdict["encoding"] == "utf-8-replace"
            if pdict["next_offset"] is not None:
                assert pdict["offset"] + pdict["bytes"] == pdict["next_offset"]
            rep_parts.append(page.data)
            off = page.next_offset
        assert "".join(rep_parts) == mixed_utf8.decode("utf-8", "replace")

        # 非法 encoding 在准入层被 schema 拒（tool.not_dispatched），存储层直调亦抛 ContentError
        with pytest.raises(ContentError):
            stack.contents.read_page(mixed_ref, task_id=task.task_id, encoding="utf-16")

        ws = tmp_path / "ws"
        ws.mkdir()
        bad_enc_res = run(
            task="坏编码参数",
            workspace_dir=ws,
            out_name="out.md",
            model=ScriptedModel(
                [
                    ModelResponse(
                        "tool_use",
                        tool_calls=[
                            ToolCall(
                                "bad_enc",
                                "read_content",
                                {"ref": mixed_ref, "encoding": "utf-16"},
                            )
                        ],
                    ),
                    ModelResponse("end_turn", content="done"),
                ]
            ),
            log=log,
        )
        not_dispatched = [
            e
            for e in log.read_run(bad_enc_res.run_id, verify=False)
            if e.type == "tool.not_dispatched" and e.payload.get("call_id") == "bad_enc"
        ]
        assert len(not_dispatched) == 1

        # 跨任务不可见与摘要防篡改在全部 4 种编码下均一致生效
        other_task = stack.tasks.create_task("无关任务")
        for enc in CONTENT_ENCODINGS:
            with pytest.raises(ContentNotVisible):
                stack.contents.read_page(mixed_ref, task_id=other_task.task_id, encoding=enc)

        tampered_digest = parse_ref(mixed_ref)
        (stack.snapshots.blob_dir / tampered_digest).write_bytes(b"corrupted")
        for enc in CONTENT_ENCODINGS:
            with pytest.raises(ContentDigestMismatch):
                stack.contents.read_page(mixed_ref, task_id=task.task_id, encoding=enc)
    finally:
        log.close()
        fixture.stop()


def test_utf8_replace_maximal_subpart_and_summary_page_cursor_progress(
    tmp_path: Path,
) -> None:
    """Unicode §3.9 maximal subpart 跨页一致性与 `summary_page`/`read_page` 游标严格前进回归。"""
    from tianshu.net import Fetched, summary_page

    fixture = NetFixture().route("/cjk", body="中文测试".encode()).start()
    log, stack = _stack(tmp_path, net_limits=NetLimits(min_interval_s=0))
    try:
        task = stack.tasks.create_task("编码边界", outbound_allow=[fixture.origin_uri])

        # 1) 1/2/3 字节残缺前缀与合法 1/2/3/4 字节码点在 plen=1..5 下的跨页往返与边界拒绝
        corpus: list[tuple[bytes, int]] = [
            (b"\xffABCDEF", 1),
            (b"\xc2ABCDEF", 1),
            (b"\xe4ABCDEF", 1),
            (b"\xf0ABCDEF", 1),
            (b"\xed\xa0\x80ABCDEF", 1),
            (b"\xe4\xb8ABCDEF", 2),        # 2 字节 maximal subpart -> 必须产出单个 U+FFFD
            (b"X\xe4\xb8ABCDEF", 2),
            (b"\xe4\xb8\xe4\xb8Z", 2),
            (b"\xf0\x9fABCDEF", 2),
            (b"\xf0\x9f\x98ABCDEF", 3),    # 3 字节 maximal subpart -> 必须产出单个 U+FFFD
            (b"XY\xf0\x9f\x98ABCDEF", 3),
            ("Aé中🌍Tail".encode(), 4),
            (b"\xe4\xb8" + "é中🌍".encode() + b"\xf0\x9f\x98Z", 4),
        ]

        for idx, (raw, min_full_plen) in enumerate(corpus):
            digest = hashlib.sha256(raw).hexdigest()
            (stack.snapshots.blob_dir / digest).write_bytes(raw)
            stack.contents.record(digest, raw, task_id=task.task_id, run_id=f"r_{idx}")
            ref = make_ref(digest)
            expected = raw.decode("utf-8", "replace")
            faked = Fetched(
                uri="network://http/127.0.0.1:80/x",
                status=200,
                content_type="text/plain",
                body=raw,
                truncated=False,
                redirects=(),
                elapsed_ms=1,
            )

            for plen in (1, 2, 3, 4, 5):
                if plen >= min_full_plen:
                    # read_page 全程严格前进，拼接结果与整段 decode("utf-8", "replace") 逐字一致
                    parts: list[str] = []
                    cur: int | None = 0
                    while cur is not None:
                        page = stack.contents.read_page(
                            ref,
                            task_id=task.task_id,
                            offset=cur,
                            length=plen,
                            encoding="utf-8-replace",
                        )
                        pdict = page.to_payload()
                        assert 0 < pdict["bytes"] <= plen
                        if page.next_offset is not None:
                            assert page.next_offset > cur
                            assert cur + pdict["bytes"] == page.next_offset
                        parts.append(page.data)
                        cur = page.next_offset
                    assert "".join(parts) == expected, (raw, plen)

                    # summary_page 同样严格前进且拼接一致
                    sparts: list[str] = []
                    scur: int | None = 0
                    while scur is not None:
                        stext, sbytes, snext, _ = summary_page(
                            faked, offset=scur, length=plen
                        )
                        assert 0 < sbytes <= plen
                        if snext is not None:
                            assert snext > scur
                            assert scur + sbytes == snext
                        sparts.append(stext)
                        scur = snext
                    assert "".join(sparts) == expected, (raw, plen)
                else:
                    # plen 小于多字节序列宽度时必须抛错，绝不返回停滞游标或拆成多个 U+FFFD
                    with pytest.raises(ContentError):
                        cur = 0
                        while cur is not None:
                            page = stack.contents.read_page(
                                ref,
                                task_id=task.task_id,
                                offset=cur,
                                length=plen,
                                encoding="utf-8-replace",
                            )
                            if page.next_offset is not None:
                                assert page.next_offset > cur
                            cur = page.next_offset

                    with pytest.raises(NetError) as excinfo:
                        scur = 0
                        while scur is not None:
                            _t, _b, snext, _ = summary_page(
                                faked, offset=scur, length=plen
                            )
                            if snext is not None:
                                assert snext > scur
                            scur = snext
                    assert excinfo.value.reason == "net.limits"

        # 2) 派发端到端：length=1 撞上 3 字节汉字 -> 报 net.limits 失败并结清实际字节
        run_limits = Limits(
            max_turns=6, max_total_tokens=20_000, budget_limits={"bytes": 4096}
        )
        tiny_model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "c_tiny",
                            "network.fetch",
                            {
                                "url": fixture.url("/cjk"),
                                "max_bytes": 1024,
                                "offset": 0,
                                "length": 1,
                            },
                        )
                    ],
                ),
                ModelResponse("end_turn", content="done"),
            ]
        )
        tiny_res = _drive(stack, engine_for(stack, tiny_model, limits=run_limits), task)
        tiny_payload = _tool_results(log, tiny_res.run_id)["c_tiny"]
        assert tiny_payload["status"] == "failed"
        assert tiny_payload["output"]["reason"] == "net.limits"
        assert "ref" not in tiny_payload["output"]
        usage = stack.budget.usage(f"run:{tiny_res.run_id}")
        assert usage["settled"]["bytes"] == len("中文测试".encode())
        assert usage["reserved"].get("bytes", 0) == 0
    finally:
        log.close()
        fixture.stop()
