"""M5 进程间协议：Host / CLI ↔ Daemon 的本地消息通道。

## 为什么不是 HTTP / 不是自造帧格式

TECH-ARCH §4.1 已经把结论写死：**不许监听 TCP**。Windows 上任何 TCP listener
都会触发防火墙授权框，普通用户第一次启动就看见"是否允许天枢访问网络"，
这是灾难性体验；端口冲突还没有解。所以两个进程之间只能是本地端点。

TECH-ARCH §4.2 写的是 `[4 字节 BE 长度][JSON]`。M5 的实现在**消息语义**上完全
遵守（一条消息 = 一个 JSON 对象，字段同名），但帧编码改用标准库
`multiprocessing.connection`，理由有三条，都是"少写一层就少一处到 Windows 才
会炸的差异"：

1. **Windows 没有 AF_UNIX**（Python 3.12 的 `socket` 在 Windows 上仍不支持），
   要用命名管道。`multiprocessing.connection` 的 `AF_PIPE` 后端就是命名管道，
   `AF_UNIX` 后端在 POSIX 上就是 unix socket——同一份代码、同一套语义跨三平台，
   而自造长度前缀要分别处理"流式 socket"与"消息式管道"两种读法。
2. **认证是现成的**：它用 HMAC 做连接握手（`authkey`），不通过就
   `AuthenticationError`。自己写长度前缀时，认证要么没有，要么又造一套。
3. **一帧一条消息是天然对齐的**：`send_bytes` / `recv_bytes` 保证边界，不会
   出现"读到半条 JSON 开始解析"这类只在负载大时才复现的问题。

代价：它自带 4 字节长度前缀的格式是私有的（不保证跨 Python 版本稳定），所以
**本协议只允许同机同 Python 版本族通信**——Host 与 Daemon 都来自同一个安装包，
这条前提成立。跨版本/跨语言要换传输层时，改的只是 `Server`/`Client` 两个类，
上层的 `{"id","method","params"}` / `{"ok","result"}` 不动。

## 三类消息

```
请求   {"id": "c7", "method": "run.start", "params": {...}}
响应   {"id": "c7", "ok": true, "result": {...}}
       {"id": "c7", "ok": false, "error": {"code": "...", "message": "...", "details": {...}}}
推送   {"push": "event", "event": {"seq": 42, ...}}
```

`id` 由客户端生成、只求连接内唯一；服务端原样回填。**推送没有 `id`**，
所以客户端的 `call()` 必须能跨过推送继续找自己的响应——这是本模块最容易写错
的地方，`Client._receive` 把路上遇到的推送存起来，交给 `next_event()`。

## 握手

```
→ hello {protocol_min: 1, protocol_max: 1, client: "host/0.1.0", pid: 1234}
← {"ok": true, "result": {protocol: 1, daemon_version: "0.1.0",
                          instance_id: "...", event_seq_head: 42, ...}}
```

版本用**区间协商**（PRD §5 同款口径）：两边各报自己支持的最小/最大，交叠就取
交叠上界；不交叠就明确报 `protocol_mismatch` 并**关闭连接**，不做"尽力兼容"。
`negotiate()` 是纯函数，服务端与客户端都可以单独测。

## 谁负责单实例

不是本模块。`ipc.Server.start()` 只负责"把端点建起来"；"同一个 data_root 上
只能有一个 daemon"由 `daemon.py` 的锁负责（它必须在 bind 之前拿到锁，否则
两个进程会互相删对方的 socket）。这里只提供 `probe()`：能连上就说明有人活着，
这是跨平台最可靠的存活判定——比 PID 存活探测强，因为 PID 会复用。
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from multiprocessing.connection import AuthenticationError, Listener
from multiprocessing.connection import Client as _Socket
from pathlib import Path
from typing import Any

from .errors import TianshuError

#: 协议版本区间。**加字段不能改版本**（旧客户端会忽略不认识的键），
#: 改语义/删字段/改字段含义才 +1。
PROTOCOL_MIN = 1
PROTOCOL_MAX = 1

#: 单条消息上限。无上限的本地通道等于给"模型吐了个 2GB payload"开绿灯；
#: 超过就断开而不是解析。
MAX_MESSAGE_BYTES = 8 * 1024 * 1024

#: 一次请求-响应的默认超时。长任务不会阻塞 RPC（`run.start` 只入队），
#: 所以 30 秒足够；扫描/恢复类方法自己带更长的超时。
DEFAULT_TIMEOUT = 30.0

#: 端点路径上限。AF_UNIX 的 sun_path 在 macOS 是 104 字节（Linux 108），
#: 超了会得到 `AF_UNIX path too long`——这个错误信息完全看不出是路径太长，
#: 所以在建端点之前就拒绝，并说清楚怎么办。
MAX_ENDPOINT_BYTES = 100

AUTHKEY_NAME = "ipc.key"
ENDPOINT_DIR = "run"
ENDPOINT_NAME = "agent.sock"

#: 监听队列长度。**不是随手给的**：默认值（`multiprocessing.connection.Listener`
#: 的 backlog=1）意味着只要队列里躺着一条还没被 accept 的连接，接下来的 connect
#: 在 macOS 上就会直接 `ECONNREFUSED`；而 `probe()` 恰恰是"连一下就关"的探活
#: 连接——它自己会在队列里留下一条死连接。issue #63 的"端点存在、进程活着、
#: connect 被拒"就是这么来的（复现与观测见 `docs/m5-readiness.md`）。
LISTEN_BACKLOG = 64

#: `probe()` 对"端点存在但 connect 失败"的重试预算。窗口来自"accept 线程还没把
#: 上一条探活连接处理完"（微秒到毫秒级，慢机器/高负载下更宽），不是网络重试。
PROBE_RETRY_MS = 250

#: 这些 errno 表示"端点文件在、但此刻连不上"，值得在 `PROBE_RETRY_MS` 内重试；
#: `ENOENT` 表示端点真的不存在，重试没有意义（也不该让"没有 daemon"变慢）。
_PROBE_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EINTR,
    }
)


class IpcError(TianshuError):
    """IPC 层可预期的失败（不是 bug）。"""


class ProtocolError(IpcError):
    """协议违规：版本不交叠、消息不是 JSON 对象、缺字段。"""


class ConnectionClosed(IpcError):
    """对端已关闭 / 读超时。调用方应当重连，而不是重试同一个请求。"""


class RemoteError(IpcError):
    """服务端回了 `ok:false`。

    单独一个类型是为了让调用方能**按 code 分支**（`not_found` / `conflict` /
    `forbidden` ...），而不是解析 message 字符串。`details` 原样保留，给 UI
    展示"为什么不行"用。
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


