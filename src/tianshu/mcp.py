"""MCP 客户端（stdio）：把「用户配置的 MCP server」变成「进程 + 一次调用」。

## 这一层只做一件事：与外部 MCP server 说清楚话

写入授权的判定、审批、任务白名单、事件、凭据都在派发层
（`dispatcher.py`）；准入（schema 方言、工具 id 映射、版本绑定）在
`tool_registry.py`。这里只负责：

1. 按用户给的**配置**拉起 server 进程（stdio / 换行分隔 JSON-RPC 2.0）；
2. 握手（`initialize` → `notifications/initialized`）、发现（`tools/list` 分页）；
3. 一次调用（`tools/call`）与被取消时的 `notifications/cancelled`；
4. 如实返回：协议版本、工具原样 schema、内容块、`isError`、耗时、诊断。

## 刻意不做的事（每一条都是"外发/执行面"的收口）

- **不读模型、不读任务参数**：server 列表只来自用户配置（`TIANSHU_MCP_CONFIG`
  或装配点显式注入）。装一个 server 就是用户的一次信任决定。
- **不继承环境变量**：子进程只拿到 `PATH`（Windows 另加 `SYSTEMROOT`）加上配置里
  显式声明的键。父进程里的密钥不会被顺手泄给第三方 server。
- **不重试、不自动重启、不降级协议**：握手失败 / 版本不认识 / 读到畸形帧，就把这个
  server 标成 `unavailable`，并留下原因；**不**偷偷换版本、不换启动命令。
- **不做授权判断**：没有 `allowed` 之类的东西。这里的每一次 `tools/call` 都假定
  调用方（派发层）已经完成授权；本层不提供"跳过审批"的入口。
- **不做沙箱**：进程以当前用户权限运行。`via_exec_backend=False` 是如实标注，
  不是"后端覆盖不到它但没关系"。

## 诚实边界

- 协议形状按 MCP 2025-06-18 的已知口径实现（stdio、`initialize`、`tools/list`、
  `tools/call`、`notifications/cancelled`），**未在官方页面逐条复核**；认不出的
  版本/形状一律拒绝，真实第三方 server 兼容性**未验证**。
- 只支持 stdio；HTTP/SSE、OAuth、`resources/*`、`prompts/*`、`sampling/*`、
  `roots/*`、`list_changed` 都不做。
- 同一 server 的请求**串行**（每 server 一把请求锁）：协议允许并发，但 v1 的
  调用方是单线程主循环，串行能把"响应配对错误"变成不可能。
- 取消只是**请求**：server 可以不理会，已经执行的副作用收不回来。
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import TianshuError

__all__ = [
    "MCP_CALL_TIMEOUT_MS",
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    "MCP_CONFIG_ENV",
    "MCP_DEFAULT_MAX_RESULT_BYTES",
    "MCP_DEFAULT_MAX_TOOLS",
    "MCP_MAX_MESSAGE_BYTES",
    "MCP_PROTOCOL_VERSIONS",
    "McpCallError",
    "McpCallResult",
    "McpClients",
    "McpClosed",
    "McpConfigError",
    "McpError",
    "McpProtocolError",
    "McpServerConfig",
    "McpServerState",
    "McpTimeout",
    "McpTool",
    "StdioServer",
    "load_mcp_config",
    "parse_mcp_config",
]

#: 用户配置文件的路径（进程级；与 `TIANSHU_EXEC_BACKEND` 同一类边界）。
MCP_CONFIG_ENV = "TIANSHU_MCP_CONFIG"

#: 认识的协议版本。server 回一个不在集合里的版本 → 这个 server 不可用。
#: 集合之外的"更新版本"不猜、不降级——读不懂就报错。
MCP_PROTOCOL_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18")
#: 客户端在 `initialize` 里声明的版本（最新一个我们认识的）。
MCP_DEFAULT_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[-1]
MCP_CLIENT_NAME = "tianshu-agent"
MCP_CLIENT_VERSION = "1.0"

#: 单条消息上限（读与写各自）。超限按协议错误处理。
MCP_MAX_MESSAGE_BYTES = 1_048_576
#: 握手 / 分页的默认超时。
MCP_HANDSHAKE_TIMEOUT_MS = 10_000
#: 单次 `tools/call` 的默认超时与硬上限。
MCP_CALL_TIMEOUT_MS = 30_000
MCP_MAX_CALL_TIMEOUT_MS = 600_000
#: 一次调用最多收多少字节的结果正文（派发层再按内容引用截断）。
MCP_DEFAULT_MAX_RESULT_BYTES = 65_536
MCP_MAX_RESULT_BYTES = 1_048_576
#: 每个 server 默认最多发现多少工具（准入层按此拒绝超额工具）。
MCP_DEFAULT_MAX_TOOLS = 64
MCP_MAX_TOOLS = 256
#: stderr 尾部保留多少字节（诊断用；不参与协议）。
MCP_STDERR_TAIL_BYTES = 8_192
#: 关闭时的宽限：先 terminate，等这么久再 kill。
MCP_CLOSE_GRACE_MS = 2_000
#: `tools/list` 最多翻几页（防 server 用无尽 nextCursor 拖死握手）。
MCP_MAX_LIST_PAGES = 32
#: 每次从 stdout 读多少字节（同时是"单条消息上限"的检查粒度）。
_MCP_READ_CHUNK = 65_536
#: 取消是尽力通知；不能让已经超时的调用再次等待完整调用期限。
_MCP_CANCEL_SEND_GRACE_MS = 100

#: 协议层"请求没有被当成工具调用执行"的错误码（详见 SPEC §2.4）。
_PROTOCOL_REJECTION_CODES = frozenset({-32700, -32600, -32601, -32602})

_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_MAX_NAME = 64
_SERVER_CONFIG_KEYS = frozenset(
    {
        "command",
        "cwd",
        "env",
        "handshake_timeout_ms",
        "call_timeout_ms",
        "max_result_bytes",
        "max_tools",
    }
)


# ---------------------------------------------------------------- 异常


class McpError(TianshuError):
    """MCP 客户端层的失败基类。"""


class McpConfigError(McpError):
    """用户配置不合法（fail-closed，报 server 名 + 字段 + 原因）。"""


class McpServerUnavailable(McpError):
    """这个 server 起不来 / 握手失败 / 已经失效。"""

    def __init__(self, message: str, *, server: str = "", diagnostics: str = "") -> None:
        super().__init__(message)
        self.server = server
        self.diagnostics = diagnostics


class McpProtocolError(McpError):
    """协议层错误：畸形 JSON、id 对不上、版本不认识、分页越界。"""


class McpTimeout(McpError):
    """发送或等响应超时。**副作用未知**：请求可能已经被 server 执行。"""

    def __init__(self, message: str, *, request_id: int = 0) -> None:
        super().__init__(message)
        self.request_id = request_id


class McpClosed(McpError):
    """管道断开 / server 退出。**副作用未知**。"""


class McpCallError(McpError):
    """`tools/call` 收到 JSON-RPC 错误响应。

    `protocol_rejection` 为真表示"请求没有被当成工具调用执行"（解析/方法/
    参数错误），派发层据此记 `none_applied`；其余错误码按 `unknown` 处理。
    """

    def __init__(self, message: str, *, code: int = -32603, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @property
    def protocol_rejection(self) -> bool:
        return self.code in _PROTOCOL_REJECTION_CODES


# ---------------------------------------------------------------- 配置


def _require_int(raw: Any, *, field_name: str, server: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise McpConfigError(f"server {server!r} 的 {field_name} 必须是整数：{raw!r}")
    if raw < minimum or raw > maximum:
        raise McpConfigError(
            f"server {server!r} 的 {field_name} 必须在 [{minimum}, {maximum}]：{raw}"
        )
    return raw


def _require_absolute(argv0: str, *, server: str) -> None:
    if not argv0 or "\x00" in argv0:
        raise McpConfigError(f"server {server!r} 的 argv[0] 不能为空或含 NUL")
    if not os.path.isabs(argv0):
        raise McpConfigError(
            f"server {server!r} 的 argv[0] 必须是绝对路径（不走 PATH 搜索，"
            f"否则同名二进制被替换就等于换了一个 server）：{argv0!r}"
        )


@dataclass(frozen=True)
class McpServerConfig:
    """一个 MCP server 的用户配置（不可变，构造期校验）。"""

    name: str
    command: tuple[str, ...]
    cwd: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    handshake_timeout_ms: int = MCP_HANDSHAKE_TIMEOUT_MS
    call_timeout_ms: int = MCP_CALL_TIMEOUT_MS
    max_result_bytes: int = MCP_DEFAULT_MAX_RESULT_BYTES
    max_tools: int = MCP_DEFAULT_MAX_TOOLS

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise McpConfigError(
                f"server 名必须匹配 {_NAME_RE.pattern}（资源引用与工具 id 都依赖它）："
                f"{self.name!r}"
            )
        command = tuple(self.command)
        if not command or not all(isinstance(part, str) and part for part in command):
            raise McpConfigError(f"server {self.name!r} 的 command 必须是非空字符串数组")
        _require_absolute(command[0], server=self.name)
        if self.cwd:
            if not isinstance(self.cwd, str) or not os.path.isabs(self.cwd):
                raise McpConfigError(
                    f"server {self.name!r} 的 cwd 必须是绝对路径或空：{self.cwd!r}"
                )
        env: dict[str, str] = {}
        for key, value in dict(self.env or {}).items():
            if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
                raise McpConfigError(f"server {self.name!r} 的 env 键不合法：{key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise McpConfigError(
                    f"server {self.name!r} 的 env[{key!r}] 必须是字符串且不含 NUL"
                )
            env[key] = value
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "env", env)
        for field_name, minimum, maximum in (
            ("handshake_timeout_ms", 1, MCP_MAX_CALL_TIMEOUT_MS),
            ("call_timeout_ms", 1, MCP_MAX_CALL_TIMEOUT_MS),
            ("max_result_bytes", 1, MCP_MAX_RESULT_BYTES),
            ("max_tools", 1, MCP_MAX_TOOLS),
        ):
            value = getattr(self, field_name)
            _require_int(
                value,
                field_name=field_name,
                server=self.name,
                minimum=minimum,
                maximum=maximum,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "env": dict(self.env),
            "handshake_timeout_ms": self.handshake_timeout_ms,
            "call_timeout_ms": self.call_timeout_ms,
            "max_result_bytes": self.max_result_bytes,
            "max_tools": self.max_tools,
        }

    @classmethod
    def from_dict(cls, name: str, raw: Any) -> McpServerConfig:
        if not isinstance(raw, Mapping):
            raise McpConfigError(f"server {name!r} 的配置必须是对象")
        unknown = set(raw) - _SERVER_CONFIG_KEYS
        if unknown:
            raise McpConfigError(
                f"server {name!r} 含不认识的字段 {sorted(unknown)}"
                f"（只认 {sorted(_SERVER_CONFIG_KEYS)}；配置里不许写 shell/URL/传输）"
            )
        command = raw.get("command")
        if not isinstance(command, (list, tuple)):
            raise McpConfigError(f"server {name!r} 的 command 必须是字符串数组")
        env = raw.get("env", {})
        if not isinstance(env, Mapping):
            raise McpConfigError(f"server {name!r} 的 env 必须是对象")
        kwargs: dict[str, Any] = {"name": name, "command": tuple(command), "env": dict(env)}
        for key in ("cwd",):
            if key in raw and not isinstance(raw[key], str):
                raise McpConfigError(f"server {name!r} 的 {key} 必须是字符串")
            kwargs[key] = raw.get(key, "")
        for key in (
            "handshake_timeout_ms",
            "call_timeout_ms",
            "max_result_bytes",
            "max_tools",
        ):
            if key in raw:
                kwargs[key] = raw[key]
        return cls(**kwargs)


def parse_mcp_config(raw: Any) -> dict[str, McpServerConfig]:
    """解析整份配置。**整份拒绝**（不是跳过坏的那一条）：配置错了就启动期报错。"""
    if not isinstance(raw, Mapping):
        raise McpConfigError("MCP 配置根必须是对象：{\"servers\": {...}}")
    unknown = set(raw) - {"servers"}
    if unknown:
        raise McpConfigError(f"MCP 配置含不认识的键 {sorted(unknown)}（只认 servers）")
    servers = raw.get("servers", {})
    if not isinstance(servers, Mapping):
        raise McpConfigError("MCP 配置的 servers 必须是对象")
    configs: dict[str, McpServerConfig] = {}
    for name, item in servers.items():
        if not isinstance(name, str):
            raise McpConfigError(f"server 名必须是字符串：{name!r}")
        configs[name] = McpServerConfig.from_dict(name, item)
    return configs


def load_mcp_config(path: str | os.PathLike[str]) -> dict[str, McpServerConfig]:
    """从 JSON 文件读配置。不存在 / 不是合法 JSON / 结构不合法 → `McpConfigError`。"""
    target = Path(path)
    try:
        # 字节进字节出：Windows 上文本模式会翻 CRLF，内容与 POSIX 不一致
        # （`tests/test_windows_compat.py` 对产品侧文本 IO 有 AST 判据）。
        text = target.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise McpConfigError(f"MCP 配置文件不存在：{target}") from exc
    except OSError as exc:
        raise McpConfigError(f"MCP 配置文件读不出来：{target}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise McpConfigError(f"MCP 配置文件不是 UTF-8：{target}（{exc}）") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"MCP 配置文件不是合法 JSON：{target}（{exc}）") from exc
    return parse_mcp_config(raw)


def config_from_env(env: Mapping[str, str] | None = None) -> dict[str, McpServerConfig]:
    """从进程环境读配置；没设 `TIANSHU_MCP_CONFIG` → 空（= 不启用 MCP）。"""
    table = os.environ if env is None else env
    raw_path = table.get(MCP_CONFIG_ENV, "").strip()
    if not raw_path:
        return {}
    if not os.path.isabs(raw_path):
        raise McpConfigError(
            f"{MCP_CONFIG_ENV} 必须是绝对路径（相对路径会随进程 cwd 变化指向另一份"
            f"配置，等于换了一组 server）：{raw_path!r}"
        )
    return load_mcp_config(raw_path)


# ---------------------------------------------------------------- 工具与结果


@dataclass(frozen=True)
class McpTool:
    """一个 server 声明的工具，**原样**保留（准入层要按原样算摘要）。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Any) -> McpTool:
        if not isinstance(raw, dict):
            raise McpProtocolError(f"tools/list 里的工具必须是对象：{raw!r}")
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise McpProtocolError(f"工具缺少合法的 name：{raw!r}")
        description = raw.get("description")
        if not isinstance(description, str):
            description = ""
        schema = raw.get("inputSchema")
        return cls(
            name=name,
            description=description,
            input_schema=schema if isinstance(schema, dict) else {},
            raw=dict(raw),
        )


