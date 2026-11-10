"""故障注入点。

用 `os._exit` 硬退，模拟断电/被 kill —— 不走任何清理逻辑、不给恢复代码机会。
只在设置了 `TIANSHU_FAILPOINT` 时生效，生产路径永远不触发。

它单独成模块而不是留在 `loop.py`：M2 之后写入路径是
`loop → dispatcher → FileExecutor`，四个故障点分散在三个文件里，
任何两个模块互相 import 都会成环。

`only_if` 用来把注入限定在有副作用的工具上——否则第一个只读工具就会引爆，
测不到真正想测的写入路径。

`path` + `TIANSHU_FAILPOINT_PATH` 是**持久化边界注入**用的第二道限定：
`fsutil.atomic_write_bytes` 在一次 Run 里会被调用很多次（快照清单、blob、目标
文件……），只按名字注入的话第一个命中的多半是快照清单，测不到"目标文件写到哪
一步断电"。设了 `TIANSHU_FAILPOINT_PATH` 时，只有 `atomic_write_bytes` 的
目标就是那一个路径（解析后逐字符相等）才引爆；不设则行为与以前完全一致。
两个变量都只存在测试进程里，生产路径永远不会设置它们。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _path_scoped(path: str | os.PathLike[str] | None) -> bool:
    """`TIANSHU_FAILPOINT_PATH` 为空 = 不限路径；否则只认解析后相等的那个目标。"""
    scope = os.environ.get("TIANSHU_FAILPOINT_PATH")
    if not scope:
        return True
    if path is None:
        return False
    try:
        return Path(os.fspath(path)).resolve() == Path(scope).resolve()
    except (OSError, TypeError, ValueError):
        # 路径解析不出来的注入点一发都不许炸：宁可这次测不到，也不能在
        # 一个"不知道是不是目标"的写入上制造假崩溃。
        return False


def failpoint(
    name: str,
    *,
    only_if: bool = True,
    path: str | os.PathLike[str] | None = None,
) -> None:
    if (
        only_if
        and os.environ.get("TIANSHU_FAILPOINT") == name
        and _path_scoped(path)
    ):
        sys.stderr.write(f"[failpoint] {name}\n")
        sys.stderr.flush()
        os._exit(77)


# M1 的测试与文档用的是这个私有名字；保留别名，别为了改个名字动历史证据。
_failpoint = failpoint

__all__ = ["failpoint"]
