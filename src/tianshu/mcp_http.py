"""Client subset of MCP Streamable HTTP: pinned, bounded POST, no reconnect or replay."""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from . import net
from .credentials import register_secret
from .mcp import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_DEFAULT_PROTOCOL_VERSION,
    MCP_MAX_MESSAGE_BYTES,
    McpCallResult,
    McpClosed,
    McpConfigError,
    McpError,
    McpProtocolError,
    McpServerConfig,
    McpServerUnavailable,
    McpTimeout,
    _ToolClient,
)
from .resources import NetworkTarget, ResourceError

_PROTOCOLS = frozenset({"2025-03-26", "2025-06-18"})
_CHUNK_SIZE = re.compile(rb"([0-9a-fA-F]{1,16})(?:[ \t]*;[\t -~]*)?\r\n")
_TRAILER = re.compile(rb"([!#$%&'*+.^_`|~0-9a-zA-Z-]+):[\t\x20-\x7e\x80-\xff]*\r\n")
_CHUNK_METADATA_BYTES = 65_536
_CHUNK_LINE_BYTES = 4096


def endpoint_for(url: str) -> NetworkTarget:
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or len(url) > 2048
        or re.search(r"[^\x21-\x7e]|[?#\\]", url)
    ):
        raise McpConfigError("远程 MCP url 必须是 HTTPS，且无查询、用户名或 fragment")
    try:
        path = urlsplit(url).path or "/"
        if (
            not re.fullmatch(r"/[a-zA-Z0-9._~/-]*", path)
            or any(part in (".", "..") for part in path.split("/"))
            or path.startswith("//")
        ):
            raise ValueError
        target = NetworkTarget.parse("network://https/" + url.removeprefix("https://"))
    except (ResourceError, ValueError):
        raise McpConfigError("远程 MCP url 不合法") from None
    return replace(target, path=path)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = val
    return out


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _decode(data: bytes, request_id: int) -> dict[str, Any] | None:
    try:
        # Validate nested text as UTF-8 too, including escapes from the incoming JSON.
        payload = json.loads(
            data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant
        )
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, RecursionError):
        raise McpProtocolError("远程 MCP 返回非法 JSON") from None
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise McpProtocolError("远程 MCP 返回非法 JSON-RPC 对象")
    if (
        "id" not in payload
        and isinstance(payload.get("method"), str)
        and not payload.keys() & {"result", "error"}
    ):
        return None  # Server notifications do not authorize anything.
    if (
        type(payload.get("id")) is not int
        or payload["id"] != request_id
        or "method" in payload
        or "error" in payload
        or not isinstance(payload.get("result"), dict)
    ):
        # A remote error envelope does not prove that the remote tool did not act.
        raise McpProtocolError("远程 MCP 请求失败或响应与当前请求不匹配")
    return payload["result"]


def _response_length(response: http.client.HTTPResponse) -> int | None:
    lengths = response.headers.get_all("Content-Length", [])
    encodings = response.headers.get_all("Transfer-Encoding", [])
    if (response.headers.defects or len(lengths) > 1 or len(encodings) > 1
            or (encodings and (lengths or response.version != 11 or not response.chunked))
            or (lengths and (response.length is None
                             or not re.fullmatch(r"[0-9]+", lengths[0].strip(" \t"))))):
        raise McpProtocolError("远程 MCP HTTP 响应分帧不明确")
    return response.length if lengths else None


def _read_chunks(response: http.client.HTTPResponse) -> Iterator[bytes]:
    # CPython 3.12's chunk reader discards the payload delimiter without checking it
    # and treats EOF in the trailers as success. Yield only fully validated chunks.
    stream = response.fp
    if stream is None:
        raise McpProtocolError("远程 MCP HTTP 响应流已关闭")
    remaining = MCP_MAX_MESSAGE_BYTES
    overhead = _CHUNK_METADATA_BYTES

    def read_line() -> bytes:
        nonlocal overhead
        limit = min(overhead, _CHUNK_LINE_BYTES)
        line = stream.readline(limit + 1)
        if not line or len(line) > limit or not line.endswith(b"\r\n"):
            raise McpProtocolError("远程 MCP 分块响应头不完整或超限")
        overhead -= len(line)
        return line

    while True:
        chunk = _CHUNK_SIZE.fullmatch(read_line())
        if chunk is None:
            raise McpProtocolError("远程 MCP 响应的分块长度无效")
        size = int(chunk[1], 16)
        if size == 0:
            break
        if size > remaining or overhead < 2:
            raise McpProtocolError("远程 MCP 分块响应超出上限")
        block = stream.read(size + 2)
        if len(block) != size + 2 or block[-2:] != b"\r\n":
            raise McpProtocolError("远程 MCP HTTP 分块不完整")
        overhead -= 2
        remaining -= size
        yield block[:-2]

    while (line := read_line()) != b"\r\n":
        trailer = _TRAILER.fullmatch(line)
        if trailer is None or trailer[1].lower() in {b"content-length", b"transfer-encoding"}:
            raise McpProtocolError("远程 MCP HTTP 尾部字段无效")


