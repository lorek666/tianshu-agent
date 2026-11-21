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
- **只自动重试「可证明没发出请求」的失败**（2026-11-14，SPEC-G2-NET-RESILIENCE；
  2026-11-15 复审收紧重定向边界）：连接阶段失败（`net.connect` /
  `net.connect_timeout`——**应用层请求字节为零**；TLS 握手字节可能已到对端，
  如实）**且该次尝试未跟随任何重定向**时按 `NetLimits.connect_attempts` 重试。
  已跟随重定向（服务器已见过至少一个 GET）、TLS 之后的一切失败（读断、超时、
  协议错、任何 HTTP 响应含 5xx）**绝不自动重试**——重发是第二个 GET，必须由
  模型重新发起、重新拿批准。这条边界被服务器端连接数测试钉住。另如实记录：
  `net.connect_timeout` 实际几乎不可达——连接超时 = 整个剩余期限，首试烧尽后
  deadline 检查会拒绝第二次尝试（per-attempt 预算切分见 SPEC 未决）。
- **进程级速率下限**：相邻两次 `fetch` 开始时刻至少隔 `min_interval_s`
  （默认 1.0s，`0` 显式关闭；等待分片并随时可取消，等待不计入抓取 deadline）。
- **不做内容判级**：不判断"这是不是私有材料"；审批展示完整 URL，由人判断。

## 诚实边界

- 取消只能阻止**新**请求 / 新读；已经写进内核发送缓冲的 TCP 字节无法撤回。
- **连接期单次 DNS 解析 + Socket IP 钉死**（W9 v2.0）：域名目标在每跳连接前仅执行
  一次 `getaddrinfo`，拦截全部非全局单播地址（`net.private_address_blocked`），
  并直接向校验通过的数值 `sockaddr` 建立 TCP 套接字（保留原 `Host` 头、TLS SNI
  与 X.509 主机名核验），消除检查与连接二次解析的 DNS Rebinding TOCTOU 窗口；
  操作系统 `getaddrinfo` 系统调用本身仍无法被 Python 线程定时器中途异步打断。
- 二进制或超长正文在内联 `summary` 中给有界 UTF-8 预览，完整已读字节落入内容引用，
  可通过 `read_content`（`2.0` 支持 `base64` / `latin-1` / `utf-8-replace`）无损分页读取。
