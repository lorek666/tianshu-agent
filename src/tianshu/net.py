"""网络抓取执行器（PRD §5.2.3 的执行器 / §11.1 G2 的网络阶段）。

## 这一层只做一件事：把「一个已批准的网络目标」变成「字节 + 事实」

授权、审批、预算、事件、内容引用都在派发层（`dispatcher.py`）。这里只负责：

1. 把 `network://<transport>/<host>[:port]/<path>` 变成一个真实的 HTTP GET；
2. 按**执行侧规则**收口：重定向只许同源、响应有字节上限、整次抓取有硬超时、
   每一跳/每一块读之前都能被取消；
3. 如实返回事实：状态码、内容类型、已经读到的字节、是否被截断、跳转链、耗时。
   失败（DNS / 连接被拒 / TLS / 超时）抛 `NetError`，由派发层记成**失败**而不是
   「没有内容」。

## 刻意不做的事（每一条都是"外发面"的收口）

- **不读代理环境变量**：`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 一律不参与。
  读它们等于让"批准的主机"之外多出一个实际收包方，而审批摘要里看不到它。
- **不发 Cookie、不带凭据**：没有 cookie jar，没有 `Authorization`，没有从环境/
  文件读的任何凭据。请求里只有目标 URL 与固定头——这是「私有内容未经许可不发包」
  在外发面上可被核对的事实（但**不是**"已证明没有任何泄露通道"，见文档）。
- **不跟随跨源重定向**：批准的是"这个源上的这个 URL"，靠 302 把包发到另一个源
  等于绕过批准。同源换路径允许（仍在同一台服务器上），跨源/换端口/换 transport
  一律拒绝；需要跨源时让模型重新发起动作、重新拿批准。
- **不信任 `Content-Length`**：只数实际读到的字节。声明可以是假的，读到的不能。
- **不自动重试**：一次失败就是失败。重试策略不在本层，也不在 v1 的网络阶段。
- **不做内容判级**：不判断"这是不是私有材料"；审批展示完整 URL，由人判断。

## 诚实边界

- 取消只能阻止**新**请求 / 新读；已经写进内核发送缓冲的 TCP 字节无法撤回。
- 没有 DNS rebinding 防护、没有"解析后二次校验"：白名单显式声明源，解析到私有
  地址也会照发。这是登记过的缺口，不假装已经解决。
- 二进制正文只给有损摘要（UTF-8 `errors="replace"`），原始字节原样进内容引用。
"""

from __future__ import annotations

import http.client
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import TianshuError
from .resources import NetworkTarget, ResourceError, parse_resource

#: 整次抓取（连接 + 读）的硬上限。
NET_TIMEOUT_MS = 10_000
#: 单次抓取最多读多少字节（超出部分丢弃并标 truncated）。
NET_MAX_BYTES = 1_048_576
#: 最多跟随几跳重定向。
NET_MAX_REDIRECTS = 3
#: 返回给模型的有界摘要上限。
NET_SUMMARY_BYTES = 4_096
#: 每次从 socket 读多少（同时是取消检查的粒度）。
NET_CHUNK = 65_536
#: 固定 User-Agent：不随任务/内容变化，避免把它当成一条外发通道。
NET_USER_AGENT = "tianshu-agent/1 net-fetch"

#: 需要跟随的重定向状态码。304 不是重定向（没有 Location 也合法）。
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

#: 允许"当作文本给出有界摘要"的内容类型前缀。其余按二进制给元数据。
_TEXT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/ld+json",
    "application/javascript",
    "application/x-ndjson",
)


class NetError(TianshuError):
    """平台级失败：DNS / 连接 / TLS / 超时 / 协议 / 重定向越界。

    `reason` 是给事件与用户看的稳定分类（`net.timeout` 等），不是异常类名。
    """

    def __init__(self, message: str, *, reason: str = "net.error") -> None:
        super().__init__(message)
        self.reason = reason


class NetCancelled(TianshuError):
    """抓取过程中发现 Run 已被取消。已经发出的请求无法撤回，但不再发新请求。"""


