"""Workspace 边界：所有路径先规范化，再与 root 比较。

这是 PRD §8.1 的 L1 层——跨平台唯一真正一致的那层。
不做字符串前缀比较（会被 `..`、符号链接、大小写绕过），只做 resolve 后比较。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import WorkspaceViolation

# 大小写不敏感的文件系统（Windows 默认、macOS 默认 APFS）上，
# 同一目录可能有多种写法；重叠判断必须折叠大小写，否则 /A 与 /a/b 会被当成两棵树。
CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"


def normalize_name(name: str) -> str:
    """macOS 的 APFS 存 NFD，Python 原样返回。比较前统一成 NFC。

    不处理会导致带中文/重音的文件名比对失败、UI 出现重复项。
    """
    return unicodedata.normalize("NFC", name)


def overlap_key(path: Path | str) -> str:
    """用于判断"同一棵子树"的路径键（不是资源身份键）。"""
    key = os.path.normpath(normalize_name(str(path)))
    return key.casefold() if CASE_INSENSITIVE_FS else key


def is_ancestor_or_self(parent: str, child: str) -> bool:
    """parent 是 child 的祖先或本身（两者都已过 overlap_key）。"""
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


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
    def of(cls, path: Path) -> ResourceIdentity:
        resolved = path.resolve()
        try:
            st = os.stat(resolved)
            if st.st_ino:
                return cls(key=f"{st.st_dev}:{st.st_ino}", path=str(resolved))
        except OSError:
            pass
        # 退化：Windows 或用不了 inode 的文件系统
        return cls(
            key="path:" + overlap_key(resolved),
            path=str(resolved),
        )

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
        if not self.root.is_dir():
            raise WorkspaceViolation(f"workspace 不存在或不是目录：{root}")

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
        return str(resolved.relative_to(self.root))


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