# --------------------------------------------------------------------------- 路径


def default_data_root() -> Path:
    """平台标准数据目录（TECH-ARCH §9）。

    `TIANSHU_DATA_ROOT` 覆盖它——测试与 CI 靠这个把数据放进 tmp，**不允许**
    任何测试写用户主目录。Windows 用 LOCALAPPDATA 而不是 APPDATA(Roaming)：
    事件日志会撑爆漫游配置同步。
    """
    override = os.environ.get("TIANSHU_DATA_ROOT")
    if override:
        return Path(override)
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return base / "AgentOS"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "AgentOS"
    xdg = os.environ.get("XDG_DATA_HOME") or home / ".local" / "share"
    return Path(xdg) / "AgentOS"


def run_dir(data_root: Path) -> Path:
    return Path(data_root) / ENDPOINT_DIR


def endpoint_path(data_root: Path) -> str:
    """平台端点地址。

    POSIX：`<data_root>/run/agent.sock`（文件系统路径）。
    Windows：`\\\\.\\pipe\\AgentOS-<hash>`——命名管道没有"文件"，用 data_root 的
    短哈希做名字，这样同一台机器上不同 data_root 的两个实例（测试、开发）互不
    干扰，而同一个 data_root 天然指向同一个管道。
    """
    root = Path(data_root)
    if os.name == "nt":
        digest = uuid.uuid5(uuid.NAMESPACE_URL, str(root.resolve())).hex[:16]
        return rf"\\.\pipe\AgentOS-{digest}"
    path = run_dir(root) / ENDPOINT_NAME
    raw = len(str(path).encode("utf-8"))
    if raw > MAX_ENDPOINT_BYTES:
        raise ProtocolError(
            f"unix socket 路径太长（{raw} 字节，上限 {MAX_ENDPOINT_BYTES}）："
            f"{path}；把 --data-root 换到更短的目录"
        )
    return str(path)


