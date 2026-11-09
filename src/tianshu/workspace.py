"""Workspace 边界：所有路径先规范化，再与 root 比较。

这是 PRD §8.1 的 L1 层——跨平台唯一真正一致的那层。
不做字符串前缀比较（会被 `..`、符号链接、大小写绕过），只做 resolve 后比较。

**边界与存在性是两件事**（M3 拆开）：

- 词法边界（"这个候选路径解析后还在 root 里吗"）是纯路径运算，所有后端
  共用，必须是唯一实现——多一套实现就多一个绕过点。
- "root 现在存不存在、是不是目录"是后端的事实。local 后端在 `require_root`
  里快速失败；符号链接由本类的 `resolve()` 跟随 realpath 来消解（memfs
  没有符号链接，那条路径对它没有意义），后端只检查"根落不落在自己的
  命名空间里"。

所以 `Workspace.__init__` 不再自己 `is_dir()`，而是问当前后端
`require_root()`。对 local 后端行为与 M2 完全一致（不存在就抛
WorkspaceViolation），对 memfs 才允许逻辑根。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import WorkspaceViolation
from .pathkeys import (
    CASE_INSENSITIVE_FS,
    is_ancestor_or_self,
    normalize_name,
    overlap_key,
)
from .sandbox import FileBackend, current_backend


@dataclass(frozen=True)
class ResourceIdentity:
    """稳定的目录身份。

    不能用路径字符串当身份：符号链接、大小写、Unicode 规范化都能让
    同一个目录出现多个写法。用 (st_dev, st_ino) 才是真的"同一个东西"。
    Windows 上 st_ino 在部分文件系统为 0，退回规范化路径。

    另外保留规范路径：inode 只回答"是不是同一个目录"，回答不了
    "是不是同一棵子树"，父子目录互斥必须靠路径前缀。
    """

    key: str
    path: str

    @classmethod
    def of(
        cls, path: Path, *, backend: FileBackend | None = None
    ) -> ResourceIdentity:
        """身份键由当前后端给出。

        **跨后端只承诺 `path_key`（路径前缀覆盖）。** local 的键是
        `dev:ino`，memfs 是规范化路径；两者不可比，也不该比。租约的
        `assert_covers` 同时看 key 与 path_key：同后端时 key 更准，
        跨后端时只剩路径前缀这一层承诺——这正是 M3 之后仍然安全的边界。
        """
        resolved = path.resolve()
        impl = backend if backend is not None else current_backend()
        return cls(key=impl.identity_key(resolved), path=str(resolved))

    @property
    def path_key(self) -> str:
        return overlap_key(self.path)

    def overlaps(self, other: ResourceIdentity) -> bool:
        """同一资源，或父子目录（同一棵子树）。"""
        if self.key == other.key:
            return True
        a, b = self.path_key, other.path_key
        return is_ancestor_or_self(a, b) or is_ancestor_or_self(b, a)


@dataclass(frozen=True)
class WorkspaceBinding:
    """Task 与一个目录的绑定（D6：Task 是用户可见单位，目录是 Task 的属性）。

    `mode` 决定这个目录能不能写；`snapshot_policy` 决定写前是否留快照。
    `workspaces=[]` 合法——纯对话任务不需要任何目录。
    """

    root_path: str
    mode: str = "writable"
    snapshot_policy: str = "on_write"

    MODES = ("readonly", "writable")
    SNAPSHOT_POLICIES = ("none", "on_write")

    def __post_init__(self) -> None:
        # 绑定在构造时就必须规范化，且**对所有输入形式一致**。
        # 只给 `str` 输入做 resolve 是不够的：JSON/dict 输入（事件重建、
        # CLI、手写测试）会原样留下 `/tmp/x` 这种别名，而执行侧解析出来的
        # 目标是 `/private/tmp/x`，于是 `_covers` 判成"目录外"——同一个目录
        # 两种写法，授权判断就出现了两个答案。反方向更危险：别名写法能绕过
        # 本应命中的绑定。
        object.__setattr__(self, "root_path", str(Path(self.root_path).resolve()))
        if self.mode not in self.MODES:
            raise WorkspaceViolation(f"未知 mode：{self.mode}（可选 {self.MODES}）")
        if self.snapshot_policy not in self.SNAPSHOT_POLICIES:
            raise WorkspaceViolation(
                f"未知 snapshot_policy：{self.snapshot_policy}"
                f"（可选 {self.SNAPSHOT_POLICIES}）"
            )

    @classmethod
    def of(cls, item: WorkspaceBinding | str | dict[str, Any]) -> WorkspaceBinding:
        """接受对象、路径字符串或 JSON 记录——旧数据里只有路径字符串。"""
        if isinstance(item, WorkspaceBinding):
            return item
        if isinstance(item, str):
            return cls(root_path=str(Path(item).resolve()))
        if isinstance(item, dict):
            known = {k: v for k, v in item.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        raise WorkspaceViolation(f"无法解析 workspace binding：{item!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_path": self.root_path,
            "mode": self.mode,
            "snapshot_policy": self.snapshot_policy,
        }

    def workspace(self) -> Workspace:
        return Workspace(Path(self.root_path))

    def identity(self) -> ResourceIdentity:
        return ResourceIdentity.of(Path(self.root_path))

    @property
    def can_write(self) -> bool:
        return self.mode == "writable"


def parse_bindings(raw: Any) -> list[WorkspaceBinding]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkspaceViolation(f"workspaces 必须是列表：{raw!r}")
    return [WorkspaceBinding.of(item) for item in raw]


class Workspace:
    """限定在一个根目录内的读写。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        # 边界由本类负责；"这个根现在能不能用"由后端负责。
        current_backend().require_root(self.root)

    def resolve(self, candidate: str | Path) -> Path:
        """把候选路径规范化为绝对路径，并保证它仍在 root 内。"""
        raw = Path(candidate)
        target = raw if raw.is_absolute() else self.root / raw

        # resolve() 会跟随符号链接并消解 `..`；对不存在的路径用 strict=False
        resolved = target.resolve()

        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceViolation(
                f"路径越界：{candidate!r} → {resolved}（workspace: {self.root}）"
            )
        return resolved

    def identity(self) -> ResourceIdentity:
        return ResourceIdentity.of(self.root)

    def relative(self, resolved: Path) -> str:
        """相对根路径，**一律 `/` 分隔**。

        `str(Path.relative_to())` 在 Windows 上给的是反斜杠，而这个字符串会
        进 output、快照证据、凭据与事件 payload——证据的形状必须跨平台一致，
        否则同一份记录在两个平台上哈希不同。`as_posix()` 是唯一的换算点。
        """
        return resolved.relative_to(self.root).as_posix()


__all__ = [
    "CASE_INSENSITIVE_FS",
    "ResourceIdentity",
    "Workspace",
    "WorkspaceBinding",
    "is_ancestor_or_self",
    "normalize_name",
    "overlap_key",
    "parse_bindings",
]