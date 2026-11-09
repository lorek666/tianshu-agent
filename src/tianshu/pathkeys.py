"""路径键：大小写 / Unicode 规范化与祖先判断。

这三个函数同时被三处需要：`Workspace`（边界）、`leases`（资源覆盖）、
`sandbox`（后端的资源身份）。单独成模块是为了让 sandbox **不 import
workspace**——后端回答"事实"，Workspace 回答"边界"，两者不该互相依赖。

`workspace.py` 仍然 re-export 这些名字，历史 import 路径（`from .workspace
import overlap_key`）不需要改。
"""

from __future__ import annotations

import os
import sys
import unicodedata

# 大小写不敏感的文件系统（Windows 默认、macOS 默认 APFS）上，
# 同一目录可能有多种写法；重叠判断必须折叠大小写，否则 /A 与 /a/b 会被当成两棵树。
CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"


def normalize_name(name: str) -> str:
    """macOS 的 APFS 存 NFD，Python 原样返回。比较前统一成 NFC。

    不处理会导致带中文/重音的文件名比对失败、UI 出现重复项。
    """
    return unicodedata.normalize("NFC", name)


def overlap_key(path: str) -> str:
    """用于判断"同一棵子树"的路径键（不是资源身份键）。"""
    key = os.path.normpath(normalize_name(str(path)))
    return key.casefold() if CASE_INSENSITIVE_FS else key


def is_ancestor_or_self(parent: str, child: str) -> bool:
    """parent 是 child 的祖先或本身（两者都已过 overlap_key）。"""
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


__all__ = [
    "CASE_INSENSITIVE_FS",
    "is_ancestor_or_self",
    "normalize_name",
    "overlap_key",
]