@dataclass(frozen=True)
class NetLimits:
    """一次 Run 的网络抓取上限。

    默认值就是模块常量；测试与将来的按 Run 配置都通过这个对象注入，
    不在执行器里散落魔法数字。
    """

    timeout_ms: int = NET_TIMEOUT_MS
    max_bytes: int = NET_MAX_BYTES
    max_redirects: int = NET_MAX_REDIRECTS
    summary_bytes: int = NET_SUMMARY_BYTES

    def __post_init__(self) -> None:
        for name, value in (
            ("timeout_ms", self.timeout_ms),
            ("max_bytes", self.max_bytes),
            ("summary_bytes", self.summary_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise NetError(f"{name} 必须是正整数：{value!r}", reason="net.limits")
        if not isinstance(self.max_redirects, int) or self.max_redirects < 0:
            raise NetError(
                f"max_redirects 必须是非负整数：{self.max_redirects!r}",
                reason="net.limits",
            )


@dataclass(frozen=True)
class Fetched:
    """一次成功抓取的事实。任何一项都不许用"猜"填。"""

    uri: str
    status: int
    content_type: str
    body: bytes
    truncated: bool
    redirects: tuple[str, ...]
    elapsed_ms: int

    @property
    def is_text(self) -> bool:
        return self.content_type.lower().startswith(_TEXT_TYPE_PREFIXES)


def canonical_target(raw: object) -> NetworkTarget:
    """工具参数 → 目标。**只接受** `network://` 规范形。

    不接受 `https://...`：一个目标只能有一种写法，否则同一目标会因为两种输入
    得到两个 `args_hash` / 两份审批摘要。错误信息里给出规范写法，让模型能自己改。
    """
    if isinstance(raw, str) and raw.startswith(("http://", "https://")):
        scheme = "https" if raw.startswith("https://") else "http"
        raise NetError(
            f"网络工具的参数必须是 network:// 资源引用，不是 {scheme}:// URL"
            f"（例如 https://example.com/a 要写成 network://https/example.com/a）："
            "一个目标只有一种写法，否则同一个目标会得到两份审批摘要",
            reason="net.arg_not_canonical",
        )
    try:
        ref = parse_resource(raw)
    except ResourceError as exc:
        raise NetError(
            f"网络目标不是合法资源引用：{exc}", reason="net.arg_invalid"
        ) from exc
    if ref.kind != "network":
        raise NetError(
            f"网络工具的 url 必须是 network:// 资源（收到 {ref.kind}）：{raw!r}",
            reason="net.arg_invalid",
        )
    return ref.network_target


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise NetError("抓取超时：超过本次抓取的硬上限", reason="net.timeout")
    return left


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise NetCancelled("Run 已取消：不再发新的网络请求")


def _connect(target: NetworkTarget, timeout_s: float) -> http.client.HTTPConnection:
    if target.transport == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            target.host,
            target.port,
            timeout=timeout_s,
            context=ssl.create_default_context(),
        )
    else:
        conn = http.client.HTTPConnection(
            target.host, target.port, timeout=timeout_s
        )
    return conn


def _resolve_location(current: NetworkTarget, location: str) -> NetworkTarget:
    """把 `Location` 解析成目标，并按**同源**规则校验。

    - 绝对 URL（`http(s)://...`）：解析后必须与当前同源；
    - 以 `/` 开头的路径：同源，替换路径；
    - 其他相对路径：按当前路径的目录解析（RFC 3986 的简化版）；
    - fragment 丢弃（它不会发给服务器），query 原样保留。
    """
    location = location.strip()
    if not location:
        raise NetError("重定向的 Location 为空", reason="net.redirect_invalid")
    split = urlsplit(location)
    if split.scheme:
        if split.scheme not in ("http", "https"):
            raise NetError(
                f"重定向到不支持的 scheme：{split.scheme!r}",
                reason="net.redirect_invalid",
            )
        if split.username or split.password:
            raise NetError(
                "重定向目标带 userinfo（凭据混淆），拒绝",
                reason="net.redirect_invalid",
            )
        authority = split.netloc
        try:
            target = NetworkTarget.parse(
                f"network://{split.scheme}/{authority}{split.path or '/'}"
                + (f"?{split.query}" if split.query else "")
            )
        except ResourceError as exc:
            raise NetError(
                f"重定向目标不合法：{exc}", reason="net.redirect_invalid"
            ) from exc
    else:
        path = split.path
        if not path.startswith("/"):
            # 相对路径：拼到当前路径的目录上（`/a/b` + `c` → `/a/c`）。
            base = current.path.rsplit("/", 1)[0] or "/"
            path = f"{base}/{path}"
        try:
            target = NetworkTarget.parse(
                current.origin_uri + path
                + (f"?{split.query}" if split.query else "")
            )
        except ResourceError as exc:
            raise NetError(
                f"重定向目标不合法：{exc}", reason="net.redirect_invalid"
            ) from exc
    if (target.transport, target.host, target.port) != (
        current.transport,
        current.host,
        current.port,
    ):
        raise NetError(
            f"重定向跨源，拒绝跟随：{current.origin_uri} → {target.origin_uri}"
            "（批准的是一个源上的一个 URL；跨源要重新发起动作并重新批准）",
            reason="net.redirect_origin_denied",
        )
    return target


def fetch(
    target: NetworkTarget,
    *,
    limits: NetLimits | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Fetched:
    """GET 一个网络目标。阻塞式；所有失败都以 `NetError` / `NetCancelled` 抛出。"""
    limits = limits or NetLimits()
    started = time.monotonic()
    deadline = started + limits.timeout_ms / 1000.0
    current = target
    redirects: list[str] = []
    visited: set[str] = {current.to_uri()}

    for hop in range(limits.max_redirects + 1):
        _check_cancel(cancel)
        remaining = _remaining(deadline)
        conn = _connect(current, remaining)
        try:
            # 显式连接：把"连不上 / TLS 握手失败"（确定一个请求字节都没发）与
            # "读到一半断了"（用量未知）分开。账本结算要靠这个区分。
            try:
                conn.connect()
            except TimeoutError as exc:
                raise NetError(
                    f"连接超时（{limits.timeout_ms}ms 上限）：{exc}",
                    reason="net.connect_timeout",
                ) from exc
            except ssl.SSLError as exc:
                raise NetError(f"TLS 失败：{exc}", reason="net.tls") from exc
            except OSError as exc:
                raise NetError(
                    f"网络不可达：{type(exc).__name__}: {exc}",
                    reason="net.connect",
                ) from exc
            path = current.path + (f"?{current.query}" if current.query else "")
            conn.request(
                "GET",
                path,
                headers={
                    "User-Agent": NET_USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "") or ""

            if status in _REDIRECT_CODES:
                location = response.getheader("Location")
                if not location:
                    raise NetError(
                        f"HTTP {status} 重定向缺少 Location",
                        reason="net.redirect_invalid",
                    )
                # 重定向正文不会作为结果返回，不能在 max_bytes 之外无界下载。
                # 每跳都用独立连接；finally 会关闭当前连接，无需 drain 正文。
                nxt = _resolve_location(current, location)
                if nxt.to_uri() in visited:
                    raise NetError(
                        f"重定向成环：{nxt.to_uri()}", reason="net.redirect_loop"
                    )
                visited.add(nxt.to_uri())
                redirects.append(nxt.to_uri())
                current = nxt
                if hop == limits.max_redirects:
                    raise NetError(
                        f"重定向次数超过上限（{limits.max_redirects}）",
                        reason="net.redirect_limit",
                    )
                continue

            body = bytearray()
            truncated = False
            while True:
                _check_cancel(cancel)
                remaining = _remaining(deadline)
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                want = min(NET_CHUNK, limits.max_bytes - len(body) + 1)
                if want <= 0:
                    truncated = True
                    break
                chunk = response.read(want)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limits.max_bytes:
                    del body[limits.max_bytes :]
                    truncated = True
                    break
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return Fetched(
                uri=current.to_uri(),
                status=status,
                content_type=content_type,
                body=bytes(body),
                truncated=truncated,
                redirects=tuple(redirects),
                elapsed_ms=elapsed_ms,
            )
        except NetCancelled:
            raise
        except NetError:
            raise
        except TimeoutError as exc:
            raise NetError(
                f"抓取超时（{limits.timeout_ms}ms 上限）：{exc}",
                reason="net.timeout",
            ) from exc
        except ssl.SSLError as exc:
            raise NetError(f"TLS 失败：{exc}", reason="net.tls") from exc
        except OSError as exc:
            # 连接已经建立之后的 IO 失败：可能已经读到了一部分字节，用量未知。
            raise NetError(
                f"读取失败：{type(exc).__name__}: {exc}",
                reason="net.read",
            ) from exc
        except http.client.HTTPException as exc:
            raise NetError(
                f"HTTP 协议错误：{type(exc).__name__}: {exc}",
                reason="net.protocol",
            ) from exc
        finally:
            conn.close()
    # 循环只会因为 continue 走完 max_redirects+1 轮，未在上面返回就是越界。
    raise NetError("重定向次数超过上限", reason="net.redirect_limit")


def summary_text(
    fetched: Fetched, *, limit_bytes: int = NET_SUMMARY_BYTES
) -> tuple[str, bool]:
    """正文的有界摘要（有损 UTF-8）。

    返回 `(文本, 是否被截断)`。二进制内容也走这条路：`errors="replace"` 之后
    看起来是乱码，但**原始字节**在内容引用里，这是摘要不是原文。
    """
    if limit_bytes <= 0:
        raise NetError(f"摘要上限必须是正整数：{limit_bytes!r}", reason="net.limits")
    text = fetched.body.decode("utf-8", "replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text, fetched.truncated
    clipped = encoded[:limit_bytes]
    # 不要切在多字节字符中间：退到最近的 UTF-8 起始字节。
    while clipped and (clipped[-1] & 0xC0) == 0x80:
        clipped = clipped[:-1]
    return clipped.decode("utf-8", "ignore"), True


__all__ = [
    "NET_CHUNK",
    "NET_MAX_BYTES",
    "NET_MAX_REDIRECTS",
    "NET_SUMMARY_BYTES",
    "NET_TIMEOUT_MS",
    "NET_USER_AGENT",
    "Fetched",
    "NetCancelled",
    "NetError",
    "NetLimits",
    "canonical_target",
    "fetch",
    "summary_text",
]
