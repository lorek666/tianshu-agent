"""故障注入点。

用 `os._exit` 硬退，模拟断电/被 kill —— 不走任何清理逻辑、不给恢复代码机会。
只在设置了 `TIANSHU_FAILPOINT` 时生效，生产路径永远不触发。

它单独成模块而不是留在 `loop.py`：M2 之后写入路径是
`loop → dispatcher → FileExecutor`，四个故障点分散在三个文件里，
任何两个模块互相 import 都会成环。

`only_if` 用来把注入限定在有副作用的工具上——否则第一个只读工具就会引爆，
测不到真正想测的写入路径。
"""

from __future__ import annotations

import os
import sys


def failpoint(name: str, *, only_if: bool = True) -> None:
    if only_if and os.environ.get("TIANSHU_FAILPOINT") == name:
        sys.stderr.write(f"[failpoint] {name}\n")
        sys.stderr.flush()
        os._exit(77)


# M1 的测试与文档用的是这个私有名字；保留别名，别为了改个名字动历史证据。
_failpoint = failpoint

__all__ = ["failpoint"]
