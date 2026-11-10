"""原子文件写入。

`write_bytes()` 会先截断再写，进程在这一刻被杀就是半截文件。
所以走「同目录临时文件 → fsync → os.replace → fsync 目录」。

os.replace 在同一文件系统内是原子的：要么看到旧文件，要么看到新文件，
不会看到一半。

**覆盖已有文件时保留原 mode。** `mkstemp` 建出来的临时文件是 0600，
`os.replace` 保留的是临时文件的 mode——不显式还原的话，覆盖一个 0644 的
用户文件会把它悄悄变成 0600。新建文件仍然维持 0600（私有），这是刻意的：
新建的内容默认不给同机其他用户读。

**四个持久化边界故障点**（`after-tmp-write` / `after-tmp-fsync` /
`after-rename` / `after-dir-fsync`）只在 `TIANSHU_FAILPOINT` 命中时硬退，
生产路径永不触发。它们把"断电可能发生在哪一步"钉成四个确定位置：

- `after-tmp-write`：数据已写进 tmp，未 fsync（真实断电可能连 tmp 内容都丢）；
- `after-tmp-fsync`：tmp 内容已持久，但名字还没发布（rename 未发生）；
- `after-rename`：新名字已发布，目录项还没 fsync（断电后 rename 可能回退）；
- `after-dir-fsync`：整个原子替换已按顺序持久化。

只有 `after-dir-fsync` 给了"rename 已持久"的保证；前三个都是**不确定窗口**，
调用方（恢复/撤销）必须能把它们当成"写前或写后"之一来处理。路径限定见
`crashpoint.py` 的 `TIANSHU_FAILPOINT_PATH`。
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .crashpoint import failpoint


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
            failpoint("after-tmp-write", path=target)
            os.fsync(fh.fileno())
            failpoint("after-tmp-fsync", path=target)
        if mode is not None:
            # 用 os.chmod 而不是 os.fchmod：后者在 Windows 上不存在，
            # 而这个文件要在四个目标平台上都能跑。
            os.chmod(tmp, mode)
        os.replace(tmp, target)
        failpoint("after-rename", path=target)
        _fsync_dir(target.parent)
        failpoint("after-dir-fsync", path=target)
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
