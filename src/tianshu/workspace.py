"""Workspace 边界：所有路径先规范化，再与 root 比较。

这是 PRD §8.1 的 L1 层——跨平台唯一真正一致的那层。
不做字符串前缀比较（会被 `..`、符号链接、大小写绕过），只做 resolve 后比较。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .errors import WorkspaceViolation


def normalize_name(name: str) -> str:
    """macOS 的 APFS 存 NFD，Python 原样返回。比较前统一成 NFC。

    不处理会导致带中文/重音的文件名比对失败、UI 出现重复项。
    """
    return unicodedata.normalize("NFC", name)


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

    def relative(self, resolved: Path) -> str:
        return str(resolved.relative_to(self.root))
