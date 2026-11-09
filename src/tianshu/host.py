"""M5 Host：单实例、监护 Daemon、把 UI 的 HTTP 桥到本地 IPC。

Host 的职责边界（PRD §1.2）：它**不拥有任务状态**，只做三件事——

1. **单实例**：同一份数据目录只允许一个 Host。判据不是"锁文件在不在"（会残留），
   而是"端口上还有没有人应答"；锁文件只用来把 URL/token 交给第二个实例。
2. **监护**：Daemon 是独立进程，Host 负责把它拉起来，并在它崩溃时按
   1s/2s/4s/8s/30s 退避重启（TECH-ARCH §2.1）。**Host 被杀不影响 Daemon**：
   这正是"关掉界面任务继续跑"的实现方式，不是靠一条长连接撑着。
3. **UI 桥**：浏览器不能说 IPC，所以 Host 在 `127.0.0.1` 上随机端口起一个
   HTTP 服务，把 `POST /rpc`、`GET /state`、`GET /events` 翻译成 IPC 调用。

## 为什么是 loopback HTTP（而不是"直接给 WebView 用 IPC"）

TECH-ARCH 的原方案是 Tauri 外壳 + WebView 直接调 Rust Host 的 IPC。M5 没有外壳
（见 `docs/M5-desktop.md` D1/D3）：浏览器里跑 UI，就得给它一个能说的协议。
`docs/M5-desktop.md` 的偏差表 D3 已经登记这条，替换时机是外壳落地时——那时
删掉本文件的 HTTP 层，UI 换成 WebView 的 postMessage，Daemon 侧契约不动。

## 安全边界（重要）

- 只绑 `127.0.0.1`，不绑 `0.0.0.0`：局域网里的别人连不上。
- 所有数据端点都要 `X-Tianshu-Token`；token 随机、进程内生成，只写进
  `host.lock`（0600）和页面 `<meta>`。**不进 URL、不进日志。**
- 校验 `Host` 头必须是 `127.0.0.1:<port>` / `localhost:<port>`：本机 DNS 重绑定
  攻击会用别的域名解析到 127.0.0.1，光靠"只有本机能连"挡不住。
- 静态资源只从 `ui/dist` 里取，`..` 与符号链接越界一律 404。
- `/health` 不校验 token（只回答"活着吗"），也不返回任何用户数据。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import ipc
from .errors import TianshuError

HOST_LOCK_NAME = "host.lock"
MAX_RPC_BODY = 1 * 1024 * 1024
#: 超限请求体在拒绝前最多丢弃这么多。不读完就断开，客户端可能只看到连接重置而
#: 看不到 413；但也不能无上限读完（那就是给 DoS 开门）。
MAX_DRAIN_BODY = 2 * 1024 * 1024
RESTART_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 30.0)
#: Daemon 活过这个时长才把退避计数清零：否则"起来-立刻崩"会一直从 1s 重试，
#: 把 CPU 打满还让人以为它在正常工作。
BACKOFF_RESET_AFTER_S = 60.0


class HostError(TianshuError):
    """Host 层的可预期失败。"""


# --------------------------------------------------------------------------- 锁


@dataclass
class HostInfo:
    pid: int
    port: int
    token: str
    started_at: int
    ui_dir: str = ""


def host_lock_path(data_root: Path) -> Path:
    return ipc.run_dir(data_root) / HOST_LOCK_NAME


def read_host_info(data_root: Path) -> HostInfo | None:
    path = host_lock_path(data_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    try:
        return HostInfo(
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            token=str(raw["token"]),
            started_at=int(raw.get("started_at") or 0),
            ui_dir=str(raw.get("ui_dir") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_host_info(data_root: Path, info: HostInfo) -> None:
    path = host_lock_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": info.pid,
        "port": info.port,
        "token": info.token,
        "started_at": info.started_at,
        "ui_dir": info.ui_dir,
    }
    path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def clear_host_info(data_root: Path, *, pid: int | None = None) -> None:
    """只有还属于自己时才删：别把新实例刚写好的锁删掉。"""
    current = read_host_info(data_root)
    if current is None:
        return
    if pid is not None and current.pid != pid:
        return
    host_lock_path(data_root).unlink(missing_ok=True)


def _http_alive(info: HostInfo, *, timeout: float = 0.4) -> bool:
    """真连一次 HTTP。只看 PID 活着会被 PID 复用骗过去。"""
    try:
        with socket.create_connection(("127.0.0.1", info.port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        status, _headers, body = _http_get(info.port, "/health", timeout=timeout)
    except OSError:
        return False
    return status == HTTPStatus.OK and b'"ok":true' in body.replace(b" ", b"")


def _http_get(port: int, path: str, *, timeout: float = 1.0) -> tuple[int, dict[str, str], bytes]:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split(b" ")[1])
    except (IndexError, ValueError):
        raise OSError("HTTP 响应无法解析") from None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").lower()] = value.decode("latin-1").strip()
    return status, headers, body


def acquire_host_info(data_root: Path, *, ui_dir: Path) -> HostInfo:
    """写锁前先确认没有活着的 Host；有就抛 `HostError`（附 URL，调用方直接复用）。"""
    existing = read_host_info(data_root)
    if existing is not None:
        if _http_alive(existing):
            raise HostError(
                f"Host 已在运行：http://127.0.0.1:{existing.port}/"
                f"?token={existing.token}"
            )
        # 残留锁：进程已死或端口被别的东西占了。删掉重建，不沿用旧 token
        # （旧 token 可能已经出现在某个聊天窗口里了）。
        host_lock_path(data_root).unlink(missing_ok=True)
    info = HostInfo(
        pid=os.getpid(),
        port=0,
        token=secrets.token_urlsafe(24),
        started_at=int(time.time() * 1000),
        ui_dir=str(ui_dir),
    )
    write_host_info(data_root, info)
    return info


# --------------------------------------------------------------------------- 监护


class DaemonSupervisor:
    """拉起并监护 Daemon。

    `command` 是完整的 argv（Host 只负责 spawn，不拼业务参数）。
    """

    def __init__(
        self,
        data_root: Path,
        command: list[str],
        *,
        log_path: Path | None = None,
        start_timeout_s: float = 15.0,
    ) -> None:
        self.data_root = Path(data_root)
        self.command = list(command)
        self.log_path = log_path or (Path(data_root) / "logs" / "daemon.log")
        self.start_timeout_s = start_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_file: Any = None
        self.restarts = 0
        self.last_error = ""

    # -- 查询 --

    def alive(self) -> bool:
        return ipc.probe(self.data_root, timeout=0.3)

    # -- 生命周期 --

    def ensure(self) -> None:
        if self.alive():
            return
        self._spawn_and_wait()

    def start_monitor(self) -> None:
        self._thread = threading.Thread(
            target=self._monitor_loop, name="daemon-supervisor", daemon=True
        )
        self._thread.start()

    def stop(self, *, reason: str = "user", timeout: float = 5.0) -> None:
        self._stop.set()
        self._shutdown_daemon(reason=reason, timeout=timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout + 1)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._terminate(self._process.pid)
            self._process = None
        self._close_log()

    # -- 内部 --

    def _monitor_loop(self) -> None:
        """Daemon 死了就按退避重启；活过 60s 才清零退避计数。"""
        alive_since: float | None = None
        while not self._stop.is_set():
            self._stop.wait(0.5)
            if self._stop.is_set():
                return
            if self.alive():
                if alive_since is None:
                    alive_since = time.monotonic()
                continue
            if alive_since is not None and time.monotonic() - alive_since > BACKOFF_RESET_AFTER_S:
                self.restarts = 0
            alive_since = None
            delay = RESTART_BACKOFF_S[min(self.restarts, len(RESTART_BACKOFF_S) - 1)]
            self.restarts += 1
            # 退避期间也可能自己起来了（比如另一个 CLI 拉起的 daemon）：
            # 每 100ms 看一眼，别傻等。
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not self._stop.is_set():
                if self.alive():
                    break
                self._stop.wait(0.1)
            if self._stop.is_set():
                return
            if self.alive():
                continue
            try:
                self._spawn_and_wait()
            except HostError as exc:
                self.last_error = str(exc)

    def _spawn_and_wait(self) -> None:
        with self._lock:
            self._open_log()
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
                    | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                )
            else:
                # 新会话：Host 被 Ctrl-C / 被杀，Daemon 不该跟着陪葬。
                kwargs["start_new_session"] = True
            self._process = subprocess.Popen(  # noqa: S603 - 命令由本进程构造
                self.command,
                stdout=self._log_file,
                stderr=self._log_file,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            if self.alive():
                return
            if self._process.poll() is not None and not self.alive():
                # 进程已经退出且端点没起来：不要无限等，把原因带出去。
                raise HostError(
                    f"Daemon 启动失败（exit={self._process.returncode}）；"
                    f"日志：{self.log_path}"
                )
            time.sleep(0.05)
        raise HostError(f"Daemon 在 {self.start_timeout_s:.0f}s 内没有就绪；日志：{self.log_path}")

    def _shutdown_daemon(self, *, reason: str, timeout: float) -> None:
        try:
            with ipc.Client(self.data_root, timeout=timeout) as client:
                client.call("shutdown", {"reason": reason}, timeout=timeout)
        except (ipc.IpcError, OSError):
            return  # 本来就没起来/已经退了
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not ipc.probe(self.data_root, timeout=0.2):
                return
            time.sleep(0.05)
        # 超时：按 daemon.lock 里的 pid 收掉进程树（Host 是监护人，这是它该做的）。
        pid = _daemon_pid(self.data_root)
        if pid is not None:
            self._terminate(pid)

    @staticmethod
    def _terminate(pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(pid, 15)
        except OSError:
            pass

    def _open_log(self) -> None:
        if self._log_file is None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.log_path, "ab")  # noqa: SIM115 - 生命周期由本类管
            if os.name != "nt":
                try:
                    os.chmod(self.log_path, 0o600)
                except OSError:
                    pass

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            finally:
                self._log_file = None


def _daemon_pid(data_root: Path) -> int | None:
    path = ipc.run_dir(data_root) / "daemon.lock"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_bytes().decode("utf-8"))
        return int(raw["pid"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- UI 桥


@dataclass
class BridgeConfig:
    data_root: Path
    ui_dir: Path
    token: str
    host: str = "127.0.0.1"
    port: int = 0
    serve_forever: bool = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "TianshuHost/0.1"
    protocol_version = "HTTP/1.1"

    # 注入点：Bridge 实例把自己的配置挂在这里（BaseHTTPRequestHandler 每条
    # 连接都会新建实例，不能把状态放实例上）。
    config: BridgeConfig
    stop_callback: Any = None

    # -- 基础 --

    def log_message(self, fmt: str, *args: Any) -> None:
        # 只写最低限度：默认实现会把每个请求都打进 stderr，用户看到的是噪音。
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server 的接口名
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/health":
                return self._json(HTTPStatus.OK, {"ok": True, "pid": os.getpid()})
            if path == "/state":
                if not self._authorized():
                    return
                return self._proxy("state", {})
            if path == "/events":
                if not self._authorized():
                    return
                since = query.get("since_seq", ["0"])[0]
                once = query.get("once", ["0"])[0] == "1"
                return self._stream_events(int(since), once=once)
            if path == "/shutdown":
                if not self._authorized():
                    return
                self._json(HTTPStatus.OK, {"ok": True, "shutting_down": True})
                if callable(self.stop_callback):
                    threading.Thread(target=self.stop_callback, daemon=True).start()
                return
            return self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/rpc":
            return self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": {"code": "not_found", "message": "没有这个端点"}},
            )
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_RPC_BODY:
            if 0 < length <= MAX_DRAIN_BODY:
                # 读完再拒绝：否则客户端在 sendall 中途被 RST，拿不到 413，
                # 只能报"连接被重置"——一个用户无法据此处理的错误。
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            return self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": {"code": "bad_params", "message": "请求体大小非法"}},
            )
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "bad_params", "message": "请求体不是 JSON"}},
            )
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if not method:
            return self._json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": {"code": "bad_params", "message": "缺少 method"}},
            )
        return self._proxy(method, params)

    # -- 安全 --

    def _authorized(self) -> bool:
        if not self._host_header_ok():
            self._json(
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": {"code": "forbidden", "message": "Host 头不合法"}},
            )
            return False
        token = self.headers.get("X-Tianshu-Token") or ""
        if not secrets.compare_digest(token, self.config.token):
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "error": {"code": "unauthorized", "message": "缺少或错误的 token"}},
            )
            return False
        return True

    def _host_header_ok(self) -> bool:
        """防 DNS 重绑定：浏览器带的 Host 必须是回环地址。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name, _, port = host.partition(":")
        if name not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        expected = str(self.server.server_address[1])  # type: ignore[attr-defined]
        return port in ("", expected)

    # -- 输出 --

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str, params: dict[str, Any]) -> None:
        try:
            # 连接超时单独给短值：Daemon 不在时前端要**马上**看到"连接已断开"，
            # 而不是转 60 秒圈。方法本身可以慢（recover/undo 要写盘），才给长超时。
            with ipc.Client(self.config.data_root, timeout=2) as client:
                result = client.call(method, params, timeout=60)
        except ipc.RemoteError as exc:
            code = exc.code or "internal"
            status = {
                "unknown_method": HTTPStatus.NOT_IMPLEMENTED,
                "bad_params": HTTPStatus.BAD_REQUEST,
                "not_found": HTTPStatus.NOT_FOUND,
                "conflict": HTTPStatus.CONFLICT,
                "forbidden": HTTPStatus.FORBIDDEN,
                "refused": HTTPStatus.CONFLICT,
                "protocol_mismatch": HTTPStatus.BAD_GATEWAY,
            }.get(code, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self._json(
                status,
                {
                    "ok": False,
                    "error": {"code": code, "message": exc.message, "details": exc.details},
                },
            )
        except (ipc.IpcError, OSError) as exc:
            # 守护进程不在：前端据此显示"连接已断开"，而不是一个 500 堆栈。
            return self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "error": {"code": "daemon_unavailable", "message": str(exc)},
                },
            )
        return self._json(HTTPStatus.OK, {"ok": True, "result": result})

    def _stream_events(self, since_seq: int, *, once: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        try:
            client = ipc.Client(self.config.data_root, timeout=2)
            client.connect()
        except (ipc.IpcError, OSError):
            self._write_comment("daemon_unavailable")
            return
        try:
            client.subscribe(max(0, since_seq))
            idle = 0.0
            while True:
                try:
                    message = client.next_event(timeout=0.5)
                except ipc.ProtocolError:
                    break
                if message is None:
                    idle += 0.5
                    if once:
                        break
                    if idle >= 10.0:
                        # 心跳注释帧：让浏览器/中间层知道连接还活着，
                        # 也避免被空闲超时静默掐断。
                        self._write_comment("keepalive")
                        idle = 0.0
                    continue
                idle = 0.0
                event = message.get("event")
                if not isinstance(event, dict):
                    continue
                self._write_frame(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            client.close()

    def _write_frame(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"event: event\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _write_comment(self, text: str) -> None:
        try:
            self.wfile.write(f": {text}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # -- 静态资源 --

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        ui_dir = self.config.ui_dir
        index = ui_dir / "index.html"
        if not index.exists():
            return self._fallback_page()
        relative = path.lstrip("/")
        target = (ui_dir / relative).resolve()
        try:
            target.relative_to(ui_dir.resolve())
        except ValueError:
            return self._json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": {"code": "not_found", "message": "没有这个文件"}},
            )
        if not target.is_file():
            # SPA：没有扩展名的路径交回 index.html，刷新子路由不该 404。
            if "." not in Path(relative).name:
                target = index
            else:
                return self._json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "not_found", "message": "没有这个文件"}},
                )
        data = target.read_bytes()
        if target.name == "index.html":
            data = _inject_token(data, self.config.token)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        cache = "no-store" if target.name == "index.html" else "max-age=300"
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _fallback_page(self) -> None:
        html = (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<title>天枢 Agent</title>"
            "<body style=\"font-family:system-ui;padding:40px;line-height:1.7\">"
            "<h1>界面资源不存在</h1>"
            "<p>Host 已经起来了，但找不到 <code>ui/dist/index.html</code>。</p>"
            "<p>在仓库根执行 <code>pnpm --dir ui install</code> 再 "
            "<code>pnpm --dir ui run build</code>，然后重启 <code>tianshu host</code>。</p>"
            "</body></html>"
        ).encode()
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


