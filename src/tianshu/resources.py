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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from .errors import TianshuError

KIND_FILE = "file"
KIND_MEMORY = "memory"

#: 封闭集：不在里面的种类一律拒绝。新增种类必须同时改这里、
#: `parse_resource()` 的分支与 PRD §5.4.1 的资源种类说明。
RESOURCE_KINDS: frozenset[str] = frozenset({KIND_FILE, KIND_MEMORY})

SCHEME_SEP = "://"
MEMORY_SCHEME = "memory"

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

    @property
    def uri(self) -> str:
        """回写成 `resources` 里那个字符串。

        file 种类是**裸绝对路径**（不带 scheme）：历史上每一条 `resources`
        都是绝对路径，重放与摘要比对要求它逐字节不变。
        """
        return self.value

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
    "KIND_MEMORY",
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
    "memory_uri",
    "parse_memory_uri",
    "parse_resource",
    "resource_uri",
]