def _sse_lines(response: http.client.HTTPResponse) -> Iterator[bytes]:
    if not response.chunked:
        remaining = MCP_MAX_MESSAGE_BYTES
        while remaining > 0:
            line = response.readline(remaining + 1)
            remaining -= len(line)
            if remaining < 0 or not line.endswith(b"\n"):
                return
            yield line
        return

    # One SSE line can cross any number of HTTP chunks. Check the containing
    # chunk's delimiter before interpreting its bytes, but don't await the next chunk.
    prefix = bytearray()
    for chunk in _read_chunks(response):
        start = 0
        while (end := chunk.find(b"\n", start)) != -1:
            if prefix:
                prefix.extend(chunk[start:end + 1])
                yield bytes(prefix)
                prefix.clear()
            else:
                yield chunk[start:end + 1]
            start = end + 1
        prefix.extend(chunk[start:])


def _read_result(response: http.client.HTTPResponse, request_id: int) -> dict[str, Any]:
    length = _response_length(response)
    content_type = response.headers.get_content_type()
    if content_type == "application/json":
        if response.chunked:
            data = b"".join(_read_chunks(response))
        else:
            if length is None or length > MCP_MAX_MESSAGE_BYTES:
                raise McpProtocolError("远程 MCP JSON 响应缺少明确的有界长度")
            data = response.read(length)
            if len(data) != length:
                raise McpProtocolError("远程 MCP JSON 响应未读满声明的长度")
        if (result := _decode(data, request_id)) is not None:
            return result
    elif content_type == "text/event-stream":
        # Stop at the matching result, even if the peer keeps the event stream open.
        data_lines: list[bytes] = []
        for line in _sse_lines(response):
            line = line.rstrip(b"\r\n")
            if not line:
                if data_lines:
                    result = _decode(b"\n".join(data_lines), request_id)
                    if result is not None:
                        return result
                data_lines.clear()
            elif line == b"data" or line.startswith(b"data:"):
                value = line.partition(b":")[2]
                data_lines.append(value.removeprefix(b" "))
    raise McpProtocolError("远程 MCP 未返回完整的有界响应")