TOKEN_META_RE = re.compile(
    r'<meta\s+name="tianshu-token"\s+content="[^"]*"\s*/?>', re.IGNORECASE
)


def _inject_token(html: bytes, token: str) -> bytes:
    """把 token 给页面。三种页面形态都要能处理，缺一种就是白屏：

    1. 带 `__TIANSHU_TOKEN__` 占位符；
    2. **带一个 content 为空的 `tianshu-token` meta**（`ui/index.html` 就是这种：
       前端约定"从 meta 读，读不到就是没被 Host 加载"）。必须**替换**那一个，
       不能再插一个新的——`querySelector` 拿第一个，拿到空值就等于没 token，
       页面会一直显示"无法连接守护进程"。
    3. 什么标记都没有：往 `<head>` 里插一个。

    `re` 而不是字符串切分：meta 的属主顺序、自闭合写法、多余空格都可能变，
    按固定字符串替换会在前端改一行格式时静默失效。
    """
    text = html.decode("utf-8", errors="replace")
    if "__TIANSHU_TOKEN__" in text:
        return text.replace("__TIANSHU_TOKEN__", token).encode("utf-8")
    replaced = TOKEN_META_RE.sub(
        f'<meta name="tianshu-token" content="{token}" />', text, count=1
    )
    if replaced != text:
        return replaced.encode("utf-8")
    meta = f'<meta name="tianshu-token" content="{token}">'
    if "<head>" in replaced:
        return replaced.replace("<head>", f"<head>{meta}", 1).encode("utf-8")
    return (meta + replaced).encode("utf-8")


