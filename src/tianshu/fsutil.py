"""原子文件写入。

`write_bytes()` 会先截断再写，进程在这一刻被杀就是半截文件。
所以走「同目录临时文件 → fsync → os.replace → fsync 目录」。

os.replace 在同一文件系统内是原子的：要么看到旧文件，要么看到新文件，
不会看到一半。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fsync_dir(path: Path) -> None:
    """rename 的持久化需要目录 fsync，否则断电后目录项可能没落盘。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return  # Windows 不支持对目录 open，跳过
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