class HttpServer(_ToolClient):
    pid = None

    def __init__(self, config: McpServerConfig, *, environ: Mapping[str, str] | None) -> None:
        self.config = config
        self._endpoint = endpoint_for(config.url)
        self._environ = os.environ if environ is None else environ
        self._token = ""
        self._session = ""
        self.identity = ""
        self.protocol_version = ""
        self.server_info: dict[str, Any] = {}
        self._tools = ()
        self._tools_truncated = False
        self._last_request_id = 0
        self._lock = threading.RLock()
        self._closed = False

    @property
    def is_alive(self) -> bool:
        return bool(self.identity) and not self._closed

    @property
    def tools(self):
        return self._tools

    @property
    def tools_truncated(self) -> bool:
        return self._tools_truncated

    def diagnostics(self) -> str:
        return "ok" if self.is_alive else "远程 MCP 会话已失效"

    def start(self) -> None:
        try:
            if slot := self.config.bearer_token_env:
                token = self._environ.get(slot, "")
                if not re.fullmatch(r"[a-zA-Z0-9._~+/=-]{1,8192}", token):
                    raise McpConfigError("指定的远程 MCP 凭据缺失或格式无效")
                self._token = token
                register_secret(token)
            initialize = self.request(
                "initialize",
                {
                    "protocolVersion": MCP_DEFAULT_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
                },
                timeout_ms=self.config.handshake_timeout_ms,
            )
            version = initialize.get("protocolVersion")
            caps = initialize.get("capabilities")
            if (
                not isinstance(version, str)
                or version not in _PROTOCOLS
                or not isinstance(caps, dict)
                or not isinstance(caps.get("tools"), dict)
            ):
                raise McpProtocolError("远程 MCP 未声明受支持的版本及 tools 能力")
            self.protocol_version = version
            info = initialize.get("serverInfo")
            self.server_info = info if isinstance(info, dict) else {}
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                deadline=time.monotonic() + self.config.handshake_timeout_ms / 1000,
            )
            self._discover_tools()
            # New client generation never inherits an old action's authority, even if
            # a server reuses the same session ID or has no session ID at all.
            self.identity = "\n".join(
                (
                    self.config.url,
                    self.config.bearer_token_env,
                    secrets.token_hex(16),
                )
            )
        except McpError as exc:
            self.close()
            raise McpServerUnavailable(str(exc), server=self.name) from exc

    def close(self) -> None:
        self._closed = True
        self._token = ""
        self._session = ""

    def call_tool(self, tool, arguments, *, timeout_ms=None, call_id="") -> McpCallResult:
        limit = self.config.call_timeout_ms if timeout_ms is None else timeout_ms
        # Keep the request ID bound to its result even with concurrent callers.
        started = time.monotonic()
        if not self._lock.acquire(timeout=limit / 1000):
            raise McpTimeout("等待前一个远程 MCP 调用结束超时")
        try:
            if not self.is_alive:
                raise McpClosed("远程 MCP 会话已失效")
            remaining = limit - int((time.monotonic() - started) * 1000)
            if remaining <= 0:
                raise McpTimeout("等待前一个远程 MCP 调用结束超时")
            return super().call_tool(tool, arguments, timeout_ms=remaining, call_id=call_id)
        finally:
            self._lock.release()

    def request(
        self, method: str, params: dict[str, Any] | None, *, timeout_ms: int
    ) -> dict[str, Any]:
        self._last_request_id += 1
        return self._post(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": self._last_request_id,
            },
            deadline=time.monotonic() + timeout_ms / 1000,
        )

    def _post(self, message: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        if self._closed:
            raise McpClosed("远程 MCP 会话已失效")
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, RecursionError):
            raise McpProtocolError("远程 MCP 请求参数不合法") from None
        if len(body) > MCP_MAX_MESSAGE_BYTES:
            raise McpProtocolError("远程 MCP 请求超出上限")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        conn = response = timer = None
        interrupted = threading.Event()
        try:
            net.require_public_target(self._endpoint)
            conn = net._connect(self._endpoint, net._remaining(deadline))
            net._remaining(deadline)
            conn.connect()
            sock = conn.sock
            remaining = net._remaining(deadline)

            def interrupt() -> None:
                interrupted.set()
                if sock is not None:
                    try:
                        socket.socket.shutdown(sock, socket.SHUT_RDWR)
                    except OSError:
                        pass

            timer = threading.Timer(remaining, interrupt)
            timer.daemon = True
            timer.start()
            conn.request("POST", self._endpoint.path, body=body, headers=headers)
            response = conn.getresponse()
            if "id" not in message and response.status == 202:
                return {}
            if "id" not in message or response.status != 200:
                if response.status in (401, 403, 404):
                    self.close()
                raise McpProtocolError(f"远程 MCP HTTP {response.status}")
            if message["method"] == "initialize":
                sessions = response.headers.get_all("Mcp-Session-Id", [])
                if sessions:
                    if len(sessions) != 1 or not re.fullmatch(r"[\x21-\x7e]{1,1024}", sessions[0]):
                        raise McpProtocolError("远程 MCP 会话标识无效")
                    self._session = sessions[0]
            result = _read_result(response, message["id"])
            net._remaining(deadline, interrupted)
            # The peer can see the auth headers, but a reflected credential or session
            # ID must not be committed in discovery/result events or sent to a model.
            text = json.dumps(result, ensure_ascii=False)
            if any(secret and secret in text for secret in (self._token, self._session)):
                raise McpProtocolError("远程 MCP 响应含凭据或会话标识，已拒绝")
            return result
        except TimeoutError:
            raise McpTimeout("远程 MCP 等待响应超时", request_id=self._last_request_id) from None
        except (OSError, http.client.HTTPException, net.NetError, McpProtocolError) as exc:
            if interrupted.is_set() or (isinstance(exc, net.NetError) and "timeout" in exc.reason):
                raise McpTimeout(
                    "远程 MCP 等待响应超时", request_id=self._last_request_id
                ) from None
            if isinstance(exc, McpProtocolError):
                raise
            raise McpClosed("远程 MCP 网络连接失败") from None
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if conn is not None:
                conn.close()