"""

from __future__ import annotations

import codecs
import errno
import http.client
import ipaddress
import math
import os
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

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

#: 连接阶段的总尝试次数（1 = 不重试）。只对「TCP 从未建立」的失败重试，
#: 见模块 docstring 与 SPEC-G2-NET-RESILIENCE §2.3。
NET_CONNECT_ATTEMPTS = 2
#: 相邻两次抓取开始时刻的进程级最小间隔（秒），0 = 关闭。
NET_MIN_INTERVAL_S = 1.0

#: 环境覆盖：非法值回落默认（沿用本仓库 env 容错惯例），不做静默禁用。
ENV_NET_CONNECT_ATTEMPTS = "TIANSHU_NET_CONNECT_ATTEMPTS"
ENV_NET_MIN_INTERVAL_S = "TIANSHU_NET_MIN_INTERVAL_S"

#: 可自动重试的失败分类。**只有这两类**发生在 `conn.connect()` 里、应用层
#: 请求字节为零（TLS 握手字节除外，如实）；且**只有该次尝试未跟随任何重定向**
#: 才真的重试——已消费重定向 = 服务器已见过请求，重发是第二个 GET（见
#: fetch() 与模块 docstring，2026-11-15 复审修复）。
_RETRYABLE_REASONS = frozenset({"net.connect", "net.connect_timeout"})

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


def _env_connect_attempts() -> int:
    raw = os.environ.get(ENV_NET_CONNECT_ATTEMPTS)
    if not raw:
        return NET_CONNECT_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return NET_CONNECT_ATTEMPTS
    return value if value >= 1 else NET_CONNECT_ATTEMPTS


def _env_min_interval_s() -> float:
    raw = os.environ.get(ENV_NET_MIN_INTERVAL_S)
    if not raw:
        return NET_MIN_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        return NET_MIN_INTERVAL_S
    return value if value >= 0 else NET_MIN_INTERVAL_S


@dataclass(frozen=True)
class NetLimits:
    """一次 Run 的网络抓取上限。

    默认值就是模块常量（env 可覆盖默认）；测试与将来的按 Run 配置都通过
    这个对象注入，不在执行器里散落魔法数字。
    """

    timeout_ms: int = NET_TIMEOUT_MS
    max_bytes: int = NET_MAX_BYTES
    max_redirects: int = NET_MAX_REDIRECTS
    summary_bytes: int = NET_SUMMARY_BYTES
    connect_attempts: int = field(default_factory=_env_connect_attempts)
    min_interval_s: float = field(default_factory=_env_min_interval_s)

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
        if (
            not isinstance(self.connect_attempts, int)
            or isinstance(self.connect_attempts, bool)
            or self.connect_attempts < 1
        ):
            raise NetError(
                f"connect_attempts 必须是 >= 1 的整数：{self.connect_attempts!r}",
                reason="net.limits",
            )
        if (
            not isinstance(self.min_interval_s, (int, float))
            or isinstance(self.min_interval_s, bool)
            or not math.isfinite(self.min_interval_s)
            or self.min_interval_s < 0
        ):
            raise NetError(
                f"min_interval_s 必须是 >= 0 的有限数值：{self.min_interval_s!r}",
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
    #: 到这次成功为止的连接尝试次数（1 = 首次即成功；库层事实，不进工具输出）。
    attempts: int = 1

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


def _remaining(
    deadline: float, interrupted: threading.Event | None = None
) -> float:
    if interrupted is not None and interrupted.is_set():
        raise NetError("抓取超时：超过本次抓取的硬上限", reason="net.timeout")
    left = deadline - time.monotonic()
    if left <= 0:
        raise NetError("抓取超时：超过本次抓取的硬上限", reason="net.timeout")
    return left


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise NetCancelled("Run 已取消：不再发新的网络请求")


_BROADCAST_IPV4 = ipaddress.IPv4Address("255.255.255.255")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_SIIT_TRANSLATED = ipaddress.IPv6Network("::ffff:0:0:0/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")

#: Python 3.12 标准库 `is_global` 尚未覆盖或口径偏宽的 IANA 特殊保留非全局地址段。
_SPECIAL_NON_GLOBAL_IPV4: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("192.88.99.0/24"),  # RFC 7526 6to4 Relay Anycast
)
_SPECIAL_NON_GLOBAL_IPV6: tuple[ipaddress.IPv6Network, ...] = (
    _NAT64_WELL_KNOWN,                        # RFC 6052 Well-Known NAT64
    _NAT64_LOCAL,                             # RFC 8215 Local-Use NAT64
    ipaddress.IPv6Network("100::/64"),        # RFC 6666 Discard-Only
    ipaddress.IPv6Network("2001:20::/28"),    # RFC 7343 ORCHIDv2
    ipaddress.IPv6Network("2001:db8::/32"),   # RFC 3849 Documentation
    ipaddress.IPv6Network("3fff::/20"),       # RFC 9637 Documentation
    ipaddress.IPv6Network("5f00::/16"),       # RFC 9602 SRv6 SIDs
    ipaddress.IPv6Network("fec0::/10"),       # RFC 3879 Site-Local
)


@dataclass(frozen=True)
class _PinnedEndpoint:
    """已完成单次解析与安全校验、可直接 `socket.connect` 的数值端点。"""

    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]


def _parse_ip_literal(
    host: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = (
        host[1:-1] if host.startswith("[") and host.endswith("]") else host
    )
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _embedded_ipv4s(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[ipaddress.IPv4Address, ...]:
    if not isinstance(ip, ipaddress.IPv6Address):
        return ()
    embedded: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        embedded.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        embedded.append(ip.sixtofour)
    if ip.teredo is not None:
        embedded.extend(ip.teredo)
    packed = ip.packed
    if ip in _NAT64_WELL_KNOWN or ip in _SIIT_TRANSLATED:
        embedded.append(ipaddress.IPv4Address(packed[12:16]))
    elif ip in _NAT64_LOCAL:
        # RFC 8215 + RFC 6052 §2.2：本地 NAT64 前缀可按 /96、/64、/56、/48 四种字节位嵌入 IPv4。
        candidates = (
            packed[12:16],
            packed[9:13],
            packed[7:8] + packed[9:12],
            packed[6:8] + packed[9:11],
        )
        non_zero = [
            ipaddress.IPv4Address(raw) for raw in candidates if raw != b"\x00\x00\x00\x00"
        ]
        if non_zero:
            embedded.extend(non_zero)
        else:
            embedded.append(ipaddress.IPv4Address("0.0.0.0"))
    elif ip in _IPV4_COMPATIBLE and int(ip) > 1:
        # 已废弃的 `::/96` IPv4 兼容形式（排除 `::` 未指定与 `::1` 回环）。
        embedded.append(ipaddress.IPv4Address(packed[12:16]))
    return tuple(embedded)


def _validate_literal_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, raw_host: str
) -> None:
    """字面量 IP：放行白名单批准的回环/私网，恒拒未指定/多播/站点本地/链路本地元数据。"""
    if "%" in raw_host:
        raise NetError(
            f"禁止连接带作用域标识的链路本地字面量地址：{raw_host}",
            reason="net.private_address_blocked",
        )
    for cand in (ip, *_embedded_ipv4s(ip)):
        if (
            cand.is_unspecified
            or cand.is_multicast
            or cand.is_link_local
            or bool(getattr(cand, "is_site_local", False))
            or cand == _BROADCAST_IPV4
        ):
            raise NetError(
                f"禁止连接非单播或链路本地元数据地址：{raw_host}",
                reason="net.private_address_blocked",
            )


def _is_disallowed_resolved_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or bool(getattr(ip, "is_site_local", False))
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _SPECIAL_NON_GLOBAL_IPV4)
    return any(ip in net for net in _SPECIAL_NON_GLOBAL_IPV6)


def _validate_resolved_ip(
    addr_str: str, *, host: str = ""
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """域名解析结果校验：任意非全局单播或内嵌非全局地址一律 fail-closed。"""
    clean = addr_str.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError as exc:
        raise NetError(
            f"DNS 解析返回非法地址 {addr_str!r}（主机 {host!r}）",
            reason="net.private_address_blocked",
        ) from exc
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        checks: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] = (
            ip.ipv4_mapped,
        )
    else:
        checks = (ip, *_embedded_ipv4s(ip))
    if "%" in addr_str or any(_is_disallowed_resolved_ip(cand) for cand in checks):
        label = f"{host} -> {addr_str}" if host else addr_str
        raise NetError(
            f"域名解析命中非全局/私有地址，拒绝建立连接：{label}",
            reason="net.private_address_blocked",
        )
    return ip


def require_public_target(target: NetworkTarget) -> None:
    """Apply the DNS address policy to explicit IPs in public-only tools.

    Hostnames are checked as part of the single, pinned resolution in `_connect`.
    Legacy `network.fetch` continues to allow explicitly approved private IPs.
    """
    if _parse_ip_literal(target.host) is not None:
        _validate_resolved_ip(target.host.removeprefix("[").removesuffix("]"))


def _resolve_host(host: str, port: int) -> list[tuple[Any, ...]]:
    """可注入的单次主机解析入口（每跳最多调用一次）。"""
    return list(
        socket.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    )


def _resolve_and_pin(
    target: NetworkTarget,
    *,
    resolver: Callable[[str, int], list[tuple[Any, ...]]] | None = None,
) -> tuple[_PinnedEndpoint, ...]:
    """区分字面量 IP 与域名：单次解析、全记录集 fail-closed 校验并产出钉死端点。"""
    port = (
        target.port
        if target.port is not None
        else (443 if target.transport == "https" else 80)
    )
    literal_ip = _parse_ip_literal(target.host)
    if literal_ip is not None:
        _validate_literal_ip(literal_ip, raw_host=target.host)
        if isinstance(literal_ip, ipaddress.IPv4Address):
            return (
                _PinnedEndpoint(
                    socket.AF_INET, socket.SOCK_STREAM, 0, (str(literal_ip), port)
                ),
            )
        return (
            _PinnedEndpoint(
                socket.AF_INET6,
                socket.SOCK_STREAM,
                0,
                (str(literal_ip), port, 0, 0),
            ),
        )

    resolve_fn = _resolve_host if resolver is None else resolver
    try:
        infos = resolve_fn(target.host, port)
    except NetError:
        raise
    except TimeoutError as exc:
        raise NetError(
            f"DNS 解析超时：{target.host}: {exc}",
            reason="net.connect_timeout",
        ) from exc
    except OSError as exc:
        raise NetError(
            f"DNS 解析失败：{target.host}: {type(exc).__name__}: {exc}",
            reason="net.connect",
        ) from exc

    if not infos:
        raise NetError(
            f"DNS 解析未返回任何可用地址：{target.host}",
            reason="net.connect",
        )

    # 先对整个 RRset 全量校验：哪怕排在后面的某条记录是内网/回环，也必须在打开
    # 任何 socket 之前整体拒绝（fail-closed）。
    endpoints: list[_PinnedEndpoint] = []
    for entry in infos:
        if not isinstance(entry, tuple) or len(entry) < 5:
            raise NetError(
                f"DNS 解析记录格式不合法：{entry!r}", reason="net.connect"
            )
        af, socktype, proto, _canonname, sockaddr = entry[:5]
        if (
            not isinstance(sockaddr, tuple)
            or len(sockaddr) < 2
            or not isinstance(sockaddr[0], str)
        ):
            raise NetError(
                f"DNS sockaddr 格式不合法：{sockaddr!r}", reason="net.connect"
            )
        validated_ip = _validate_resolved_ip(sockaddr[0], host=target.host)
        pinned_sockaddr = (str(validated_ip), int(sockaddr[1]), *sockaddr[2:])
        endpoints.append(
            _PinnedEndpoint(
                family=int(af),
                socktype=int(socktype) if socktype else socket.SOCK_STREAM,
                proto=int(proto),
                sockaddr=pinned_sockaddr,
            )
        )
    return tuple(endpoints)


def _connect_pinned_endpoints(
    endpoints: tuple[_PinnedEndpoint, ...],
    timeout: float | None,
    *,
    deadline: float | None = None,
) -> socket.socket:
    if not endpoints:
        raise OSError("没有可用的已校验网络端点")
    last_exc: BaseException | None = None
    for ep in endpoints:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("连接超时：超过本次抓取的硬上限")
            effective_timeout: float | None = (
                left if timeout is None else min(timeout, left)
            )
        else:
            effective_timeout = timeout
        sock: socket.socket | None = None
        try:
            sock = socket.socket(ep.family, ep.socktype, ep.proto)
            if effective_timeout is not None:
                sock.settimeout(effective_timeout)
            sock.connect(ep.sockaddr)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                if exc.errno != errno.ENOPROTOOPT:
                    raise
            return sock
        except BaseException as exc:
            last_exc = exc
            if sock is not None:
                sock.close()
            if not isinstance(exc, OSError):
                raise
    assert last_exc is not None
    raise last_exc


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """直接向已校验数值 `sockaddr` 发起连接，保留原始 `Host` 请求头且绝不二次解析 DNS。"""

    def __init__(
        self,
        host: str,
        port: int | None,
        *,
        endpoints: tuple[_PinnedEndpoint, ...],
        timeout: float,
        deadline: float | None = None,
    ) -> None:
        init_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        super().__init__(init_host, port, timeout=timeout)
        self._endpoints = endpoints
        self._deadline = deadline

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = _connect_pinned_endpoints(
            self._endpoints, self.timeout, deadline=self._deadline
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """直接向已校验数值 `sockaddr` 发起连接，并用原始域名完成 TLS SNI 与 X.509 核验。"""

    def __init__(
        self,
        host: str,
        port: int | None,
        *,
        endpoints: tuple[_PinnedEndpoint, ...],
        timeout: float,
        context: ssl.SSLContext | None = None,
        deadline: float | None = None,
    ) -> None:
        init_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if context is None:
            context = ssl.create_default_context()
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        super().__init__(init_host, port, timeout=timeout, context=context)
        self._endpoints = endpoints
        self._deadline = deadline

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        raw_sock = _connect_pinned_endpoints(
            self._endpoints, self.timeout, deadline=self._deadline
        )
        try:
            if self._tunnel_host:
                self.sock = raw_sock
                self._tunnel()
                server_hostname = self._tunnel_host
            else:
                server_hostname = self.host
            self.sock = self._context.wrap_socket(
                raw_sock, server_hostname=server_hostname
            )
        except BaseException:
            raw_sock.close()
            raise


def _connect(
    target: NetworkTarget,
    timeout_s: float,
    *,
    resolver: Callable[[str, int], list[tuple[Any, ...]]] | None = None,
) -> http.client.HTTPConnection:
    endpoints = _resolve_and_pin(target, resolver=resolver)
    deadline = time.monotonic() + timeout_s
    if target.transport == "https":
        conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
            target.host,
            target.port,
            endpoints=endpoints,
            timeout=timeout_s,
            context=ssl.create_default_context(),
            deadline=deadline,
        )
    else:
        conn = _PinnedHTTPConnection(
            target.host,
            target.port,
            endpoints=endpoints,
            timeout=timeout_s,
            deadline=deadline,
        )
    return conn


def _resolve_location(current: NetworkTarget, location: str) -> NetworkTarget:
    """把 `Location` 解析成目标，并按**同源**规则校验。

    - 绝对 URL（`http(s)://...`）：解析后必须与当前同源；
    - 以 `/` 开头的路径：同源，替换路径；
    - 其他相对引用：按 RFC 3986 合并，空路径保留当前路径；
    - fragment 丢弃（它不会发给服务器），query 原样保留。
    """
    location = location.strip()
    if not location:
        raise NetError("重定向的 Location 为空", reason="net.redirect_invalid")
    base = f"{current.transport}://{urlsplit(current.origin_uri).path.lstrip('/')}"
    base += current.path + (f"?{current.query}" if current.query else "")
    try:
        split = urlsplit(urljoin(base, location))
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
        target = NetworkTarget.parse(
            f"network://{split.scheme}/{split.netloc}{split.path or '/'}"
            + (f"?{split.query}" if split.query else "")
        )
    except (ResourceError, ValueError) as exc:
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


#: 进程级速率状态：相邻两次 `fetch` 开始时刻的间隔由 `_wait_for_rate_slot`
#: 保证。礼貌是进程属性——两个 Run 的抓取互相计入对方的间隔（SPEC §6）。
_FETCH_STATE_LOCK = threading.Lock()
_LAST_FETCH_START: float | None = None


def _wait_for_rate_slot(
    clock: Callable[[], float],
    last_started: float | None,
    min_interval_s: float,
    cancel: Callable[[], bool] | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """距上次抓取开始不足 `min_interval_s` 就分片等待（注入时钟，测试确定性）。

    - `min_interval_s <= 0` 或首次抓取（`last_started is None`）不等待；
    - 等待分片上限 50ms，每片前查取消——等待中取消立即 `NetCancelled`；
    - 等待不计入抓取 deadline（deadline 从真正开始连接起算，由调用方处理）。
    """
    if min_interval_s <= 0 or last_started is None:
        return
    while True:
        _check_cancel(cancel)
        elapsed = clock() - last_started
        if elapsed >= min_interval_s:
            return
        sleep(min(min_interval_s - elapsed, 0.05))


def fetch(
    target: NetworkTarget,
    *,
    limits: NetLimits | None = None,
    cancel: Callable[[], bool] | None = None,
    resolver: Callable[[str, int], list[tuple[Any, ...]]] | None = None,
) -> Fetched:
    """GET 一个网络目标。阻塞式；所有失败都以 `NetError` / `NetCancelled` 抛出。

    连接阶段失败（`net.connect` / `net.connect_timeout`）且该次尝试**未跟随
    任何重定向**时按 `NetLimits.connect_attempts` 自动重试——应用层请求字节
    为零（TLS 握手字节除外），重发不构成第二次外发请求；已跟随重定向后
    （服务器已见过至少一个 GET）或其余失败原样抛出——重试必须重新批准。
    重试共享同一个绝对期限，每次尝试前查取消。
    """
    global _LAST_FETCH_START
    limits = limits or NetLimits()
    with _FETCH_STATE_LOCK:
        _wait_for_rate_slot(
            time.monotonic, _LAST_FETCH_START, limits.min_interval_s, cancel
        )
        started = time.monotonic()
        _LAST_FETCH_START = started
    deadline = started + limits.timeout_ms / 1000.0
    # visited 跨尝试保留：重定向防环不因重试而重置。
    visited: set[str] = {target.to_uri()}
    # 复审修复（2026-11-15）：跨整个 fetch 的「本链已跟随重定向」记录——
    # 只要任何一跳已把 GET 发给服务器，就绝不自动重试（重发 = 第二个 GET，
    # 无重新批准）。否则「重定向后第二跳连接失败」会被误重试成第二个 GET，
    # 且因 visited 保留被误报成 redirect_loop。
    redirects_followed: list[str] = []
    # 最后一次连接阶段失败的原始错误：deadline 烧尽时重抛它，保住如实分类。
    last_connect_error: NetError | None = None
    for attempt in range(1, limits.connect_attempts + 1):
        if attempt > 1:
            _check_cancel(cancel)
            # deadline 已烧尽就不进下一次尝试：否则 _fetch_once 开头的
            # _remaining(deadline) 会抛 net.timeout，把「连接阶段失败、
            # 0 字节已知」的如实分类顶成「连接建立后的失败、用量未知」——
            # 失败分类与字节账本口径一起错档。deadline 是总预算，用尽即
            # 尝试用尽，最终失败 = 最后一次真实尝试的原始分类。
            assert last_connect_error is not None  # pragma: no cover - 结构保证
            if deadline - time.monotonic() <= 0:
                raise last_connect_error
        try:
            return _fetch_once(
                target,
                limits,
                cancel,
                deadline,
                visited,
                attempt,
                started,
                redirects_followed,
                resolver=resolver,
            )
        except NetError as exc:
            if (
                exc.reason not in _RETRYABLE_REASONS
                or attempt >= limits.connect_attempts
                or redirects_followed
            ):
                raise
            last_connect_error = exc
    raise NetError("重定向次数超过上限", reason="net.redirect_limit")  # pragma: no cover


def _fetch_once(
    target: NetworkTarget,
    limits: NetLimits,
    cancel: Callable[[], bool] | None,
    deadline: float,
    visited: set[str],
    attempt: int,
    fetch_started: float,
    redirects_followed: list[str],
    *,
    resolver: Callable[[str, int], list[tuple[Any, ...]]] | None = None,
) -> Fetched:
    """单次尝试：完整的重定向跟随 + 有界读取。失败以 `NetError` / `NetCancelled` 抛出。"""
    current = target
    redirects: list[str] = []
    # deadline 是整次 fetch 共享的绝对期限，中断标记必须跨重定向跳保留：
    # 若第 0 跳在解析 Location 或 finally 收尾时已触发 Timer，不得在进入
    # 第 1 跳时被重置为 False 而多发未授权的下一跳请求或误报 connect_timeout。
    deadline_interrupted = threading.Event()

    for hop in range(limits.max_redirects + 1):
        _check_cancel(cancel)
        remaining = _remaining(deadline, deadline_interrupted)
        conn = (
            _connect(current, remaining)
            if resolver is None
            else _connect(current, remaining, resolver=resolver)
        )
        timer: threading.Timer | None = None
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
            # HTTPResponse.read/readline 内部可能持续多次 recv；socket 的空闲
            # 超时无法约束滴流。保留实际 socket（getresponse 后 conn.sock 可能
            # 已移交给 response），在同一绝对期限到达时中断头/正文的阻塞读。
            sock = conn.sock
            remaining = _remaining(deadline, deadline_interrupted)

            def interrupt_read(
                sock: socket.socket | None = sock,
                interrupted: threading.Event = deadline_interrupted,
            ) -> None:
                interrupted.set()
                if sock is not None:
                    try:
                        # 只打断底层 fd；不在另一线程改 SSLSocket._sslobj。
                        socket.socket.shutdown(sock, socket.SHUT_RDWR)
                    except OSError:
                        pass

            timer = threading.Timer(remaining, interrupt_read)
            timer.daemon = True
            timer.start()
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
            _remaining(deadline, deadline_interrupted)
            response = conn.getresponse()
            _remaining(deadline, deadline_interrupted)
            status = response.status
            content_type = response.getheader("Content-Type", "") or ""
            # ``http.client`` normally raises IncompleteRead when a known-length body
            # ends early, but that behaviour depends on read size and EOF timing.
            # Capture the normalized remaining length so bounded reads cannot turn a
            # short response into a successful fetch on another Python/platform.
            expected_body_length = getattr(response, "length", None)

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
                # 每消费一跳就登记：这一链的 GET 已经发给服务器了。
                redirects_followed.append(nxt.to_uri())
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
                remaining = _remaining(deadline, deadline_interrupted)
                if conn.sock is not None:
                    conn.sock.settimeout(remaining)
                want = min(NET_CHUNK, limits.max_bytes - len(body) + 1)
                if want <= 0:
                    truncated = True
                    break
                chunk = response.read(want)
                _remaining(deadline, deadline_interrupted)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limits.max_bytes:
                    del body[limits.max_bytes :]
                    truncated = True
                    break
            if (
                expected_body_length is not None
                and not truncated
                and len(body) != expected_body_length
            ):
                raise NetError(
                    "HTTP 正文不完整：实际读取 "
                    f"{len(body)} 字节，Content-Length 声明 {expected_body_length} 字节",
                    reason="net.read",
                )
            elapsed_ms = int((time.monotonic() - fetch_started) * 1000)
            return Fetched(
                uri=current.to_uri(),
                status=status,
                content_type=content_type,
                body=bytes(body),
                truncated=truncated,
                redirects=tuple(redirects),
                elapsed_ms=elapsed_ms,
                attempts=attempt,
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
            if deadline_interrupted.is_set():
                raise NetError(
                    f"抓取超时（{limits.timeout_ms}ms 上限）：{exc}",
                    reason="net.timeout",
                ) from exc
            raise NetError(f"TLS 失败：{exc}", reason="net.tls") from exc
        except OSError as exc:
            if deadline_interrupted.is_set():
                raise NetError(
                    f"抓取超时（{limits.timeout_ms}ms 上限）：{exc}",
                    reason="net.timeout",
                ) from exc
            _remaining(deadline)
            # 连接已经建立之后的 IO 失败：可能已经读到了一部分字节，用量未知。
            raise NetError(
                f"读取失败：{type(exc).__name__}: {exc}",
                reason="net.read",
            ) from exc
        except http.client.HTTPException as exc:
            if deadline_interrupted.is_set():
                raise NetError(
                    f"抓取超时（{limits.timeout_ms}ms 上限）：{exc}",
                    reason="net.timeout",
                ) from exc
            _remaining(deadline)
            raise NetError(
                f"HTTP 协议错误：{type(exc).__name__}: {exc}",
                reason="net.protocol",
            ) from exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            conn.close()
    # 循环只会因为 continue 走完 max_redirects+1 轮，未在上面返回就是越界。
    raise NetError("重定向次数超过上限", reason="net.redirect_limit")


def _first_utf8_unit(lookahead: bytes) -> tuple[str, int]:
    """返回起始处首个 UTF-8 原子单元 `(char, width)`（遵守 Unicode §3.9 maximal subpart）。"""
    try:
        decoded = lookahead.decode("utf-8", "strict")
        ch = decoded[0]
        return ch, len(ch.encode("utf-8"))
    except UnicodeDecodeError as exc:
        if exc.start > 0:
            ch = lookahead[: exc.start].decode("utf-8", "strict")[0]
            return ch, len(ch.encode("utf-8"))
        return "\ufffd", exc.end


def summary_page(
    fetched: Fetched,
    *,
    offset: int = 0,
    length: int = NET_SUMMARY_BYTES,
) -> tuple[str, int, int | None, bool]:
    """计算已抓取字节的内联预览页 `(summary, page_bytes, next_offset, truncated)`。

    - 文本按 UTF-8 字符边界安全回退，绝不切碎多字节字符或 maximal subpart；
    - `page_bytes` 为本页覆盖的底层原始字节长度；
    - 当 `offset + page_bytes < len(fetched.body)` 时返回严格前进的 `next_offset > offset`，
      供 `read_content` 无网续读；若 `length < 4` 不足以容纳单个多字节 UTF-8 序列则抛 `NetError`。
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise NetError(f"offset 必须是非负整数：{offset!r}", reason="net.limits")
    if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
        raise NetError(f"摘要上限必须是正整数：{length!r}", reason="net.limits")
    body = fetched.body
    total = len(body)
    if offset >= total:
        return "", 0, None, bool(fetched.truncated or total > 0)
    window = body[offset : offset + length]
    final = offset + len(window) >= total
    text, page_bytes = codecs.utf_8_decode(window, "replace", final)
    if len(window) > 0 and page_bytes <= 0:
        unit_char, unit_width = _first_utf8_unit(body[offset : offset + 4])
        if unit_width > length:
            raise NetError(
                f"内联摘要 length={length} 过小，不足以容纳单个多字节 UTF-8 序列"
                f"（offset={offset}，需 >= {unit_width}；"
                "请设 length >= 4 或改用 read_content base64）",
                reason="net.limits",
            )
        text = unit_char
        page_bytes = unit_width
    next_offset: int | None = (
        None if offset + page_bytes >= total else offset + page_bytes
    )
    clipped = bool(fetched.truncated or page_bytes < total)
    return text, page_bytes, next_offset, clipped


def summary_text(
    fetched: Fetched, *, limit_bytes: int = NET_SUMMARY_BYTES
) -> tuple[str, bool]:
    """正文的有界摘要（有损 UTF-8）。

    返回 `(文本, 是否被截断)`。二进制内容也走这条路：`errors="replace"` 之后
    看起来是乱码，但**原始字节**在内容引用里，这是摘要不是原文。
    """
    text, _page_bytes, _next_offset, clipped = summary_page(
        fetched, offset=0, length=limit_bytes
    )
    return text, clipped


__all__ = [
    "ENV_NET_CONNECT_ATTEMPTS",
    "ENV_NET_MIN_INTERVAL_S",
    "NET_CHUNK",
    "NET_CONNECT_ATTEMPTS",
    "NET_MAX_BYTES",
    "NET_MAX_REDIRECTS",
    "NET_MIN_INTERVAL_S",
    "NET_SUMMARY_BYTES",
    "NET_TIMEOUT_MS",
    "NET_USER_AGENT",
    "Fetched",
    "NetCancelled",
    "NetError",
    "NetLimits",
    "canonical_target",
    "fetch",
    "summary_page",
    "summary_text",
]
