"""资源种类：把「要操作什么」从「必须是一个文件路径」里解耦出来。

## 为什么需要这一层

`ActionRequest.resources` 是 `tuple[str, ...]`，而派发层从头到尾按**绝对路径**
理解它：`_resolve_target()` 要 `Path(resources[0]).resolve()`、要一个覆盖它的
workspace 绑定、要一个覆盖它的**目录租约**，`FileExecutor._verify()` 还要
`ResourceIdentity.of(target)` 与 `Workspace.resolve(rel_path) == target`。
证据键也是 `(workspace 根, 相对路径)`。

记忆没有文件路径，也不能被伪装成一个目录（那正是 F10 明确拒绝的形状：把记忆
塞进一个真实目录，等于让「删掉投影」变成「删掉用户文件」）。所以需要一个
**显式**的资源种类，而不是给记忆编一个假路径。

## 为什么不给 `ActionRequest` 加 `resource_kind` 字段

因为那会改 `to_canonical_dict()`，于是**所有** file 请求的 `digest()` 与
`action_digest()` 都变。`action_digest` 是审批绑定的东西，`digest` 是派发与
grant 绑定的东西——历史库里的审批、grant、派发记录会集体失效，重放也会对不上。
为了一个新能力把既有授权链全部作废，不值得。

改成**方案前缀（scheme prefix）**：`resources` 里的字符串自己带种类。

- 绝对路径 → `file`（历史上每一条 `resources` 都是绝对路径，**逐字节不变**）；
- `memory://...` → `memory`；
- 其他任何 `xxx://` → 拒绝（fail-closed，不给"未知 scheme 当成文件"的后路）。

这样既有请求的摘要**一个字节都没动**，而新资源种类是显式的、可解析的、
可校验的。将来接 web / mcp / child 只需往 `RESOURCE_KINDS` 里加一种并实现
它的解析，不必再动文件路径那条线。

## 记忆作用域怎么编码进 URI

`memory://<scope>/<scope_id>`，`scope_id` 用 percent-encoding 转义（斜杠、冒号、
反斜杠在真实路径里都会出现）。**不做哈希**：事件日志里的引用是人类要读的，
`memory://workspace/%2FVolumes%2FDate%2Fproj` 虽然不好看，但能一眼看出是哪个
目录；哈希之后就只能靠猜，而「每条记忆必须能指回来源」是 F10 的硬要求。

`user` 作用域的 `scope_id` 固定是 `local`（D2 单用户，没有第二个用户可言）。
换成多用户是另一张契约工单，不是在这个字符串里留个位置假装支持。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from .errors import TianshuError

KIND_FILE = "file"
KIND_MEMORY = "memory"
KIND_NETWORK = "network"
KIND_MCP = "mcp"
KIND_SUBTASK = "subtask"

#: 封闭集：不在里面的种类一律拒绝。新增种类必须同时改这里、
#: `parse_resource()` 的分支与 PRD §5.4.1 的资源种类说明。
RESOURCE_KINDS: frozenset[str] = frozenset(
    {KIND_FILE, KIND_MEMORY, KIND_NETWORK, KIND_MCP, KIND_SUBTASK}
)

SCHEME_SEP = "://"
MEMORY_SCHEME = "memory"
NETWORK_SCHEME = "network"
MCP_SCHEME = "mcp"
SUBTASK_SCHEME = "subtask"

#: 网络传输方式封闭集。**没有** `ftp://`、`file://` 之类：这一层只描述
#: "往哪儿发"，多一种传输就多一套规范化规则，而 v1 只需要这两种。
NETWORK_TRANSPORTS: frozenset[str] = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

#: 主机标签：小写字母/数字/连字符，不以连字符开头结尾。IDN 先转 punycode 再判。
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_MCP_NAME = re.compile(r"^[a-z0-9_.-]{1,64}$")
PORT_RANGE = range(1, 65536)

SCOPE_USER = "user"
SCOPE_WORKSPACE = "workspace"
SCOPE_TASK = "task"

#: 记忆作用域封闭集（F10 §6）。多租户 / 团队 / ACL 不在 v1（D2）。
MEMORY_SCOPES: frozenset[str] = frozenset({SCOPE_USER, SCOPE_WORKSPACE, SCOPE_TASK})

#: `user` 作用域的唯一合法 scope_id：本项目是单用户产品。
USER_SCOPE_ID = "local"

#: task 的 scope_id 就是 task_id，形状与 tasks.py 生成的 id 一致。
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class ResourceError(TianshuError):
    """资源字符串解析不了。

    这是**调用方给错了**（模型给了畸形引用，或代码里拼错了 scheme），不是
    "暂时不支持"。宁可在这里炸，也不许把它降级当成一个文件路径去解析——
    那条后路会让 `memory://../../etc/passwd` 变成一次真实的文件访问。
    """


@dataclass(frozen=True)
class MemoryScope:
    """一个记忆作用域。`scope_id` 的语义由 `scope` 决定。"""

    scope: str
    scope_id: str

    def __post_init__(self) -> None:
        if self.scope not in MEMORY_SCOPES:
            raise ResourceError(
                f"未知记忆作用域：{self.scope!r}（合法值：{sorted(MEMORY_SCOPES)}）"
            )
        if self.scope == SCOPE_USER and self.scope_id != USER_SCOPE_ID:
            raise ResourceError(
                f"user 作用域的 scope_id 只能是 {USER_SCOPE_ID!r}：{self.scope_id!r}"
            )
        if self.scope == SCOPE_TASK and not _OPAQUE_ID.match(self.scope_id):
            raise ResourceError(f"task 作用域的 scope_id 形状不合法：{self.scope_id!r}")
        if self.scope == SCOPE_WORKSPACE and not os.path.isabs(self.scope_id):
            raise ResourceError(
                f"workspace 作用域的 scope_id 必须是绝对路径：{self.scope_id!r}"
            )

    def to_uri(self) -> str:
        return (
            f"{MEMORY_SCHEME}{SCHEME_SEP}{self.scope}/"
            f"{quote(self.scope_id, safe='')}"
        )

    def to_dict(self) -> dict[str, str]:
        return {"scope": self.scope, "scope_id": self.scope_id}

    @classmethod
    def user(cls) -> MemoryScope:
        return cls(SCOPE_USER, USER_SCOPE_ID)

    @classmethod
    def workspace(cls, root: Path | str) -> MemoryScope:
        """按**规范化后**的绝对路径建作用域。

        必须 canonicalize：`/a/b`、`/a/./b`、`/a/../a/b` 是同一个目录，如果
        它们算三个作用域，用户会在三个"不同"的 workspace 记忆里找不到自己
        十分钟前存的东西——而这三个其实是一个地方。
        """
        text = str(root)
        # 与 `parse_memory_uri` 同一个理由：`realpath` 会把相对路径按当前工作
        # 目录补成绝对路径，于是"相对路径"变成合法作用域，同一个值在不同
        # 目录下指向不同记忆。必须先拒绝，再归一。
        if not os.path.isabs(text):
            raise ResourceError(
                f"workspace 作用域的根必须是绝对路径（不接受相对路径）：{text!r}"
            )
        return cls(SCOPE_WORKSPACE, os.path.realpath(text))

    @classmethod
    def task(cls, task_id: str) -> MemoryScope:
        return cls(SCOPE_TASK, task_id)


@dataclass(frozen=True)
class NetworkTarget:
    """一次外发的目标：`network://<transport>/<host>[:port]/<path>[?query]`。

    ## 为什么不是 `https://...` 直接当资源

    `parse_resource()` 的第一条判据是"绝对路径就是 file"，而 `https://x/y` 不是
    绝对路径、又含 `://`，所以它会被读成"认不得的 scheme"而拒绝——这正是 G1a
    想要的效果（URL 不伪装成文件路径）。但"拒绝"不等于"没有这种资源"，于是
    用**自己的 scheme** 明确表达，而不是放宽 http(s) 的解析。

    ## 规范化到什么程度（每一项都有理由）

    - host 小写 + IDNA 转 punycode：`ExAmPle.COM` 与 `example.com` 是同一台机器，
      不归一就会出现"批准了 A、实际发到 B 却也算 A"这类凭据混淆；
    - 默认端口省略（http 80 / https 443）：同一个目标的两种写法必须得到同一个
      URI，否则审批摘要会因为写法不同而失效；
    - path 折叠 `.` / `..` 且**不许逃出根**：`/a/../b` 与 `/b` 是同一路径；
    - 百分号编码的段**先解码再判**：`%2e%2e` 解码后是 `..`，不解码就折叠不掉，
      等于给路径穿越留了一个"看起来规范"的入口（解码后含 `/`、`\\`、NUL 一律拒）；
    - query 原样保留（顺序有意义，不排序）；
    - 拒绝 userinfo（`user:pass@host`）与 fragment（`#`）。
    """

    transport: str
    host: str
    port: int | None
    path: str
    query: str = ""

    def __post_init__(self) -> None:
        if self.transport not in NETWORK_TRANSPORTS:
            raise ResourceError(
                f"不支持的传输方式：{self.transport!r}"
                f"（合法值：{sorted(NETWORK_TRANSPORTS)}）"
            )
        if not self.host:
            raise ResourceError("网络目标缺少主机")
        if self.port is not None and self.port not in PORT_RANGE:
            raise ResourceError(f"端口越界：{self.port!r}")
        if self.port == _DEFAULT_PORTS[self.transport]:
            raise ResourceError(
                f"默认端口必须省略（{self.transport} 默认 {_DEFAULT_PORTS[self.transport]}）："
                f"{self.port}"
            )
        if not self.path.startswith("/"):
            raise ResourceError(f"路径必须以 / 开头：{self.path!r}")
        if "#" in self.query:
            raise ResourceError(f"网络目标不接受 fragment：{self.query!r}")

    def to_uri(self) -> str:
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        uri = f"{NETWORK_SCHEME}{SCHEME_SEP}{self.transport}/{authority}{self.path}"
        if self.query:
            uri = f"{uri}?{self.query}"
        return uri

    @property
    def origin_uri(self) -> str:
        """**源**的规范写法：`network://<transport>/<host>[:port]`（不带路径）。

        任务级外发白名单存的是源，不是 URL：白名单回答"这个任务能不能跟这台
        服务器说话"，路径级别的取舍交给逐次人工批准。这里的写法与 `to_uri()`
        的根路径（`network://https/example.com/`）**归一后是同一个目标**，
        但对人来说少一个尾斜杠更不像 URL，也不会诱导人把路径写进白名单。
        """
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{NETWORK_SCHEME}{SCHEME_SEP}{self.transport}/{authority}"

    @property
    def origin(self) -> str:
        """`<transport>://<host>[:port]`：审批展示与"同一站点"判定用它。"""
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{self.transport}{SCHEME_SEP}{authority}"

    @classmethod
    def parse(cls, raw: str) -> NetworkTarget:
        if not isinstance(raw, str) or not raw.startswith(
            f"{NETWORK_SCHEME}{SCHEME_SEP}"
        ):
            raise ResourceError(f"不是网络资源引用：{raw!r}")
        rest = raw[len(NETWORK_SCHEME) + len(SCHEME_SEP) :]
        transport, _, tail = rest.partition("/")
        if transport not in NETWORK_TRANSPORTS:
            raise ResourceError(
                f"不支持的传输方式：{transport!r}"
                f"（合法值：{sorted(NETWORK_TRANSPORTS)}）"
            )
        if not tail:
            raise ResourceError(f"网络引用缺少主机与路径：{raw!r}")
        if "#" in tail:
            raise ResourceError(f"网络引用不接受 fragment：{raw!r}")

        before_query, _, query = tail.partition("?")
        authority, _, raw_path = before_query.partition("/")
        if "@" in authority:
            # userinfo 在 URL 里是凭据的家，在资源引用里就是混淆：批准的是主机，
            # 不是"看起来像主机"的一整串。
            raise ResourceError(f"网络目标不接受 userinfo（user:pass@host）：{raw!r}")
        if not authority:
            raise ResourceError(f"网络引用缺少主机：{raw!r}")

        segs = raw_path.split("/")
        host_segments = authority.rsplit(":", 1)
        if len(host_segments) == 2:
            host_text, port_text = host_segments
            if not port_text.isdigit():
                raise ResourceError(f"端口不是数字：{raw!r}")
            port: int | None = int(port_text)
        else:
            host_text, port = host_segments[0], None
        if port == _DEFAULT_PORTS[transport]:
            port = None

        host = _normalize_host(host_text)
        path = _normalize_path(segs)
        return cls(
            transport=transport, host=host, port=port, path=path, query=query
        )


def _normalize_host(text: str) -> str:
    if not text:
        raise ResourceError("网络目标缺少主机")
    if "%" in text:
        # 百分号编码的主机名是"两种写法指向一台机器"的老漏洞温床。
        raise ResourceError(f"主机不接受百分号编码：{text!r}")
    lowered = text.lower()
    if not lowered.isascii():
        try:
            lowered = lowered.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ResourceError(f"主机名 IDNA 归一失败：{text!r}（{exc}）") from exc
    labels = lowered.split(".")
    for label in labels:
        if not _HOST_LABEL.match(label):
            raise ResourceError(f"主机名标签不合法：{label!r}（{text!r}）")
    return lowered


def _normalize_path(segments: list[str]) -> str:
    """折叠 `.`/`..`，拒绝逃出根，拒绝解码后含路径分隔符的段。"""
    out: list[str] = []
    for raw in segments:
        decoded = unquote(raw)
        if decoded in (".", ""):
            continue
        if decoded == "..":
            if not out:
                raise ResourceError("网络路径不能逃出根：../")
            out.pop()
            continue
        if any(ch in decoded for ch in ("/", "\\", "\x00")):
            # `%2F` / `%5C` / `%00` 解码后是分隔符或截断符：不还原成结构，
            # 直接拒绝（还原就等于让我们自己的规范化变成了新的解析器）。
            raise ResourceError(f"路径段解码后含非法字符：{raw!r}")
        out.append(decoded)
    return "/" + "/".join(out)


@dataclass(frozen=True)
class McpTarget:
    """一个 MCP 工具：`mcp://<server>/<tool>`。

    MCP 工具是**不可信外部程序**（PRD 附表：恶意工具输出 / 不可信 MCP 注解不能
    成为授权或幂等依据），所以它按 `exec` 处理：要人工批准，且没有"读一下没关系"
    的自动路径。名字限制在 `[a-z0-9_.-]`：这两段会被写进事件与审批摘要，放开
    大小写/空格等于让日志里出现"看起来一样的两个工具"。
    """

    server: str
    tool: str

    def __post_init__(self) -> None:
        for attr in ("server", "tool"):
            value = getattr(self, attr)
            if not _MCP_NAME.match(value or ""):
                raise ResourceError(f"MCP {attr} 形状不合法：{value!r}")

    def to_uri(self) -> str:
        return f"{MCP_SCHEME}{SCHEME_SEP}{self.server}/{self.tool}"

    @classmethod
    def parse(cls, raw: str) -> McpTarget:
        if not isinstance(raw, str) or not raw.startswith(f"{MCP_SCHEME}{SCHEME_SEP}"):
            raise ResourceError(f"不是 MCP 资源引用：{raw!r}")
        rest = raw[len(MCP_SCHEME) + len(SCHEME_SEP) :]
        parts = rest.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ResourceError(f"MCP 引用必须是 mcp://<server>/<tool>：{raw!r}")
        return cls(server=parts[0], tool=parts[1])


def _validate_subtask_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _OPAQUE_ID.match(task_id):
        raise ResourceError(f"子任务 id 形状不合法：{task_id!r}")
    return task_id


@dataclass(frozen=True)
class ResourceRef:
    """一个解析过、种类已知、值已规范化的资源引用。"""

    kind: str
    value: str

    def __post_init__(self) -> None:
        if self.kind not in RESOURCE_KINDS:
            raise ResourceError(
                f"未知资源种类：{self.kind!r}（合法值：{sorted(RESOURCE_KINDS)}）"
            )
        if not self.value:
            raise ResourceError("资源值不能为空")
        # 每种种类都必须是**已规范化**的形状：写进去的值会进审批摘要与事件，
        # 允许"非规范的合法值"等于让同一个目标有两种摘要。
        if self.kind == KIND_NETWORK:
            if NetworkTarget.parse(self.value).to_uri() != self.value:
                raise ResourceError(f"网络资源不是规范形状：{self.value!r}")
        elif self.kind == KIND_MCP:
            if McpTarget.parse(self.value).to_uri() != self.value:
                raise ResourceError(f"MCP 资源不是规范形状：{self.value!r}")
        elif self.kind == KIND_SUBTASK:
            prefix = f"{SUBTASK_SCHEME}{SCHEME_SEP}"
            if not self.value.startswith(prefix):
                raise ResourceError(f"子任务资源必须以 {prefix} 开头：{self.value!r}")
            _validate_subtask_id(self.value[len(prefix) :])

    @property
    def uri(self) -> str:
        """回写成 `resources` 里那个字符串。

        file 种类是**裸绝对路径**（不带 scheme）：历史上每一条 `resources`
        都是绝对路径，重放与摘要比对要求它逐字节不变。
        """
        return self.value

    @property
    def network_target(self) -> NetworkTarget:
        if self.kind != KIND_NETWORK:
            raise ResourceError(f"不是网络资源：{self.kind!r}")
        return NetworkTarget.parse(self.value)

    @property
    def mcp_target(self) -> McpTarget:
        if self.kind != KIND_MCP:
            raise ResourceError(f"不是 MCP 资源：{self.kind!r}")
        return McpTarget.parse(self.value)

    @property
    def subtask_id(self) -> str:
        if self.kind != KIND_SUBTASK:
            raise ResourceError(f"不是子任务资源：{self.kind!r}")
        return self.value[len(SUBTASK_SCHEME) + len(SCHEME_SEP) :]

    @property
    def memory_scope(self) -> MemoryScope:
        if self.kind != KIND_MEMORY:
            raise ResourceError(f"不是记忆资源：{self.kind!r}")
        return parse_memory_uri(self.value)

    @classmethod
    def file(cls, path: Path | str) -> ResourceRef:
        return cls(KIND_FILE, str(path))

    @classmethod
    def memory(cls, scope: MemoryScope) -> ResourceRef:
        return cls(KIND_MEMORY, scope.to_uri())


def memory_uri(scope: MemoryScope) -> str:
    return scope.to_uri()


def parse_memory_uri(raw: str) -> MemoryScope:
    """把 `memory://<scope>/<scope_id>` 解析回作用域。畸形一律抛错。"""
    if not isinstance(raw, str) or not raw.startswith(f"{MEMORY_SCHEME}{SCHEME_SEP}"):
        raise ResourceError(f"不是记忆资源引用：{raw!r}")

    rest = raw[len(MEMORY_SCHEME) + len(SCHEME_SEP) :]
    if "/" not in rest:
        raise ResourceError(f"记忆引用缺少作用域分隔符：{raw!r}")
    scope, _, encoded = rest.partition("/")
    if not encoded:
        raise ResourceError(f"记忆引用的 scope_id 为空：{raw!r}")

    scope_id = unquote(encoded)
    if scope == SCOPE_WORKSPACE:
        # 声明是 workspace 作用域，就必须真的是一个绝对路径。**必须先判
        # 再 realpath**：`os.path.realpath("relative")` 不报错，它会按**当前
        # 工作目录**把相对路径补成绝对路径。先 realpath 就等于让一句相对引用
        # 变成合法作用域，判据从"必须是绝对路径"悄悄退化成"取决于进程的 CWD
        # 在哪"——同一个 URI 在不同目录下指向不同记忆，fail-closed 就没了。
        if not os.path.isabs(scope_id):
            raise ResourceError(
                "workspace 作用域的 scope_id 必须是绝对路径"
                f"（不接受相对路径）：{scope_id!r}"
            )
        # 绝对路径仍要 canonicalize：`/a/./b`、`/a/../a/b` 是同一个目录，
        # 不归一就会变成三个互不可见的作用域。
        scope_id = os.path.realpath(scope_id)
    return MemoryScope(scope, scope_id)


def parse_resource(raw: object) -> ResourceRef:
    """把 `resources` 里的一个字符串解析成 `ResourceRef`。**fail-closed**。

    判定顺序是有讲究的：**绝对路径优先**。

    理由不是风格问题：`/Users/x/a://b` 在 POSIX 上是**合法文件名**（`:` 和 `/`
    都不是非法字符，`://` 只是其中一段子串）。如果把"含 `://` 就当 scheme"放在
    前面，一条历史 `resources` 里真实存在的路径就会被读成一个未知 scheme 而拒绝，
    或者更糟——被读成某个将来的 scheme。历史上每一条 `resources` 都是绝对路径，
    所以"是绝对路径"这个判据必须第一个生效，让它们**永远**只可能是 file。
    """
    if not isinstance(raw, str) or not raw:
        raise ResourceError(f"资源必须是非空字符串：{raw!r}")

    # 1. 绝对路径 → file。必须先判，历史资源才逐字节稳。
    if os.path.isabs(raw):
        return ResourceRef.file(raw)

    # 2. 认得 scheme 的 → 按种类解析。
    if raw.startswith(f"{MEMORY_SCHEME}{SCHEME_SEP}"):
        return ResourceRef.memory(parse_memory_uri(raw))
    if raw.startswith(f"{NETWORK_SCHEME}{SCHEME_SEP}"):
        return ResourceRef(KIND_NETWORK, NetworkTarget.parse(raw).to_uri())
    if raw.startswith(f"{MCP_SCHEME}{SCHEME_SEP}"):
        return ResourceRef(KIND_MCP, McpTarget.parse(raw).to_uri())
    if raw.startswith(f"{SUBTASK_SCHEME}{SCHEME_SEP}"):
        rest = raw[len(SUBTASK_SCHEME) + len(SCHEME_SEP) :]
        if "/" in rest:
            raise ResourceError(f"子任务引用只接受一个 id：{raw!r}")
        return ResourceRef(KIND_SUBTASK, f"{SUBTASK_SCHEME}{SCHEME_SEP}"
                           f"{_validate_subtask_id(rest)}")

    # 3. 不认得的 scheme → 拒绝。`file://` 也在这里被拒：本项目不用 URL 表达
    #    到处都是绝对路径的东西，接受两种写法只多一处不一致。
    if SCHEME_SEP in raw:
        scheme = raw.partition(SCHEME_SEP)[0]
        raise ResourceError(
            f"不支持的资源 scheme：{scheme!r}（支持：绝对路径 = file，"
            f"{MEMORY_SCHEME}{SCHEME_SEP} = memory）"
        )

    # 4. 相对路径 → 拒绝。派发层不做"相对谁"的猜测。
    raise ResourceError(f"文件资源必须是绝对路径：{raw!r}")


def resource_uri(raw: object) -> str:
    """规范化回写：派发层用它把 `resources` 里的字符串统一成规范形状。"""
    return parse_resource(raw).uri


def is_memory_resource(raw: object) -> bool:
    return parse_resource(raw).kind == KIND_MEMORY


def parse_network_origin(raw: object) -> str:
    """任务级外发白名单的一项 → 规范的**源**。

    只接受 `network://<transport>/<host>[:port]`（`https://...` 不是本项目的
    资源写法，明确拒绝而不是偷偷当别名）：一个目标一种写法，审批摘要与事件
    payload 才不会被"同一个源的两种写法"劈成两份。

    路径与查询一律拒绝：白名单是**源**级别声明；如果允许写路径，人会以为
    "只许访问 /docs"，而实际执行侧只按 host+port 判——那是一条**假**的保证。
    """
    if not isinstance(raw, str) or not raw:
        raise ResourceError(f"外发白名单项必须是非空字符串：{raw!r}")
    if raw != raw.strip():
        raise ResourceError(f"外发白名单项不能带首尾空白：{raw!r}")
    try:
        target = NetworkTarget.parse(raw)
    except ResourceError as exc:
        raise ResourceError(
            f"外发白名单项必须是源（例如 network://https/example.com）：{exc}"
        ) from exc
    if target.path != "/" or target.query:
        raise ResourceError(
            "外发白名单项是源（transport + 主机 + 端口），不接受路径或查询："
            f"{raw!r}；路径级别的取舍在逐次人工批准里做"
        )
    return target.origin_uri


def normalize_network_origins(items: object) -> tuple[str, ...]:
    """把创建入口给的白名单归一成**去重 + 排序**的规范元组。

    排序不是洁癖：同一份声明必须在事件 payload 里得到同一份字节，
    否则"同一个任务两次创建"会产生两条看起来不同的记录。
    """
    if items is None:
        return ()
    if isinstance(items, str) or not isinstance(items, (list, tuple)):
        raise ResourceError(
            f"outbound_allow 必须是字符串数组：{type(items).__name__}"
        )
    return tuple(sorted({parse_network_origin(item) for item in items}))


def network_origin_allowed(
    target: NetworkTarget, origins: Iterable[object]
) -> bool:
    """目标是否落在白名单里（按 transport + host + port **精确**比较）。

    不做后缀 / 通配匹配：`a.example.com` 与 `example.com` 是两台机器，
    拿后缀当允许等于让一个源头批准带出整个域。

    落库的白名单项本应已是规范形；万一出现了坏值，这里**不放行**（continue），
    而不是让一条损坏的声明把任务变成"什么都能发"。
    """
    for raw in origins:
        try:
            origin = NetworkTarget.parse(raw)  # type: ignore[arg-type]
        except ResourceError:
            continue
        if (origin.transport, origin.host, origin.port) == (
            target.transport,
            target.host,
            target.port,
        ):
            return True
    return False


def build_memory_scope(scope: object, scope_id: object) -> MemoryScope:
    """由 (scope, scope_id) 构造作用域，**统一 canonicalize**。

    `workspace` 必须走 `MemoryScope.workspace()`（realpath），否则同一条记忆
    会因 `/a/b` 与 `/a/./b` 被算成两个作用域；调用方（CLI / UI / API）传原始
    路径时，声明集合是按 canonical 存的，不去归一就会"语义相同却判不在声明内"。
    """
    if not isinstance(scope, str) or not scope:
        raise ResourceError(f"scope 必须是非空字符串：{scope!r}")
    if scope not in MEMORY_SCOPES:
        raise ResourceError(f"未知记忆作用域：{scope!r}")
    if scope == SCOPE_WORKSPACE:
        if not isinstance(scope_id, str) or not scope_id:
            raise ResourceError("workspace 作用域的 scope_id 必须是非空绝对路径")
        return MemoryScope.workspace(scope_id)
    if scope == SCOPE_USER:
        if scope_id is None:
            return MemoryScope.user()
        return MemoryScope(scope, scope_id)
    if not isinstance(scope_id, str) or not scope_id:
        raise ResourceError(f"{scope} 作用域的 scope_id 必须是非空字符串")
    return MemoryScope(scope, scope_id)


__all__ = [
    "KIND_FILE",
    "KIND_MCP",
    "KIND_MEMORY",
    "KIND_NETWORK",
    "KIND_SUBTASK",
    "MCP_SCHEME",
    "McpTarget",
    "NETWORK_SCHEME",
    "NETWORK_TRANSPORTS",
    "NetworkTarget",
    "SUBTASK_SCHEME",
    "MEMORY_SCOPES",
    "MEMORY_SCHEME",
    "MemoryScope",
    "build_memory_scope",
    "RESOURCE_KINDS",
    "ResourceError",
    "ResourceRef",
    "SCOPE_TASK",
    "SCOPE_USER",
    "SCOPE_WORKSPACE",
    "USER_SCOPE_ID",
    "is_memory_resource",
    "network_origin_allowed",
    "normalize_network_origins",
    "memory_uri",
    "parse_memory_uri",
    "parse_network_origin",
    "parse_resource",
    "resource_uri",
]