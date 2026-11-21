"""Explicit loopback ingress for one preconfigured local event schedule."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import ipc
from .errors import TianshuError

_PATH = "/v1/webhook"
_EVENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SCHEDULE = re.compile(r"sched_[a-f0-9]{12}")
_MAX_BODY = 1024
_WINDOW = 300
_READ_TIMEOUT = 3.0


class _RequestInput(io.RawIOBase):
    """Use the same deadline for every recv hidden inside a buffered read."""

    def __init__(self, connection: socket.socket) -> None:
        super().__init__()
        self._connection = connection
        self._deadline = time.monotonic() + _READ_TIMEOUT

    def readable(self) -> bool:
        return True

    def remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Webhook request read deadline exceeded")
        return remaining

    def readinto(self, buffer: bytearray | memoryview) -> int:
        self._connection.settimeout(self.remaining())
        return self._connection.recv_into(buffer)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _event_id(raw: bytes) -> str:
    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(body, dict) or set(body) != {"event_id"}:
        raise ValueError("expected event_id")
    event = body["event_id"]
    if not isinstance(event, str) or not _EVENT.fullmatch(event):
        raise ValueError("invalid event_id")
    return event


def _handler(root: Path, schedule_id: str, secret: bytes):
    prefix = f"tianshu-webhook-v1\n{schedule_id}\n".encode("ascii")

    class WebhookHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(_READ_TIMEOUT)
            self.rfile.close()
            self._request_input = _RequestInput(self.connection)
            self.rfile = io.BufferedReader(self._request_input)

        def log_message(self, _format: str, *_args: Any) -> None:
            return  # Untrusted paths, event identifiers and headers are never logged.

        def _reply(self, status: int, data: dict[str, Any]) -> None:
            reply = json.dumps(data, separators=(",", ":")).encode("ascii")
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(reply)

        def _header(self, key: str) -> str:
            values = self.headers.get_all(key, [])
            return values[0] if len(values) == 1 else ""

        def do_POST(self) -> None:  # noqa: N802
            # BaseHTTPRequestHandler normalizes a leading "//" in self.path.
            if self.requestline.split()[1] != _PATH:
                self._reply(404, {"error": "not_found"})
                return
            length = self._header("Content-Length")
            if (self.headers.get_all("Transfer-Encoding")
                    or self.headers.get_all("Content-Encoding")
                    or self._header("Content-Type").lower() != "application/json"
                    or not re.fullmatch(r"[0-9]{1,10}", length)):
                self._reply(400, {"error": "invalid_request"})
                return
            size = int(length)
            if size > _MAX_BODY:
                self._reply(413, {"error": "too_large"})
                return
            stamp = self._header("X-Tianshu-Timestamp")
            signature = self._header("X-Tianshu-Signature")
            if (not re.fullmatch(r"[1-9][0-9]{0,11}", stamp)
                    or abs(int(time.time()) - int(stamp)) > _WINDOW
                    or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature)):
                self._reply(401, {"error": "unauthorized"})
                return
            body = self.rfile.read(size)
            # The request may have been entirely prefetched while parsing headers.
            self._request_input.remaining()
            self.connection.settimeout(_READ_TIMEOUT)
            if len(body) != size:
                self._reply(400, {"error": "invalid_request"})
                return
            signed = prefix + stamp.encode("ascii") + b"\n" + body
            expected = "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                self._reply(401, {"error": "unauthorized"})
                return
            try:
                event = _event_id(body)
            except (ValueError, RecursionError):
                self._reply(400, {"error": "invalid_event"})
                return
            try:
                with ipc.Client(root, timeout=3) as client:
                    receipt = client.call("schedule.signal", {
                        "schedule_id": schedule_id, "event_id": event,
                    })
            except ipc.RemoteError as exc:
                if exc.code in ("conflict", "not_found"):
                    self._reply(409, {"error": "schedule_unavailable"})
                else:
                    self._reply(503, {"error": "daemon_unavailable"})
                return
            except (ipc.IpcError, OSError, EOFError):
                self._reply(503, {"error": "daemon_unavailable"})
                return
            if not isinstance(receipt.get("accepted"), bool):
                self._reply(503, {"error": "daemon_unavailable"})
                return
            self._reply(202, {"accepted": receipt["accepted"]})

    return WebhookHandler


def serve(root: Path, schedule_id: str, *, port: int, secret_env: str) -> int:
    if not _SCHEDULE.fullmatch(schedule_id) or not 0 <= port <= 65535:
        raise TianshuError("Webhook 需要有效的事件日程 ID 和端口")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", secret_env):
        raise TianshuError("Webhook 密钥环境变量名无效")
    secret = os.environ.get(secret_env, "").encode("utf-8")
    if not 32 <= len(secret) <= 4096:
        raise TianshuError("Webhook 密钥环境变量必须包含 32 到 4096 个 UTF-8 字节")
    with ipc.Client(root, timeout=3) as client:
        schedules = client.call("schedule.list")["schedules"]
    if not any(row["schedule_id"] == schedule_id and row["trigger"]["kind"] == "event"
               for row in schedules):
        raise TianshuError("Webhook 只能绑定既有本机事件日程")
    with HTTPServer(("127.0.0.1", port), _handler(root, schedule_id, secret)) as server:
        print(f"Webhook: http://127.0.0.1:{server.server_port}{_PATH}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
