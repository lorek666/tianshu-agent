"""评测用的本机 web 夹具（loopback）：给 §10.5「联网研究交付」一个**确定性**的来源。

为什么评测也要有自己的服务器：

- PRD §10.5 要求「断网 / 无搜索配置诚实失败」「保存的来源可离线核验」。这两条
  只能用**确定存在**的本机来源来证明：公网站点既不确定、又要求 CI 能出网，还会
  把别人的服务器写进验收条件；
- 它必须和 `tests/httpserver.py` 分开：测试夹具要注入故障（延迟 / 半截响应），
  评测夹具只要**固定路由 + 命中记录**。混成一个，评测器就会开始依赖测试的故障
  注入参数——那是把"验收集"和"单元测试"搅在一起。

不引第三方库，一个进程一个线程，端口由内核分配（`127.0.0.1:0`）。
`stop()` 之后端口立刻不可连——「来源可离线核验」就是在这个时刻读内容引用的。
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True)
class WebRoute:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class _Handler(BaseHTTPRequestHandler):
    server_version = "tianshu-eval-fixture"

    def log_message(self, *_args: object) -> None:  # noqa: D401 - 继承接口
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        fixture: WebFixture = self.server.fixture  # type: ignore[attr-defined]
        fixture.hits.append(self.path)
        route = fixture.routes.get(self.path) or WebRoute(
            status=404, body=b"no such route"
        )
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        self.send_header("Content-Length", str(len(route.body)))
        for key, value in route.headers:
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(route.body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端先断
            pass


class WebFixture:
    """一个 loopback HTTP 服务：固定路由 + 命中记录。"""

    def __init__(self) -> None:
        self.routes: dict[str, WebRoute] = {}
        self.hits: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 配置

    def route(
        self,
        path: str,
        *,
        body: bytes | str = b"",
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> WebFixture:
        if not path.startswith("/"):
            path = f"/{path}"
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[path] = WebRoute(
            status=status,
            body=body,
            content_type=content_type,
            headers=tuple((headers or {}).items()),
        )
        return self

    # ------------------------------------------------------------ 生命周期

    def start(self) -> WebFixture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.fixture = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="eval-web-fixture", daemon=True
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

    def __enter__(self) -> WebFixture:
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
        """`127.0.0.1:<port>`：事件与任务文本里出现的人话地址。"""
        return f"127.0.0.1:{self.port}"

    @property
    def origin(self) -> str:
        """任务级白名单要的**源**（`outbound_allow` 的一项）。"""
        return f"network://http/{self.base}"

    def url(self, path: str) -> str:
        """工具参数要的规范网络资源引用。"""
        if not path.startswith("/"):
            path = f"/{path}"
        return f"network://http/{self.base}{path}"

    @property
    def hit_count(self) -> int:
        return len(self.hits)


def closed_origin() -> tuple[str, socket.socket]:
    """一个**已 bind 未 listen** 的本机端口：连上去必被拒（ECONNREFUSED）。

    给「断网 / 无来源」任务用：地址是真实的、端口确实属于本进程，只是没有人
    accept。比"随便挑一个端口"确定，也不会把别人的服务写进验收条件。
    调用方负责 `sock.close()`。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return f"127.0.0.1:{int(sock.getsockname()[1])}", sock


__all__ = ["WebFixture", "WebRoute", "closed_origin"]