class UiBridge(ThreadingHTTPServer):
    """HTTP 服务本体。`serve()` 在后台线程跑，`shutdown()` 停。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        _Handler.config = config
        super().__init__((config.host, config.port), _Handler)
        self._thread: threading.Thread | None = None
        self.on_shutdown: Any = None

    def install_stop_callback(self, callback: Any) -> None:
        """`/shutdown` 被调用时要做什么（Host 退出）。"""
        _Handler.stop_callback = callback

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.port}/?token={self.config.token}"

    def serve_background(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="ui-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


def run_host(
    data_root: Path,
    *,
    ui_dir: Path,
    daemon_command: list[str],
    port: int = 0,
    open_browser: bool = False,
    supervise: bool = True,
    start_timeout_s: float = 15.0,
) -> int:
    """Host 主流程：拿锁 → 拉起 Daemon → 起 UI 桥 → 阻塞。返回退出码。"""
    data_root = Path(data_root)
    data_root.mkdir(parents=True, exist_ok=True)
    ui_dir = Path(ui_dir).resolve()
    daemon_command = list(daemon_command)
    if "--data-root" not in daemon_command:
        daemon_command += ["--data-root", str(data_root)]
    try:
        info = acquire_host_info(data_root, ui_dir=ui_dir)
    except HostError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    supervisor = DaemonSupervisor(data_root, daemon_command, start_timeout_s=start_timeout_s)
    try:
        try:
            supervisor.ensure()
        except HostError as exc:
            print(f"启动守护进程失败：{exc}", file=sys.stderr)
            clear_host_info(data_root, pid=info.pid)
            return 1
        if supervise:
            supervisor.start_monitor()

        config = BridgeConfig(data_root=data_root, ui_dir=ui_dir, token=info.token, port=port)
        bridge = UiBridge(config)
        info.port = bridge.port
        write_host_info(data_root, info)

        stop_event = threading.Event()

        def stop_all() -> None:
            if stop_event.is_set():
                return
            stop_event.set()
            try:
                supervisor.stop()
            finally:
                bridge.stop()

        bridge.install_stop_callback(stop_all)
        bridge.serve_background()
        print(f"天枢 Agent 已启动：{bridge.url}", flush=True)
        if open_browser:
            webbrowser.open(bridge.url)
        try:
            while not stop_event.is_set():
                stop_event.wait(0.5)
        except KeyboardInterrupt:
            print("收到中断，正在退出…", file=sys.stderr, flush=True)
            stop_all()
        return 0
    finally:
        clear_host_info(data_root, pid=info.pid)


def stop_host(data_root: Path, *, timeout: float = 10.0) -> int:
    """`tianshu host stop`：让活着的 Host 自己退出（它负责关 Daemon）。"""
    info = read_host_info(data_root)
    if info is None or not _http_alive(info):
        clear_host_info(data_root)
        print("Host 没有在运行", flush=True)
        return 0
    try:
        _http_post(info.port, "/shutdown", info.token, timeout=timeout)
    except OSError as exc:
        print(f"通知 Host 退出失败：{exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _http_alive(info):
            clear_host_info(data_root)
            print("Host 已退出", flush=True)
            return 0
        time.sleep(0.1)
    print("Host 未在超时内退出", file=sys.stderr)
    return 1


def _http_post(port: int, path: str, token: str, *, timeout: float = 10.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Tianshu-Token: {token}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "BridgeConfig",
    "DaemonSupervisor",
    "HostError",
    "HostInfo",
    "UiBridge",
    "acquire_host_info",
    "clear_host_info",
    "host_lock_path",
    "read_host_info",
    "run_host",
    "stop_host",
    "write_host_info",
]