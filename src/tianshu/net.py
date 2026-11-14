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
- **只自动重试「可证明没发出去」的失败**（2026-11-14，SPEC-G2-NET-RESILIENCE）：
  连接阶段失败（`net.connect` / `net.connect_timeout`——TCP 从未建立，零字节
  到达对端）按 `NetLimits.connect_attempts` 重试；TLS 之后的一切失败（读断、
  超时、协议错、任何 HTTP 响应含 5xx）**绝不自动重试**——服务器已经见过这次
  请求，重发是第二个 GET，必须由模型重新发起、重新拿批准。这条边界被
  服务器端命中数测试钉住。
- **进程级速率下限**：相邻两次 `fetch` 开始时刻至少隔 `min_interval_s`
  （默认 1.0s，`0` 显式关闭；等待分片并随时可取消，等待不计入抓取 deadline）。
- **不做内容判级**：不判断"这是不是私有材料"；审批展示完整 URL，由人判断。

## 诚实边界

- 取消只能阻止**新**请求 / 新读；已经写进内核发送缓冲的 TCP 字节无法撤回。
- 没有 DNS rebinding 防护、没有"解析后二次校验"：白名单显式声明源，解析到私有
  地址也会照发。这是登记过的缺口，不假装已经解决。
- 二进制正文只给有损摘要（UTF-8 `errors="replace"`），原始字节原样进内容引用。
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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

#: 可自动重试的失败分类。**只有这两类**发生在 `conn.connect()` 里、TCP 连接
#: 从未建立——零字节到达对端，重发不构成第二次外发请求（不需要重新审批）。
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
            or self.min_interval_s < 0
        ):
            raise NetError(
                f"min_interval_s 必须是 >= 0 的数值：{self.min_interval_s!r}",
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
) -> Fetched:
    """GET 一个网络目标。阻塞式；所有失败都以 `NetError` / `NetCancelled` 抛出。

    连接阶段失败（`net.connect` / `net.connect_timeout`）按
    `NetLimits.connect_attempts` 自动重试——TCP 从未建立，重发不构成第二次
    外发请求；其余失败原样抛出（服务器已见过请求，重试必须重新批准）。
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
    for attempt in range(1, limits.connect_attempts + 1):
        if attempt > 1:
            _check_cancel(cancel)
        try:
            return _fetch_once(
                target, limits, cancel, deadline, visited, attempt, started
            )
        except NetError as exc:
            if exc.reason not in _RETRYABLE_REASONS or attempt >= limits.connect_attempts:
                raise
    raise NetError("重定向次数超过上限", reason="net.redirect_limit")  # pragma: no cover


def _fetch_once(
    target: NetworkTarget,
    limits: NetLimits,
    cancel: Callable[[], bool] | None,
    deadline: float,
    visited: set[str],
    attempt: int,
    fetch_started: float,
) -> Fetched:
    """单次尝试：完整的重定向跟随 + 有界读取。失败以 `NetError` / `NetCancelled` 抛出。"""
    current = target
    redirects: list[str] = []

    for hop in range(limits.max_redirects + 1):
        _check_cancel(cancel)
        remaining = _remaining(deadline)
        conn = _connect(current, remaining)
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
            remaining = _remaining(deadline)

            def interrupt_read(sock: socket.socket | None = sock) -> None:
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
            _remaining(deadline)
            response = conn.getresponse()
            _remaining(deadline)
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
                _remaining(deadline)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limits.max_bytes:
                    del body[limits.max_bytes :]
                    truncated = True
                    break
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
            raise NetError(f"TLS 失败：{exc}", reason="net.tls") from exc
        except OSError as exc:
            _remaining(deadline)
            # 连接已经建立之后的 IO 失败：可能已经读到了一部分字节，用量未知。
            raise NetError(
                f"读取失败：{type(exc).__name__}: {exc}",
                reason="net.read",
            ) from exc
        except http.client.HTTPException as exc:
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
    "summary_text",
]
