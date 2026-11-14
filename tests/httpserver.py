"""本机 HTTP 夹具：给网络执行器的判据用，**不碰外网**。

为什么需要它：网络相关的判据要能确定性复现"抓到了什么""重定向到哪""超时"
"一个包都没发"这些事实。用公网站点既不确定、又要求 CI 能出网，还会把别人的
服务器写进判据。这里用标准库 `http.server` 起一个 loopback 上的临时端口服务，
按路径给固定响应，并**记录每一次命中**——"取消/额度耗尽后没有新请求"这种判据
只能靠命中数证明，不能靠读代码。

不引第三方库；一个进程一个线程；`stop()` 之后端口立刻不可连。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Reply:
    """一条固定响应。`delay_ms` 在**发出响应头之前**睡，用来造超时。"""

    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    #: 发出响应头**之前**睡多久（造"等不到头"的超时）。
    delay_ms: int = 0
    #: 发出响应头**之后**、写正文之前睡多久（造"读到一半取消"的窗口）。
    body_delay_ms: int = 0


class _Handler(BaseHTTPRequestHandler):
    server_version = "tianshu-test-fixture"
    # 关掉 BaseHTTPRequestHandler 的默认日志：判据失败时输出只该有 pytest 的。
    def log_message(self, *_args: object) -> None:  # noqa: D401 - 继承接口
        return

    def _serve(self, method: str) -> None:
        fixture: NetFixture = self.server.fixture  # type: ignore[attr-defined]
        path = self.path
        fixture.hits.append((method, path))
        if method == "POST":
            # 请求体全量记录：外发内容断言（"到底把什么发给了端点"）靠它。
            length = int(self.headers.get("Content-Length") or 0)
            fixture.bodies.append(self.rfile.read(length) if length else b"")
        reply = fixture.reply_for(path)
        if reply is None:
            reply = Reply(status=404, body=b"no such fixture route")
        if reply.delay_ms:
            time.sleep(reply.delay_ms / 1000.0)
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        for key, value in reply.headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            if reply.body_delay_ms:
                time.sleep(reply.body_delay_ms / 1000.0)
            self.wfile.write(reply.body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端取消
            pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        self._serve("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        self._serve("POST")


class NetFixture:
    """一个 loopback HTTP 服务 + 命中记录。"""

    def __init__(self) -> None:
        self.routes: dict[str, Reply] = {}
        self.hits: list[tuple[str, str]] = []
        # POST 请求体（与 hits 同序；GET 请求不记录）。
        self.bodies: list[bytes] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 配置

    def route(
        self,
        path: str,
        *,
        status: int = 200,
        body: bytes | str = b"",
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
        delay_ms: int = 0,
        body_delay_ms: int = 0,
    ) -> NetFixture:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[path] = Reply(
            status=status,
            body=body,
            content_type=content_type,
            headers=dict(headers or {}),
            delay_ms=delay_ms,
            body_delay_ms=body_delay_ms,
        )
        return self

    def redirect(self, path: str, location: str, *, status: int = 302) -> NetFixture:
        return self.route(path, status=status, headers={"Location": location})

    def reply_for(self, path: str) -> Reply | None:
        return self.routes.get(path)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> NetFixture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.fixture = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="net-fixture", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def __enter__(self) -> NetFixture:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------ 地址

    @property
    def port(self) -> int:
        assert self._server is not None, "夹具没启动"
        return int(self._server.server_address[1])

    @property
    def base(self) -> str:
        """`127.0.0.1:<port>`：事件与脚本里用的人话地址。"""
        return f"127.0.0.1:{self.port}"

    @property
    def origin_uri(self) -> str:
        """任务级白名单要的**源**（`outbound_allow` 的一项）。"""
        return f"network://http/{self.base}"

    def url(self, path: str) -> str:
        """工具参数要的规范网络资源引用。"""
        if not path.startswith("/"):
            path = f"/{path}"
        return f"network://http/{self.base}{path}"

    # ------------------------------------------------------------ 命中

    @property
    def hit_count(self) -> int:
        return len(self.hits)

    def paths(self) -> list[str]:
        return [path for _method, path in self.hits]


def closed_port() -> tuple[int, socket.socket]:
    """一个**已 bind 未 listen** 的端口：连上去会立刻被拒（ECONNREFUSED）。

    比"随便挑一个端口"确定：端口确实存在、属于本进程，只是没有人 accept。
    调用方负责 `sock.close()`。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return int(sock.getsockname()[1]), sock


__all__ = ["NetFixture", "Reply", "closed_port"]