def authkey_path(data_root: Path) -> Path:
    return run_dir(data_root) / AUTHKEY_NAME


def load_authkey(data_root: Path) -> bytes:
    """读取（不存在则创建）IPC 认证密钥。

    权限 0600、目录 0700：同一台机器上的其他用户/进程不该能连上来。**这不是
    对抗本机用户自己改程序**——开源后任何人都能改，PRD §8.1 已明确不承诺那件事；
    它防的是"顺手连一下"和"另一个 App 的 bug 把我们的通道当自己的"。
    """
    path = authkey_path(data_root)
    if path.exists():
        data = path.read_bytes()
        if len(data) < 32:
            raise ProtocolError(f"认证密钥文件损坏（{len(data)} 字节）：{path}")
        return data
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(path.parent, 0o700)
    key = os.urandom(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:  # 另一个进程刚建好：用它的
        return load_authkey(data_root)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _chmod_private(path, 0o600)
    return key


def _chmod_private(path: Path, mode: int) -> None:
    """Windows 上 chmod 只影响写位、没有 ACL 语义；跳过而不是假装设好了。"""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # 挂在不支持权限的卷上（U 盘 exFAT 等）不值得让守护进程起不来；
        # 认证仍然生效（authkey 本身）。
        pass


@dataclass(frozen=True)
class ProbeResult:
    """一次存活探活的结果，**带失败原因**。

    只返回 bool 的话，"端点不存在"（ENOENT）、"端点文件在但没人监听"
    （ECONNREFUSED）、"连接超时"（队列满/对端不 accept）在调用方看起来一模一样，
    而它们的处置完全不同：前者是"没有 daemon"，后者可能是"daemon 正在启动/正在
    重启"。issue #63 查了两轮没法定根因，就是因为断言里只有一个 False。

    `reason` 是给人看的短句；按 `errno` 分支，不要解析 `reason`。
    """

    ok: bool
    endpoint: str
    reason: str
    errno: int | None = None
    endpoint_exists: bool = False
    attempts: int = 1


def _probe_once_posix(endpoint: str, timeout: float) -> tuple[bool, int | None, str]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(endpoint)
        return True, None, "已连接"
    except OSError as exc:
        errno_value = exc.errno
        if errno_value == errno.ENOENT:
            reason = "端点不存在（没有守护进程，或进程已退出）"
        elif errno_value == errno.ECONNREFUSED:
            reason = "端点文件存在但无人监听（连接被拒）"
        elif errno_value in (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
            reason = "连接超时（监听队列可能已满，accept 线程还没跟上）"
        else:
            reason = f"{type(exc).__name__}: {exc}"
        return False, errno_value, reason
    finally:
        sock.close()


def _probe_once(data_root: Path, timeout: float) -> tuple[bool, int | None, str, str]:
    try:
        endpoint = endpoint_path(data_root)
    except ProtocolError as exc:
        return False, None, str(exc), repr(data_root)
    if os.name == "nt":
        # 命名管道没有"文件存在性"可查：CreateFile 打不开就说明没有实例在听。
        try:
            handle = os.open(endpoint, os.O_RDWR)
        except OSError as exc:
            return False, exc.errno, f"命名管道不可用：{exc}", endpoint
        os.close(handle)
        return True, None, "已连接", endpoint
    ok, errno_value, reason = _probe_once_posix(endpoint, timeout)
    return ok, errno_value, reason, endpoint


def probe_result(
    data_root: Path, *, timeout: float = 0.5, retry_ms: int = PROBE_RETRY_MS
) -> ProbeResult:
    """探活并带回失败原因（issue #63 的 API 面）。

    语义分两层，不要混：

    - **端点是否存在**：ENOENT 直接判死，不重试。否则"没有 daemon"会被
      `PROBE_RETRY_MS` 拖慢，Host 的监护循环会变钝。
    - **端点存在但此刻 connect 失败**（ECONNREFUSED 及其它短暂 errno）：在
      `retry_ms` 预算内重试。窗口的来源是 accept 队列被上一条探活连接占着
      （`probe` 自己"连一下就关"，服务端要先失败一次握手才能腾出位置）；
      这不是"网络抖动"，是本地传输层的排队语义。

    重试**不改变**"能连上才算活"的判据：预算用完还是连不上就返回 False，
    调用方据此走"陈旧锁接管/重启监护"，不会被无限重试拖住。
    """
    budget_ms = max(0, int(retry_ms))
    deadline = time.monotonic() + budget_ms / 1000
    attempts = 0
    while True:
        attempts += 1
        ok, errno_value, reason, endpoint = _probe_once(data_root, timeout)
        if os.name == "nt":
            # 命名管道：没有 errno=ENOENT/ECONNREFUSED 的分层，重试会把
            # "#50 的偶发握手失败"与"真的没实例"混在一起；保持一次判定。
            return ProbeResult(ok, endpoint, reason, errno_value, False, attempts)
        try:
            endpoint_exists = Path(endpoint).exists()
        except OSError:
            endpoint_exists = False
        if ok:
            return ProbeResult(True, endpoint, reason, None, True, attempts)
        transient = (
            errno_value in _PROBE_TRANSIENT_ERRNOS
            and endpoint_exists
            and budget_ms > 0
        )
        if not transient or time.monotonic() >= deadline:
            return ProbeResult(
                False, endpoint, reason, errno_value, endpoint_exists, attempts
            )
        # 短睡再试：让 accept 线程把手里的探活连接处理掉（它要失败一次握手）。
        remaining = max(0.0, deadline - time.monotonic())
        time.sleep(min(0.01, remaining))


def probe(data_root: Path, *, timeout: float = 0.5) -> bool:
    """能连上端点就说明有 daemon 活着（`probe_result` 的布尔包装）。

    **端点存在 ≠ 活着**：被 `kill -9` 的进程会留下 socket 文件，所以必须真的
    连一次。失败原因的区分（ENOENT / ECONNREFUSED / 队列满）在
    `probe_result` 里，需要排障的调用方用那个，不要解析本函数的 False。
    """
    return probe_result(data_root, timeout=timeout).ok


def self_connect(data_root: Path, *, timeout: float = 1.0) -> bool:
    """自己连自己一次，**走完整认证握手**。

    这是 daemon 就绪判据的最后一步：`probe()` 只证明"listen 了"，而
    `Listener.accept()` 在返回连接前还要跑完 challenge/response——如果 accept
    线程没进循环或者卡在半路，`probe` 仍可能是 True（内核帮你排了队），
    `on_ready` 却已经在骗人了。完成一次认证握手才证明"监听到 accept 到应用层
    这一整条路是通的"。

    `timeout` 用旁路线程兜底：`multiprocessing.connection` 的客户端握手没有
    超时参数，主路径不能因为一次握手卡住而起不来。
    """
    try:
        endpoint = endpoint_path(data_root)
        authkey = load_authkey(data_root)
    except (ProtocolError, OSError):
        return False
    outcome: dict[str, bool] = {"ok": False}

    def attempt() -> None:
        try:
            conn = _Socket(endpoint, family=_family(), authkey=authkey)
        except (OSError, AuthenticationError, EOFError, ValueError):
            return
        outcome["ok"] = True
        try:
            conn.close()
        except OSError:
            pass

    worker = threading.Thread(target=attempt, name="ipc-self-connect", daemon=True)
    worker.start()
    worker.join(max(0.0, float(timeout)))
    return bool(outcome["ok"]) and not worker.is_alive()


def _family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


# --------------------------------------------------------------------------- 编解码


def encode(message: dict[str, Any]) -> bytes:
    """消息 → 一帧字节。

    `ensure_ascii=False` 是刻意的：payload 里大量中文，ASCII 转义后体积翻三倍，
    而且调试时肉眼读不了。
    """
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    raw = data.encode("utf-8")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"消息 {len(raw)} 字节，超过上限 {MAX_MESSAGE_BYTES}")
    return raw


def decode(raw: bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"无法解析消息：{exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(f"消息必须是 JSON 对象，收到 {type(message).__name__}")
    return message


def negotiate(params: dict[str, Any]) -> int:
    """版本区间协商。返回双方都能说的最高版本；不交叠抛 `ProtocolError`。

    入参来自 `hello` 的 params；校验"字段存在且是整数"也在这里——
    服务端不该为了一条畸形 hello 去猜客户端想说什么。
    """
    try:
        lo = int(params["protocol_min"])
        hi = int(params["protocol_max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"hello 缺少合法的 protocol_min/protocol_max：{params!r}") from exc
    if lo > hi:
        raise ProtocolError(f"客户端版本区间非法：[{lo}, {hi}]")
    chosen = min(PROTOCOL_MAX, hi)
    if chosen < lo or chosen < PROTOCOL_MIN or max(lo, PROTOCOL_MIN) > PROTOCOL_MAX:
        raise ProtocolError(
            f"协议版本不交叠：客户端 [{lo}, {hi}]，服务端 [{PROTOCOL_MIN}, {PROTOCOL_MAX}]"
        )
    return chosen


def request(
    method: str, params: dict[str, Any] | None = None, *, request_id: str
) -> dict[str, Any]:
    return {"id": request_id, "method": method, "params": params or {}}


def ok(request_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": request_id, "ok": True, "result": result or {}}


def fail(
    request_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"id": request_id, "ok": False, "error": error}


def event_push(
    seq: int,
    event_type: str,
    run_id: str,
    task_id: str | None,
    ts: int,
    payload: dict[str, Any],
    actor_kind: str = "core",
) -> dict[str, Any]:
    """事件推送帧。字段与 `EventView` 一致，UI 直接消费。"""
    return {
        "push": "event",
        "event": {
            "seq": seq,
            "type": event_type,
            "run_id": run_id,
            "task_id": task_id,
            "ts": ts,
            "actor_kind": actor_kind,
            "payload": payload,
        },
    }


# --------------------------------------------------------------------------- 服务端


class Server:
    """监听端点；`accept()` 返回一条连接。生命周期方法不抛业务异常。"""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.endpoint = endpoint_path(self.data_root)
        self.authkey = load_authkey(self.data_root)
        self._listener: Listener | None = None
        self._accepted: list[ServerConnection] = []
        #: accept 循环"已经真的进到 accept()"的线性化点。`start()` 只能保证
        #: listen；accept 是调用方（daemon）的线程，所以两者要分开证明——
        #: `on_ready` 必须晚于这两件事，否则就绪标记会骗人（issue #63）。
        self._accepting = threading.Event()

    @property
    def accepting(self) -> bool:
        return self._accepting.is_set()

    def wait_accepting(self, timeout: float | None = None) -> bool:
        """等 accept 线程真正进入 `accept()`。返回是否等到（超时返回 False）。"""
        return self._accepting.wait(timeout)

    def start(self) -> None:
        """建端点：**返回时一定已经 bind + listen**。

        顺序不能靠"调用方紧接着会 start accept 线程"来兜底：
        `multiprocessing.connection.Listener` 的构造函数内部同步执行
        `bind()` + `listen()`，所以 start() 返回时内核已经在替我们排队连接；
        但**排队 ≠ 能处理**，accept 线程是否进来由 `wait_accepting()` 证明。
        这两件事加在一起才是"端点能用"，缺一个都会让 `on_ready` 变成假话。
        """
        if self._listener is not None:
            raise IpcError("Server 已经 start 过了")
        if os.name == "nt":
            # 命名管道没有"文件是否存在"可看：同名管道还在 Listening 时，
            # 再 `CreateNamedPipe(..., FILE_FLAG_FIRST_PIPE_INSTANCE)` 会抛裸的
            # PermissionError（WinError 5）。先探一次，把"已有实例"翻译成
            # 可判别的 IpcError——调用方（daemon/Host/测试）只认识这个。
            if probe(self.data_root):
                raise IpcError(
                    f"端点已被占用：{self.endpoint}；"
                    "同一个 data_root 只能有一个 daemon（先 start 的那个还活着）"
                )
        else:
            run_dir(self.data_root).mkdir(parents=True, exist_ok=True)
            _chmod_private(run_dir(self.data_root), 0o700)
            stale = Path(self.endpoint)
            if stale.exists():
                if probe(self.data_root):
                    raise IpcError(
                        f"端点已被占用：{self.endpoint}；"
                        "同一个 data_root 只能有一个 daemon（先 start 的那个还活着）"
                    )
                # 连不上 = 上次崩溃留下的僵尸 socket，删掉才能 bind。
                stale.unlink(missing_ok=True)
            _chmod_private(run_dir(self.data_root), 0o700)
        self._listener = Listener(
            self.endpoint,
            family=_family(),
            authkey=self.authkey,
            # backlog 是**容量**不是调优：一条还没被 accept 的探活连接就能把
            # 默认的 1 填满，随后所有 connect 都会在 macOS 上被拒（#63）。
            backlog=LISTEN_BACKLOG,
        )
        if os.name != "nt":
            _chmod_private(Path(self.endpoint), 0o600)

    def accept(self) -> ServerConnection:
        """阻塞等下一条连接。

        三种情况都会转成 **可重试** 的 `ProtocolError`，调用方的 accept 循环
        应当继续等下一条，而不是退出：

        - 认证失败（authkey 不对）——别人的 App 或者旧安装包；
        - 握手前就断开——`probe()` 的探活连接就是这样，属于正常现象；
        - 管道/socket 被对手方重置。
        """
        if self._listener is None:
            raise IpcError("Server 还没 start")
        # 先置 event 再阻塞：`on_ready` 的判据是"这条线程已经站在 accept 里了"，
        # 不是"线程对象 start 过了"。
        self._accepting.set()
        try:
            conn = self._listener.accept()
        except (AuthenticationError, EOFError, OSError) as exc:
            raise ProtocolError(f"连接在握手阶段失败（可重试）：{exc}") from exc
        wrapped = ServerConnection(conn)
        self._accepted.append(wrapped)
        return wrapped

    def _wake_accept(self) -> None:
        """"敲一下"端点，把阻塞在 `accept()` 的线程叫醒。

        不能只靠关 listener：

        - Windows：accept 阻塞在 `ConnectNamedPipe` 的 overlapped I/O 上，从别的
          线程关句柄**不会取消**挂起的 I/O——accept 线程永远卡住，命名管道对象
          也一直存在（表现：daemon 已退出，`probe()` 却仍为真，下一个实例被拒）；
        - Linux：关掉正在被 `accept()` 使用的 fd 也不会可靠唤醒阻塞中的 syscall，
          线程会一直等到下一次连接到来（实测：CI 上这条测试红）。

        这条唤醒连接只用于打断，客户端会立刻关闭，服务端握手失败按"可重试"处理。
        """

        def knock() -> None:
            try:
                if os.name == "nt":
                    handle = os.open(self.endpoint, os.O_RDWR)
                    os.close(handle)
                else:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        sock.settimeout(0.5)
                        sock.connect(self.endpoint)
                    finally:
                        sock.close()
            except OSError:
                # 端点已经不存在（还没 bind / 已被别人清掉）：没有要唤醒的线程。
                pass

        # 唤醒动作本身**不能**阻塞 `close()`：Windows 上 `CreateFile` 在管道实例
        # 全忙时可能长时间不返回，那会把"关服务端"变成一个新的卡点（实测：CI 的
        # windows job 卡在 Pytest 里 20 分钟没结束）。放进短线程里，等不到就放弃——
        # 唤醒失败最坏是 accept 线程多活一会儿（它是 daemon 线程），不该拖住退出。
        worker = threading.Thread(target=knock, name="ipc-wake-accept", daemon=True)
        worker.start()
        worker.join(1.0)

    def close(self) -> None:
        # 先关已接受的连接：只关 listener 不会唤醒阻塞在 recv 上的连接线程，
        # 那会让 shutdown 卡到超时（daemon 的退出路径上最不该卡的地方）。
        for conn in self._accepted:
            conn.close()
        self._accepted.clear()
        if self._listener is not None:
            # 关 listener 之前先叫醒可能正阻塞在 accept() 的线程（见 _wake_accept）。
            self._wake_accept()
        if self._listener is not None:
            try:
                self._listener.close()
            finally:
                self._listener = None
        if os.name != "nt":
            try:
                Path(self.endpoint).unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> Server:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ServerConnection:
    """服务端看到的客户端连接。

    `send` / `recv` 不是线程安全的（`multiprocessing.connection` 的连接对象
    本来就不保证）。daemon 用一把 `_send_lock` 串行化发送——推送线程与响应线程
    会同时写同一条连接，不加锁会让两帧字节交错，客户端直接解析失败。
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._eof = False
        self.client: dict[str, Any] = {}
        self.subscribed = False
        self.since_seq = 0

    def send(self, message: dict[str, Any]) -> None:
        self._conn.send_bytes(encode(message))

    def recv(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """读一条消息；超时返回 None，对端关闭也返回 None **但会置 `eof`**。

        「超时」和「断线」都返回 None 是有歧义的：读循环如果只判 None，
        对端一断开 `poll()` 就立刻报告"可读"、`recv_bytes()` 立刻 EOF，
        循环会以 100% CPU 空转（实测：演示里客户端断开后，daemon 的读线程
        把一整个核烤满，见 `docs/M5-desktop.md §10`）。所以断线必须留下
        可判别的 `eof`，让读循环据此退出，而不是继续下一轮。

        没有超时默认值：调用方要么无限等（阻塞线程），要么自己用
        `poll()` 先判可读。daemon 的连接线程用带超时的等，shutdown 时
        由关闭连接打断。
        """
        if self._eof:
            return None
        try:
            if timeout is not None and not self._conn.poll(timeout):
                return None
            raw = self._conn.recv_bytes()
        except EOFError:
            self._eof = True
            return None
        except OSError:
            # Windows 上"对端已关"不一定表现为 poll() 返回 True 再 recv EOF：
            # `PeekNamedPipe` 会直接抛 BrokenPipeError / OSError。语义一样是断线。
            self._eof = True
            return None
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ProtocolError(f"消息 {len(raw)} 字节，超过上限 {MAX_MESSAGE_BYTES}")
        return decode(raw)

    @property
    def eof(self) -> bool:
        """对端已断开（或本地已关）：之后 `recv()` 永远等不到消息。"""
        return self._eof or bool(self._conn.closed)

    @property
    def closed(self) -> bool:
        return bool(self._conn.closed)

    def close(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass

    def abort(self) -> None:
        """**中断**这条连接，然后关闭：用于叫醒阻塞在 send/recv 上的线程。

        与 `close()` 的区别：`close()` 是正常关闭（对端读 EOF）；`abort()` 还会
        把套接字 shutdown 掉，让**本端**正阻塞在 `send_bytes` 里的线程立刻收到
        `EPIPE/EBADF` 而不是继续挂在一次写不出去的发送上——慢订阅者被判定放弃时
        需要这个语义，否则发送线程会一直占着一个线程不放。

        POSIX：`shutdown(SHUT_RDWR)` 作用在 `dup()` 出来的同一套接字上（socket
        选项/状态按底层 socket 共享），`dup` 的 fd 单独关闭，不影响原连接对象。
        Windows：命名管道没有 shutdown 语义，只能直接 `close()`；阻塞中的
        `WriteFile` 是否被唤醒由系统决定，调用方不应依赖线程立刻退出。
        """
        if os.name != "nt":
            fd: int | None = None
            try:
                fd = os.dup(self._conn.fileno())
            except (AttributeError, OSError, ValueError):
                fd = None
            if fd is not None:
                try:
                    sock = socket.socket(fileno=fd)
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                finally:
                    sock.close()  # 只关 dup 出来的 fd；原连接仍由 self._conn 持有
        self.close()


# --------------------------------------------------------------------------- 客户端


class Client:
    """CLI / Host / 测试共用的客户端。

    一个 `Client` 对应一条连接。**不要**在多线程里共用：`call()` 是"发一条、
    等自己那条"，两个线程同时用会把响应收串。需要并发的场景就开两条连接
    （UI 的 SSE 与 RPC 本来就是两条）。
    """

    def __init__(self, data_root: Path, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.data_root = Path(data_root)
        self.timeout = timeout
        self._conn: Any = None
        self._next_id = 0
        self._pushes: list[dict[str, Any]] = []
        self.welcome: dict[str, Any] = {}

    # -- 生命周期 --

    def connect(self, *, timeout: float | None = None) -> dict[str, Any]:
        """连上并完成 hello 握手，返回 welcome。"""
        if self._conn is not None:
            raise IpcError("Client 已经连接过了")
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        last: Exception | None = None
        # daemon 刚起来时 socket 可能已经 bind 但还没 listen；短退避重试几次，
        # 让"host 拉起 daemon 后立刻连"不用在调用方再包一层轮询。
        while True:
            try:
                self._conn = _Socket(
                    endpoint_path(self.data_root),
                    family=_family(),
                    authkey=load_authkey(self.data_root),
                )
                break
            except (OSError, AuthenticationError) as exc:
                last = exc
                if time.monotonic() >= deadline:
                    raise ConnectionClosed(
                        f"连不上守护进程（{self.data_root}）：{last}"
                    ) from last
                time.sleep(0.05)
        try:
            self.welcome = self.call(
                "hello",
                {
                    "protocol_min": PROTOCOL_MIN,
                    "protocol_max": PROTOCOL_MAX,
                    "client": f"client/{os.getpid()}",
                    "pid": os.getpid(),
                },
                timeout=budget,
            )
        except BaseException:
            self.close()
            raise
        return self.welcome

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def __enter__(self) -> Client:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 请求 --

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._conn is None:
            raise ConnectionClosed("Client 未连接")
        self._next_id += 1
        request_id = f"c{self._next_id}"
        self._conn.send_bytes(encode(request(method, params, request_id=request_id)))
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        while True:
            message = self._receive(deadline)
            if "push" in message:
                # 推送随时会插进来（事件推送与响应共用一条连接）。存起来，
                # 交给 next_event()，不能让 call() 因为"收到了别的东西"而失败。
                self._pushes.append(message)
                continue
            if message.get("id") != request_id:
                raise ProtocolError(
                    f"响应 id 不匹配：期望 {request_id}，收到 {message.get('id')!r}"
                )
            if message.get("ok"):
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            error = message.get("error") or {}
            raise RemoteError(
                str(error.get("code") or "unknown"),
                str(error.get("message") or ""),
                error.get("details") if isinstance(error.get("details"), dict) else {},
            )

    def _receive(self, deadline: float) -> dict[str, Any]:
        if self._conn is None:
            raise ConnectionClosed("Client 未连接")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionClosed("等待响应超时")
            try:
                if not self._conn.poll(remaining):
                    continue
                raw = self._conn.recv_bytes()
            except (EOFError, OSError) as exc:
                raise ConnectionClosed(f"连接已断开：{exc}") from exc
            if len(raw) > MAX_MESSAGE_BYTES:
                raise ProtocolError(f"消息 {len(raw)} 字节，超过上限 {MAX_MESSAGE_BYTES}")
            return decode(raw)

    # -- 推送 --

    def subscribe(self, since_seq: int = 0) -> dict[str, Any]:
        """订阅事件推送；服务端先补 `seq >= since_seq` 的历史，再推增量。"""
        result = self.call("subscribe", {"since_seq": since_seq})
        self._subscribed_from = int(result.get("from_seq", since_seq))
        return result

    def next_event(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        """取下一条推送：**超时**返回 None，**连接断开**抛 `ConnectionClosed`。

        先交还 `call()` 期间攒下的推送（顺序保持原样），再从连接上读。

        这里不能把断开也返回 None：订阅方的循环（host 的 SSE）靠 None 判
        "这一刻没事件"，断开后 None 会立刻再次返回，循环就变成空转。
        断开是终态，必须是异常，订阅方才能干净地结束流、让客户端重连。
        """
        if self._pushes:
            return self._pushes.pop(0)
        budget = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if not self._conn.poll(remaining):
                    return None
                raw = self._conn.recv_bytes()
            except (EOFError, OSError) as exc:
                raise ConnectionClosed(f"连接已断开：{exc}") from exc
            except AttributeError as exc:
                raise ConnectionClosed("Client 未连接") from exc
            message = decode(raw)
            if "push" in message:
                return message
            # 订阅连接上不该出现响应；出现说明调用方把两种用途混在一条连接，
            # 明确报错比静默丢弃好。
            raise ProtocolError(f"订阅连接上收到非推送消息：{message!r}")


__all__ = [
    "AUTHKEY_NAME",
    "DEFAULT_TIMEOUT",
    "MAX_ENDPOINT_BYTES",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_MAX",
    "PROTOCOL_MIN",
    "Client",
    "ConnectionClosed",
    "IpcError",
    "ProtocolError",
    "RemoteError",
    "Server",
    "ServerConnection",
    "authkey_path",
    "decode",
    "default_data_root",
    "encode",
    "endpoint_path",
    "event_push",
    "fail",
    "load_authkey",
    "negotiate",
    "ok",
    "probe",
    "request",
    "run_dir",
]