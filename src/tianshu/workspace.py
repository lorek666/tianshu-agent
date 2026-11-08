"""Workspace 边界：所有路径先规范化，再与 root 比较。

这是 PRD §8.1 的 L1 层——跨平台唯一真正一致的那层。
不做字符串前缀比较（会被 `..`、符号链接、大小写绕过），只做 resolve 后比较。
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceViolation


def normalize_name(name: str) -> str:
    """macOS 的 APFS 存 NFD，Python 原样返回。比较前统一成 NFC。

    不处理会导致带中文/重音的文件名比对失败、UI 出现重复项。
    """
    return unicodedata.normalize("NFC", name)


@dataclass(frozen=True)
class ResourceIdentity:
    """稳定的目录身份。

    不能用路径字符串当身份：符号链接、大小写、Unicode 规范化都能让
    同一个目录出现多个写法。用 (st_dev, st_ino) 才是真的"同一个东西"。
    Windows 上 st_ino 在部分文件系统为 0，退回规范化路径。
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
            key="path:" + normalize_name(str(resolved)).lower(),
            path=str(resolved),
        )


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