@dataclass(frozen=True)
class McpCallResult:
    """一次 `tools/call` 拿到的事实（不含授权判断）。"""

    request_id: int
    content: tuple[dict[str, Any], ...]
    is_error: bool
    structured: Any
    duration_ms: int
    #: 文本正文**实际收到**的字节数（截断前）。
    bytes: int = 0
    #: 是否因为超过 `max_result_bytes` 被截断。
    truncated: bool = False
    #: 被截掉的字节数（只统计文本）。
    dropped_bytes: int = 0
    #: 因为上限被丢弃的非文本块数量。
    dropped_blocks: int = 0

    @property
    def text_parts(self) -> tuple[str, ...]:
        """文本块（其它类型由调用方如实记成不支持，不在这里丢弃）。"""
        texts: list[str] = []
        for item in self.content:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return tuple(texts)

    @property
    def unsupported_types(self) -> tuple[str, ...]:
        kinds: list[str] = []
        for item in self.content:
            kind = item.get("type")
            if kind != "text" and isinstance(kind, str):
                kinds.append(kind)
        return tuple(kinds)


@dataclass(frozen=True)
class McpServerState:
    """给控制面 / 诊断看的 server 快照。"""

    name: str
    status: str  # ready | unavailable
    protocol_version: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    tools: tuple[McpTool, ...] = ()
    diagnostics: tuple[str, ...] = ()
    pid: int | None = None
    #: server 声明的工具比 `max_tools` 多、被截断了吗（"没有这个工具"与
    #: "被我们截掉了"必须能分开）。
    tools_truncated: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def _clip_blocks(
    blocks: tuple[dict[str, Any], ...], *, limit: int
) -> tuple[tuple[dict[str, Any], ...], int, int, int, int]:
    """按 UTF-8 字节上限裁剪结果块。

    返回 `(kept, total_text_bytes, kept_text_bytes, dropped_text_bytes,
    dropped_block_count)`：
    `total_text_bytes` 是**实际收到**的文本字节（截断前），所以事件里能如实写
    "收到多少、留下多少、丢了多少"，而不是只给一个 `truncated=true`。

    截断按 UTF-8 字符边界切（多字节字符不会被劈成半个）。非文本块（图片/资源）
    本身不收正文，超过上限后不再保留、按个数记。
    """
    kept: list[dict[str, Any]] = []
    total_bytes = 0
    kept_bytes = 0
    dropped_bytes = 0
    dropped_blocks = 0
    overflow = False
    for item in blocks:
        raw_text = item.get("text") if item.get("type") == "text" else None
        if isinstance(raw_text, str):
            raw = raw_text.encode("utf-8")
            total_bytes += len(raw)
            if overflow:
                dropped_bytes += len(raw)
                continue
            room = limit - kept_bytes
            if len(raw) <= room:
                kept.append(item)
                kept_bytes += len(raw)
                continue
            cut = raw[:room].decode("utf-8", errors="ignore")
            kept.append({**item, "text": cut})
            kept_bytes += len(cut.encode("utf-8"))
            dropped_bytes += len(raw) - len(cut.encode("utf-8"))
            overflow = True
            continue
        if overflow:
            dropped_blocks += 1
            continue
        kept.append(item)
    return tuple(kept), total_bytes, kept_bytes, dropped_bytes, dropped_blocks


