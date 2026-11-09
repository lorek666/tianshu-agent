"""原子文件写入。

`write_bytes()` 会先截断再写，进程在这一刻被杀就是半截文件。
所以走「同目录临时文件 → fsync → os.replace → fsync 目录」。

os.replace 在同一文件系统内是原子的：要么看到旧文件，要么看到新文件，
不会看到一半。

**覆盖已有文件时保留原 mode。** `mkstemp` 建出来的临时文件是 0600，
`os.replace` 保留的是临时文件的 mode——不显式还原的话，覆盖一个 0644 的
用户文件会把它悄悄变成 0600。新建文件仍然维持 0600（私有），这是刻意的：
新建的内容默认不给同机其他用户读。
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _existing_mode(target: Path) -> int | None:
    """目标已存在就返回它的权限位；不存在（或读不到）返回 None。"""
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        return None


def atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = _existing_mode(target)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            # 用 os.chmod 而不是 os.fchmod：后者在 Windows 上不存在，
            # 而这个文件要在四个目标平台上都能跑。
            os.chmod(tmp, mode)
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
