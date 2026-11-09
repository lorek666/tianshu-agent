"""可替换的执行后端（M3 · PR F）。

接缝划在**存储原语**这一层：工具逻辑（截断、UTF-8 回退、拒绝覆盖、写前证据
顺序、返回形状、异常映射）留在 core，后端只回答两件事——"这个路径现在有
没有东西"和"字节怎么进出"。判定与证据（grant / intent / snapshot / receipt）
一律留在 core：`Dispatcher` 仍是唯一策略判定点，`FileExecutor` 仍是唯一
"实际打开文件"的入口（PRD §8.1）。

两个实现：

| 实现 | 是什么 | 会碰宿主盘吗 |
|---|---|---|
| `LocalBackend` | 真实文件系统 | 会，且只有它会 |
| `MemBackend` | 进程内虚拟文件系统 | 不会，一个字节都不写 |

**三条不许升级的诚实声明：**

1. `MemBackend` **不是沙箱**，它比 `LocalBackend` 更弱：没有权限位、没有
   owner、没有符号链接、没有磁盘配额。它证明的是"工具副作用经过一个可
   替换的边界"，不是"副作用被限制住了"。
2. 崩溃恢复的证据（kill -9 / failpoint）**仍然只由 `LocalBackend` 覆盖**：
   `MemBackend` 的状态随进程消失，无法参与"进程被杀之后还能不能核对"。
3. PRD §8.1 的威胁表不因这个模块改变。模型越权、提示词注入、失控 worker
   仍然靠策略层 + `Workspace` 路径边界 + 审批来挡，后端不提供任何隔离能力。

**后端选择是进程级的**（`TIANSHU_SANDBOX_BACKEND` 或 `configure_backend()`），
不是 per-run。原因不是设计偏好：M3 的验收判据是
`git diff main -- src/tianshu/loop.py` 为空，而 `build_stack` 在 loop.py 里，
把后端塞进装配层会让这条机械证据失效。代价是一个进程内不能同时用两个
后端；`FileExecutor` 因此会在构造时和执行前各校验一次"当前后端没被换掉"，
发现不一致宁可拒绝（`ToolNotAllowed`，一个字节都没动），也不半真半假地跑。
per-run 的灵活性留给 M5 的 Host。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import TianshuError, WorkspaceViolation
from .fsutil import atomic_write_bytes
from .pathkeys import overlap_key

ENV_VAR = "TIANSHU_SANDBOX_BACKEND"
DEFAULT_BACKEND = "local_fs"

#: 虚拟后端的根命名空间。选这个前缀的两个理由：
#: 1. 宿主机上几乎不可能存在（不是 /tmp，避开 macOS 的 /private 符号链接）；
#: 2. 一眼能看出"这不是真实路径"，不会有人把它当成可写的目录。
MEM_NAMESPACE = Path("/__tianshu_memfs__")


@runtime_checkable
class FileBackend(Protocol):
    """存储原语。实现必须满足的语义写在每个方法上，contract test 逐条验证。

    路径参数一律是**已解析的绝对路径**（由 `Workspace.resolve` 产出）。
    错误约定：读失败抛 `OSError` 子类；写失败抛 `OSError` 子类；路径不存在
    用 `FileNotFoundError`。工具层据此区分"返回 failed"与"抛异常 → unknown"。
    """

    name: str

    def require_root(self, root: Path) -> None:
        """确认这个根可用。local：必须是真实目录；mem：必须落在虚拟命名空间。"""

    def exists(self, p: Path) -> bool: ...

    def is_file(self, p: Path) -> bool: ...

    def is_dir(self, p: Path) -> bool: ...

    def size(self, p: Path) -> int:
        """普通文件的字节数。

        不存在抛 FileNotFoundError；是目录抛 IsADirectoryError——
        目录没有可移植的"文件大小"，别让调用方拿到一个随平台变化的值。
        """

    def list_dir(self, p: Path) -> list[tuple[str, bool, int]]:
        """直接子项：(原始名字, 是否目录, 字节数)。读不到的条目跳过。

        **目录条目的字节数恒为 0。** 目录大小跨平台没有一致含义
        （macOS 96 / ext4 4096 / tmpfs 又不同），同一份 list_files 输出
        不该因为平台不同而不一样。
        """

    def read_bytes(self, p: Path, limit: int | None = None) -> tuple[bytes, int]:
        """返回 (前 limit 字节或全部, 文件总字节数)。一次打开，避免 stat/read 竞态。"""

    def write_bytes_atomic(self, p: Path, data: bytes) -> None:
        """原子替换：要么看到旧内容，要么看到新内容，不出现半截文件。"""

    def unlink(self, p: Path) -> None: ...

    def identity_key(self, p: Path) -> str:
        """资源身份键。**跨后端只承诺路径前缀**（见 ResourceIdentity 的注释）。"""


class LocalBackend:
    """真实文件系统。唯一会碰宿主盘的实现。"""

    name = "local_fs"

    def require_root(self, root: Path) -> None:
        if not root.is_dir():
            raise WorkspaceViolation(f"workspace 不存在或不是目录：{root}")

    def exists(self, p: Path) -> bool:
        return p.exists()

    def is_file(self, p: Path) -> bool:
        return p.is_file()

    def is_dir(self, p: Path) -> bool:
        return p.is_dir()

    def size(self, p: Path) -> int:
        st = p.stat()
        if p.is_dir():
            raise IsADirectoryError(str(p))
        return st.st_size

    def list_dir(self, p: Path) -> list[tuple[str, bool, int]]:
        out: list[tuple[str, bool, int]] = []
        for child in p.iterdir():
            try:
                st = child.stat()
            except OSError:
                # 读不到 size 的条目跳过：这一点与 M2 的行为一致。
                continue
            is_dir = child.is_dir()
            # 目录项的 size 固定为 0（见 FileBackend.list_dir 的约定）。
            out.append((child.name, is_dir, 0 if is_dir else st.st_size))
        return out

    def read_bytes(self, p: Path, limit: int | None = None) -> tuple[bytes, int]:
        # 只打开一次，用 fstat 取总大小：避免"先 stat 再 open"之间目标被换掉。
        with p.open("rb") as fh:
            total = os.fstat(fh.fileno()).st_size
            data = fh.read() if limit is None else fh.read(limit)
        return data, total

    def write_bytes_atomic(self, p: Path, data: bytes) -> None:
        atomic_write_bytes(p, data)

    def unlink(self, p: Path) -> None:
        p.unlink()

    def identity_key(self, p: Path) -> str:
        try:
            st = os.stat(p)
            if st.st_ino:
                return f"{st.st_dev}:{st.st_ino}"
        except OSError:
            pass
        # 退化：Windows 或用不了 inode 的文件系统
        return "path:" + overlap_key(p)


class MemBackend:
    """进程内虚拟文件系统。

    **不是沙箱**：没有权限、owner、符号链接、磁盘配额。它只用来证明"同一套
    契约可以被另一个实现满足"，以及给测试提供一个确定性的、不碰宿主盘的
    执行环境。状态随进程消失，因此不参与崩溃恢复的证据。

    根命名空间之外的真实路径一律拒绝（fail closed）：这个后端存在的意义
    之一就是在被误用成"真盘"之前先炸掉。如果宿主机上恰好存在同名命名空间，
    同样拒绝运行——那说明环境已经不可信了。
    """

    name = "memfs"

    def __init__(self, namespace: Path | str = MEM_NAMESPACE) -> None:
        # 必须 resolve：macOS 上 /var 是指向 /private/var 的符号链接，
        # 不统一的话同一个逻辑根会出现两种写法，键就对不上了。
        self.namespace = Path(namespace).resolve()
        self._lock = threading.Lock()
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = {self.namespace.as_posix()}

    # ---- 生命周期 ----

    def require_root(self, root: Path) -> None:
        resolved = root.resolve()
        if resolved != self.namespace and self.namespace not in resolved.parents:
            raise WorkspaceViolation(
                f"memfs 后端只接受虚拟根（{self.namespace} 之下），"
                f"收到真实路径：{resolved}"
            )
        if self.namespace.exists():
            raise WorkspaceViolation(
                f"虚拟命名空间在宿主机上已存在，拒绝运行（fail closed）："
                f"{self.namespace}"
            )
        with self._lock:
            self._dirs.add(resolved.as_posix())

    # ---- 读 ----

    def exists(self, p: Path) -> bool:
        key = p.as_posix()
        return key in self._files or key in self._dirs

    def is_file(self, p: Path) -> bool:
        return p.as_posix() in self._files

    def is_dir(self, p: Path) -> bool:
        return p.as_posix() in self._dirs

    def size(self, p: Path) -> int:
        key = p.as_posix()
        if key in self._dirs:
            raise IsADirectoryError(str(p))
        try:
            return len(self._files[key])
        except KeyError:
            raise FileNotFoundError(str(p)) from None

    def list_dir(self, p: Path) -> list[tuple[str, bool, int]]:
        base = p.as_posix()
        if base not in self._dirs:
            raise FileNotFoundError(str(p))
        out: list[tuple[str, bool, int]] = []
        for key, data in self._files.items():
            parent, name = _split(key)
            if parent == base:
                out.append((name, False, len(data)))
        for key in self._dirs:
            if key == base:
                continue
            parent, name = _split(key)
            if parent == base:
                out.append((name, True, 0))
        return out

    def read_bytes(self, p: Path, limit: int | None = None) -> tuple[bytes, int]:
        try:
            data = self._files[p.as_posix()]
        except KeyError:
            raise FileNotFoundError(str(p)) from None
        return (data if limit is None else data[:limit]), len(data)

    # ---- 写 ----

    def write_bytes_atomic(self, p: Path, data: bytes) -> None:
        key = p.as_posix()
        with self._lock:
            self._ensure_parents(key)
            # 单条 dict 赋值即"原子替换"，不会出现半截内容。
            self._files[key] = bytes(data)

    def unlink(self, p: Path) -> None:
        with self._lock:
            try:
                del self._files[p.as_posix()]
            except KeyError:
                raise FileNotFoundError(str(p)) from None

    def identity_key(self, p: Path) -> str:
        return "path:" + overlap_key(p)

    # ---- 测试复位 ----

    def clear(self) -> None:
        """清空虚拟状态（命名空间保留）。

        `reset_backend()` 会调它：memfs 是进程级单例，只清 `_current` 的话，
        下一个用例会看见上一个用例的文件（症状是 `OverwriteRefused:
        目标已存在`，或者 list_files 列出不该存在的条目）。
        """
        with self._lock:
            self._files.clear()
            self._dirs = {self.namespace.as_posix()}

    # ---- 内部 ----

    def _ensure_parents(self, key: str) -> None:
        parent = _split(key)[0]
        while parent and parent not in self._dirs:
            self._dirs.add(parent)
            if parent == self.namespace.as_posix():
                break
            parent = _split(parent)[0]


def _split(key: str) -> tuple[str, str]:
    head, _, name = key.rpartition("/")
    return head or "/", name


_BACKENDS: dict[str, type] = {
    DEFAULT_BACKEND: LocalBackend,
    "memfs": MemBackend,
}

_LOCAL = LocalBackend()
_MEM = MemBackend()
_current: FileBackend | None = None


def backend_names() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))


def resolve_backend(choice: FileBackend | str | None) -> FileBackend:
    """把名字解析成实例。None 表示"按环境变量，缺省 local"。"""
    if choice is None:
        choice = os.environ.get(ENV_VAR, DEFAULT_BACKEND)
    if not isinstance(choice, str):
        if not isinstance(choice, FileBackend):
            raise TianshuError(f"不是合法的后端实现：{choice!r}")
        return choice
    if choice == DEFAULT_BACKEND:
        return _LOCAL
    if choice == "memfs":
        return _MEM
    raise TianshuError(
        f"未知后端：{choice!r}（可选 {', '.join(backend_names())}；"
        f"由 {ENV_VAR} 或 configure_backend() 选择）"
    )


def configure_backend(backend: FileBackend | str | None) -> FileBackend:
    """显式选择后端。传名字或实例；返回实际生效的实例。

    这是进程级设置：一个进程同一时刻只有一个后端，见模块开头的说明。
    """
    global _current
    _current = resolve_backend(backend)
    return _current


def current_backend() -> FileBackend:
    global _current
    if _current is None:
        _current = resolve_backend(None)
    return _current


def reset_backend() -> None:
    """回到"按环境变量解析"的初始状态，并清空 memfs 单例的虚拟状态。

    两件事必须一起做：memfs 的后端实例是模块级单例，只把 `_current` 置空
    并不会清掉它的 `_files`/`_dirs`，下一个用例会看见上一个用例写过的文件
    （典型症状：`OverwriteRefused: 目标已存在`，或 list_files 多出条目）。
    显式创建并传入的 `MemBackend(...)` 实例归调用方所有，这里不动。
    """
    global _current
    _current = None
    _MEM.clear()


def memfs_root(name: str) -> Path:
    """给测试/工具用的虚拟根构造器。只拼路径，不创建任何东西。"""
    return MEM_NAMESPACE / name


__all__ = [
    "DEFAULT_BACKEND",
    "ENV_VAR",
    "FileBackend",
    "LocalBackend",
    "MEM_NAMESPACE",
    "MemBackend",
    "backend_names",
    "configure_backend",
    "current_backend",
    "memfs_root",
    "reset_backend",
    "resolve_backend",
]