# ---------------------------------------------------------------- 进程


class StdioServer:
    """一个 stdio MCP server：进程 + 读线程 + 请求/响应配对。

    生命周期：`start()` → `request()` / `call_tool()` → `close()`。
    任何协议层异常都会让这个实例**失效**（`is_alive` 变 False），不自动重试。
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        default_cwd: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        self.config = config
        self._default_cwd = str(default_cwd) if default_cwd is not None else ""
        self._environ = environ
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._responses: queue.Queue[tuple[int | None, dict[str, Any]]] = queue.Queue()
        #: 已经超时放弃的请求 id：它们的响应可能**晚到**，晚到不能算"id 不匹配"，
        #: 否则一个健康的 server 会因为一次超时被下一次请求判死。
        #: 有界（FIFO）：server 永不回应时每个超时 id 都留在这里会无界增长；
        #: 只保留最近 64 个——比这还晚的响应本身就该按协议错误处理。
        self._abandoned: set[int] = set()
        self._abandoned_order: collections.deque[int] = collections.deque()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._next_id = 0
        self._last_request_id = 0
        self._closed = False
        self._failure = ""
        self._stderr_tail = bytearray()
        self._protocol_version = ""
        self._server_info: dict[str, Any] = {}
        self._tools_truncated = False
        self._capabilities: dict[str, Any] = {}
        self._tools: tuple[McpTool, ...] = ()
        self._notifications = 0

    # ---- 属性

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_alive(self) -> bool:
        if self._closed or self._failure or self._proc is None:
            return False
        return self._proc.poll() is None

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def tools(self) -> tuple[McpTool, ...]:
        return self._tools

    @property
    def tools_truncated(self) -> bool:
        """server 声明的工具比 `max_tools` 多、被截断了吗（诊断用）。"""
        return self._tools_truncated

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    def exit_code(self) -> int | None:
        """进程的退出码；还在跑 / 没启动过 → None。测试与诊断用它确认"真的收尸了"。"""
        return None if self._proc is None else self._proc.poll()

    @property
    def notifications_seen(self) -> int:
        return self._notifications

    @property
    def pending_timeouts(self) -> int:
        """还在等"晚到响应"的超时请求数（有界，见 `_abandon`）。"""
        return len(self._abandoned)

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            raw = bytes(self._stderr_tail)
        return raw.decode("utf-8", errors="replace")

    def diagnostics(self) -> str:
        """一行诊断：失败原因 + stderr 尾部（有界）。"""
        parts = [self._failure or "ok"]
        tail = self.stderr_tail().strip()
        if tail:
            parts.append(f"stderr: {tail[-600:]}")
        return "；".join(parts)

    # ---- 启动 / 关闭

    def child_env(self) -> dict[str, str]:
        """子进程环境：只给 PATH（Windows 另加 SYSTEMROOT）+ 配置显式声明的键。"""
        table = os.environ if self._environ is None else self._environ
        base: dict[str, str] = {}
        if "PATH" in table:
            base["PATH"] = table["PATH"]
        if sys.platform == "win32" and "SYSTEMROOT" in table:
            base["SYSTEMROOT"] = table["SYSTEMROOT"]
        base.update(self.config.env)
        return base

    def start(self) -> None:
        """拉起进程、握手、发现工具。失败抛 `McpServerUnavailable`，并自我标记失效。"""
        if self._proc is not None:
            raise McpError("server 已经启动过；一个实例只服务一次生命周期")
        cwd = self.config.cwd or self._default_cwd or None
        if cwd is not None and not Path(cwd).is_dir():
            raise McpServerUnavailable(
                f"server {self.name!r} 的 cwd 不存在或不是目录：{cwd}",
                server=self.name,
            )
        try:
            self._proc = subprocess.Popen(
                list(self.config.command),
                cwd=cwd,
                env=self.child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise McpServerUnavailable(
                f"server {self.name!r} 拉不起来：{type(exc).__name__}: {exc}",
                server=self.name,
            ) from exc
        try:
            assert self._proc.stdin is not None
            # Python 3.12 在 Windows 也支持非阻塞 pipe。期限必须覆盖写入本身：
            # 对端完成握手后不再读 stdin 时，阻塞 write 永远到不了响应计时器。
            os.set_blocking(self._proc.stdin.fileno(), False)
        except OSError as exc:
            self._failure = f"配置非阻塞 stdin 失败：{exc}"
            self.close()
            raise McpServerUnavailable(self._failure, server=self.name) from exc
        self._reader = threading.Thread(
            target=self._read_loop, name=f"mcp-{self.name}-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, name=f"mcp-{self.name}-stderr", daemon=True
        )
        self._stderr_reader.start()
        try:
            self._handshake()
            self._discover_tools()
        except (McpError, OSError) as exc:
            self._failure = str(exc)
            self.close()
            raise McpServerUnavailable(
                f"server {self.name!r} 不可用：{exc}",
                server=self.name,
                diagnostics=self.diagnostics(),
            ) from exc

    def close(self) -> None:
        """尽力回收：关 stdin → terminate → 宽限 → kill。幂等。"""
        self._closed = True
        proc = self._proc
        if proc is not None:
            stream = proc.stdin
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                deadline = time.monotonic() + MCP_CLOSE_GRACE_MS / 1000
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        # 唤醒可能在等的调用方
        self._responses.put((None, {}))

    # ---- 请求

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if not self.is_alive:
            raise McpClosed(f"server {self.name!r} 已失效：{self._failure or 'closed'}")
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(
        self, method: str, params: dict[str, Any] | None, *, timeout_ms: int
    ) -> dict[str, Any]:
        if not self.is_alive:
            raise McpClosed(f"server {self.name!r} 已失效：{self._failure or 'closed'}")
        deadline = time.monotonic() + timeout_ms / 1000
        if not self._request_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise McpTimeout(f"server {self.name!r} 等待前一请求结束超时")
        try:
            request_id = self._next_id
            self._next_id += 1
            self._last_request_id = request_id
            message: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                message["params"] = params
            self._send(message, deadline=deadline)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._abandon(request_id, method)
                    raise McpTimeout(
                        f"server {self.name!r} 在 {timeout_ms}ms 内没有响应 {method}"
                        f"（request_id={request_id}）",
                        request_id=request_id,
                    )
                try:
                    got_id, payload = self._responses.get(timeout=remaining)
                except queue.Empty:
                    self._abandon(request_id, method)
                    raise McpTimeout(
                        f"server {self.name!r} 在 {timeout_ms}ms 内没有响应 {method}"
                        f"（request_id={request_id}）",
                        request_id=request_id,
                    ) from None
                if got_id is None:
                    raise McpClosed(
                        f"server {self.name!r} 已断开：{self._failure or 'closed'}"
                    )
                if got_id in self._abandoned:
                    self._abandoned.discard(got_id)
                    continue
                if got_id != request_id:
                    reason = (
                        f"server {self.name!r} 回了不属于当前请求的响应"
                        f"（id={got_id}，期望 {request_id}）"
                    )
                    self._fail(reason)
                    raise McpProtocolError(reason)
                if "error" in payload:
                    error = payload["error"]
                    if isinstance(error, dict):
                        code = error.get("code", -32603)
                        message_text = error.get("message", "")
                        data = error.get("data")
                    else:  # pragma: no cover - 畸形 error 形状
                        code, message_text, data = -32603, str(error), None
                    raise McpCallError(
                        f"{method} 被 server {self.name!r} 拒绝：{message_text}"
                        f"（code={code}）",
                        code=code if isinstance(code, int) else -32603,
                        data=data,
                    )
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise McpProtocolError(
                        f"server {self.name!r} 的 {method} 结果不是对象"
                    )
                return result
        finally:
            self._request_lock.release()

    def _abandon(self, request_id: int, method: str) -> None:
        """一个请求超时了：记下它的 id（晚到的响应要丢掉），并尽力通知 server 取消。

        取消**只是请求**（server 可以不理会）：这里不假装副作用被撤回，调用方按
        "副作用未知"结算。发不出去（连接已断）也不改变超时的结论。
        """
        if request_id not in self._abandoned:
            while len(self._abandoned_order) >= 64:
                self._abandoned.discard(self._abandoned_order.popleft())
            self._abandoned.add(request_id)
            self._abandoned_order.append(request_id)
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": f"{method} timeout in client"},
                },
                deadline=time.monotonic() + _MCP_CANCEL_SEND_GRACE_MS / 1000,
            )
        except McpError:
            pass

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        timeout_ms: int | None = None,
        call_id: str = "",
    ) -> McpCallResult:
        """执行一次工具。`McpCallError` / `McpTimeout` / `McpClosed` 由调用方翻译。"""
        limit = self.config.call_timeout_ms if timeout_ms is None else timeout_ms
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise McpError(f"timeout_ms 必须是正整数：{limit!r}")
        started = time.monotonic()
        request_method = "tools/call"
        params = {"name": tool, "arguments": arguments}
        if call_id:
            # MCP 的 `_meta` 是可选的透传位；我们只放自己的 call_id 便于日志对齐。
            params["_meta"] = {"tianshu/call_id": call_id}
        result = self.request(request_method, params, timeout_ms=limit)
        content = result.get("content")
        if not isinstance(content, list):
            raise McpProtocolError(
                f"server {self.name!r} 的 tools/call 结果 content 不是数组"
            )
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise McpProtocolError(
                    f"server {self.name!r} 的 tools/call content 含非法内容块"
                )
            if item["type"] == "text" and not isinstance(item.get("text"), str):
                raise McpProtocolError(
                    f"server {self.name!r} 的 tools/call text 不是文本"
                )
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise McpProtocolError(
                f"server {self.name!r} 的 tools/call isError 不是布尔值"
            )
        blocks = tuple(content)
        structured = result.get("structuredContent")
        kept, total_bytes, kept_bytes, dropped_bytes, dropped_blocks = _clip_blocks(
            blocks, limit=self.config.max_result_bytes
        )
        # `structuredContent` 也是结果正文：它同样受 `max_result_bytes` 约束。
        # 只统计文本会让"结果上限"名不副实（一个 5 MB 的 structuredContent 能
        # 原样通过）；这里把它按紧凑 JSON 的字节数计入同一份预算。
        if structured is not None:
            try:
                encoded = json.dumps(
                    structured, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            except (TypeError, ValueError):  # pragma: no cover - 服务端给了不可序列化对象
                encoded = b""
                structured = None
            total_bytes += len(encoded)
            room = self.config.max_result_bytes - kept_bytes
            if encoded and len(encoded) <= room:
                kept_bytes += len(encoded)
            elif encoded:
                dropped_bytes += len(encoded)
                structured = None
        dropped = dropped_bytes > 0 or dropped_blocks > 0
        return McpCallResult(
            request_id=self._last_request_id,
            content=kept,
            is_error=is_error,
            structured=structured,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes=total_bytes,
            truncated=dropped,
            dropped_bytes=dropped_bytes,
            dropped_blocks=dropped_blocks,
        )

    def cancel(self, request_id: int, reason: str = "cancelled by client") -> None:
        """发取消通知（**只是请求**：server 可以不理会）。"""
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": reason},
                },
                deadline=time.monotonic() + _MCP_CANCEL_SEND_GRACE_MS / 1000,
            )
        except McpError:
            # 已经断开的连接没有"取消"可言；调用方会按 unknown 结算。
            pass

    # ---- 握手 / 发现

    def _handshake(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
            },
            timeout_ms=self.config.handshake_timeout_ms,
        )
        version = result.get("protocolVersion")
        if not isinstance(version, str):
            raise McpProtocolError(
                f"server {self.name!r} 的 initialize 没有声明 protocolVersion"
            )
        if version not in MCP_PROTOCOL_VERSIONS:
            raise McpProtocolError(
                f"server {self.name!r} 声明了不认识的协议版本 {version!r}"
                f"（客户端认识 {list(MCP_PROTOCOL_VERSIONS)}）；拒绝降级"
            )
        self._protocol_version = version
        info = result.get("serverInfo")
        self._server_info = info if isinstance(info, dict) else {}
        capabilities = result.get("capabilities")
        self._capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.notify("notifications/initialized", {})

    def _discover_tools(self) -> None:
        tools: list[McpTool] = []
        cursor: str | None = None
        pages = 0
        while True:
            stopped_mid_page = False
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            result = self.request(
                "tools/list", params, timeout_ms=self.config.handshake_timeout_ms
            )
            page = result.get("tools")
            if page is None:
                page = []
            if not isinstance(page, list):
                raise McpProtocolError(
                    f"server {self.name!r} 的 tools/list 结果 tools 不是数组"
                )
            for item in page:
                if len(tools) >= self.config.max_tools:
                    # 配置说最多几个就最多几个：多出来的不建对象、也不问下一页。
                    # 只有"页内还有剩余"才算截断——恰好装满且没有下一页时，
                    # server 确实就只有这么多工具，不能谎报截断（那会让准入层
                    # 生成一条假的 `<truncated>` 拒绝行）。
                    stopped_mid_page = True
                    self._tools_truncated = True
                    break
                tools.append(McpTool.from_raw(item))
            pages += 1
            next_cursor = None if stopped_mid_page else result.get("nextCursor")
            if stopped_mid_page or (len(tools) >= self.config.max_tools and next_cursor):
                # 满了且**确实还有更多**（页内剩余或 nextCursor 非空）才算截断。
                self._tools_truncated = True
                break
            if not next_cursor:
                break
            if not isinstance(next_cursor, str):
                raise McpProtocolError(
                    f"server {self.name!r} 的 nextCursor 不是字符串：{next_cursor!r}"
                )
            if pages >= MCP_MAX_LIST_PAGES:
                raise McpProtocolError(
                    f"server {self.name!r} 的 tools/list 翻了 {pages} 页还没到底"
                )
            cursor = next_cursor
        self._tools = tuple(tools)

    # ---- 读写

    def _send(self, message: dict[str, Any], *, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + self.config.call_timeout_ms / 1000
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(payload) + 1 > MCP_MAX_MESSAGE_BYTES:
            raise McpProtocolError(
                f"要发给 server {self.name!r} 的消息超过 {MCP_MAX_MESSAGE_BYTES} 字节"
            )
        proc = self._proc
        stream = None if proc is None else proc.stdin
        if stream is None:
            raise McpClosed(f"server {self.name!r} 没有 stdin（已关闭）")
        request_id = message.get("id", 0)
        if not isinstance(request_id, int):
            request_id = 0
        if not self._send_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise McpTimeout(
                f"server {self.name!r} 等待 stdin 写锁超时", request_id=request_id
            )
        try:
            frame = memoryview(payload + b"\n")
            offset = 0
            try:
                while offset < len(frame):
                    if not self.is_alive:
                        raise McpClosed(f"server {self.name!r} 已断开")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if offset:
                            # 半帧不能接着复用：下一条 JSON 会接到残帧后面。
                            self._fail("写 stdin 超时，已发送不完整消息")
                        raise McpTimeout(
                            f"server {self.name!r} 写 stdin 超时", request_id=request_id
                        )
                    try:
                        written = os.write(stream.fileno(), frame[offset:])
                    except BlockingIOError:
                        written = 0
                    if written:
                        offset += written
                    else:
                        time.sleep(min(0.01, remaining))
            except (OSError, ValueError) as exc:
                self._fail(f"写 stdin 失败：{exc}")
                raise McpClosed(f"server {self.name!r} 的 stdin 已断开") from exc
        finally:
            self._send_lock.release()

    def _read_loop(self) -> None:
        proc = self._proc
        stream = None if proc is None else proc.stdout
        if stream is None:  # pragma: no cover - 构造顺序保证有 stdout
            return
        buffer = bytearray()
        while True:
            try:
                chunk = stream.read(_MCP_READ_CHUNK)
            except (OSError, ValueError):
                self._fail("读 stdout 失败")
                self._responses.put((None, {}))
                return
            if not chunk:
                self._fail(self._failure or "server 关闭了 stdout（EOF）")
                self._responses.put((None, {}))
                return
            buffer.extend(chunk)
            while True:
                index = buffer.find(b"\n")
                if index < 0:
                    if len(buffer) > MCP_MAX_MESSAGE_BYTES:
                        self._fail(
                            f"单条消息超过 {MCP_MAX_MESSAGE_BYTES} 字节还没换行"
                        )
                        self._responses.put((None, {}))
                        return
                    break
                line = bytes(buffer[:index])
                del buffer[: index + 1]
                if len(line) > MCP_MAX_MESSAGE_BYTES:
                    self._fail(
                        f"单条消息 {len(line)} 字节超过上限 {MCP_MAX_MESSAGE_BYTES}"
                    )
                    self._responses.put((None, {}))
                    return
                if not line.strip():
                    continue
                if not self._handle_line(line):
                    return

    def _handle_line(self, line: bytes) -> bool:
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._fail(f"收到畸形 JSON：{exc}")
            self._responses.put((None, {}))
            return False
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._fail(f"收到不是 JSON-RPC 2.0 的消息：{line[:200]!r}")
            self._responses.put((None, {}))
            return False
        if "id" in message and ("result" in message or "error" in message):
            message_id = message["id"]
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                self._fail(f"响应里的 id 不是整数：{message_id!r}")
                self._responses.put((None, {}))
                return False
            self._responses.put((message_id, message))
            return True
        if "id" in message and "method" in message:
            # server → 客户端的请求（sampling / roots / …）：我们没声明这些能力。
            self._answer_unsupported(message)
            return True
        if "method" in message:
            self._notifications += 1
            return True
        self._fail(f"看不懂的 JSON-RPC 消息：{line[:200]!r}")
        self._responses.put((None, {}))
        return False

    def _answer_unsupported(self, message: dict[str, Any]) -> None:
        reply = {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {
                "code": -32601,
                "message": "method not supported by this client",
            },
        }
        try:
            self._send(reply)
        except McpError:  # pragma: no cover - 连接已断时不必再报
            pass

    def _drain_stderr(self) -> None:
        proc = self._proc
        stream = None if proc is None else proc.stderr
        if stream is None:  # pragma: no cover
            return
        while True:
            try:
                chunk = stream.read(4_096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self._stderr_lock:
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > MCP_STDERR_TAIL_BYTES:
                    del self._stderr_tail[: len(self._stderr_tail) - MCP_STDERR_TAIL_BYTES]

    def _fail(self, reason: str) -> None:
        """协议/管道失败：标记失效、唤醒等待方，并且**真的把进程收掉**。

        只把 `is_alive` 改成 False 是不够的：进程还活着就是一个跑飞的外部程序
        （它可能还在写文件/联网），调用方也永远等不到它退出。这里 terminate +
        有界等待，必要时 kill；`close()` 之后仍会再收一次（幂等）。
        """
        first = not self._failure
        if first:
            self._failure = reason
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                deadline = time.monotonic() + MCP_CLOSE_GRACE_MS / 1000
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass


# ---------------------------------------------------------------- 多 server


class McpClients:
    """进程级的一批 server：启动、状态、关闭。

    **启动失败不拖累其它 server**：失败原因进 `diagnostics`，状态是 `unavailable`。
    调用方（派发层）对一个不可用 server 的请求在**弹批准之前**就拒绝。
    """

    def __init__(
        self,
        configs: Mapping[str, McpServerConfig],
        *,
        default_cwd: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._configs = dict(configs)
        self._default_cwd = default_cwd
        self._environ = environ
        self._servers: dict[str, StdioServer] = {}
        self._failures: dict[str, str] = {}
        self._started = False
        atexit.register(self.close_all)

    # ---- 构造

    @classmethod
    def from_config_file(
        cls,
        path: str | os.PathLike[str],
        *,
        default_cwd: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> McpClients:
        return cls(load_mcp_config(path), default_cwd=default_cwd, environ=environ)

    @classmethod
    def from_env(
        cls,
        *,
        default_cwd: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> McpClients | None:
        """读 `TIANSHU_MCP_CONFIG`；没配置 → None（= 不启用 MCP，工具面不变）。"""
        configs = config_from_env(environ)
        if not configs:
            return None
        return cls(configs, default_cwd=default_cwd, environ=environ)

    # ---- 生命周期

    @property
    def configs(self) -> dict[str, McpServerConfig]:
        return dict(self._configs)

    def start_all(self) -> None:
        """并发启动全部 server（每个 server 各自超时，互不阻塞）。"""
        if self._started:
            raise McpError("McpClients 已经启动过")
        self._started = True
        threads: list[threading.Thread] = []
        lock = threading.Lock()

        def boot(name: str, config: McpServerConfig) -> None:
            server = StdioServer(
                config, default_cwd=self._default_cwd, environ=self._environ
            )
            try:
                server.start()
            except McpServerUnavailable as exc:
                with lock:
                    self._failures[name] = str(exc)
                return
            with lock:
                self._servers[name] = server

        for name, config in self._configs.items():
            thread = threading.Thread(target=boot, args=(name, config), name=f"mcp-boot-{name}")
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

    def close_all(self) -> None:
        for server in list(self._servers.values()):
            server.close()
        self._servers.clear()

    # ---- 查询

    def server(self, name: str) -> StdioServer | None:
        return self._servers.get(name)

    def ready_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, server in self._servers.items() if server.is_alive))

    def states(self) -> tuple[McpServerState, ...]:
        names = sorted(set(self._configs) | set(self._servers) | set(self._failures))
        rows: list[McpServerState] = []
        for name in names:
            server = self._servers.get(name)
            if server is not None and server.is_alive:
                rows.append(
                    McpServerState(
                        name=name,
                        status="ready",
                        protocol_version=server.protocol_version,
                        server_info=server.server_info,
                        tools=server.tools,
                        diagnostics=(),
                        pid=server.pid,
                        tools_truncated=server.tools_truncated,
                    )
                )
                continue
            reason = self._failures.get(name, "")
            if server is not None and not reason:
                reason = server.diagnostics()
            rows.append(
                McpServerState(
                    name=name,
                    status="unavailable",
                    diagnostics=(reason,) if reason else ("未启动",),
                    pid=None if server is None else server.pid,
                )
            )
        return tuple(rows)

    def diagnostics(self) -> tuple[str, ...]:
        lines = [f"{name}: {reason}" for name, reason in sorted(self._failures.items())]
        for name, server in sorted(self._servers.items()):
            if not server.is_alive and server.diagnostics() != "ok":
                lines.append(f"{name}: {server.diagnostics()}")
        return tuple(lines)
