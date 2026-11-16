"""测试子进程的维护锁根注入。

生产代码不读取环境变量选择锁身份；这个启动钩子只存在于 `tests/`，并在
`TIANSHU_TEST_LOCK_ROOT` 明确存在时，于测试子进程真正导入 maintenance 后
换成当前用例的临时根，避免为了注入而预先加载无关模块。
"""

from __future__ import annotations

import os
import sys
from importlib import abc, machinery
from pathlib import Path

test_lock_root = os.environ.get("TIANSHU_TEST_LOCK_ROOT")


if test_lock_root:
    _test_lock_root = Path(test_lock_root)

    class _LockRootLoader(abc.Loader):
        def __init__(self, wrapped: abc.Loader) -> None:
            self._wrapped = wrapped

        def create_module(self, spec):
            return self._wrapped.create_module(spec)

        def exec_module(self, module) -> None:
            self._wrapped.exec_module(module)
            module._lock_root = lambda: _test_lock_root

    class _LockRootFinder(abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "tianshu.maintenance":
                return None
            spec = machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _LockRootLoader(spec.loader)
            return spec

    if "tianshu.maintenance" in sys.modules:
        sys.modules["tianshu.maintenance"]._lock_root = lambda: _test_lock_root
    else:
        sys.meta_path.insert(0, _LockRootFinder())
