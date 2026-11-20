"""测试子进程的维护锁根与恢复注册表 Home 注入。

生产代码不读取环境变量选择锁身份或恢复注册表 Home；这个启动钩子只存在于
`tests/`，并在 `TIANSHU_TEST_LOCK_ROOT` / `PYTEST_TIANSHU_REGISTRY_HOME`
明确存在时，于测试子进程真正导入对应模块后换成当前用例的临时根，避免为了
注入而预先加载无关模块，也防止子进程触碰宿主机用户级真实恢复权威目录。
"""

from __future__ import annotations

import os
import sys
from importlib import abc, machinery
from pathlib import Path

test_lock_root = os.environ.get("TIANSHU_TEST_LOCK_ROOT")
test_registry_home = os.environ.get("TIANSHU_TEST_REGISTRY_HOME") or os.environ.get(
    "PYTEST_TIANSHU_REGISTRY_HOME"
)


if test_lock_root or test_registry_home:
    _test_lock_root = Path(test_lock_root) if test_lock_root else None
    _test_registry_home = Path(test_registry_home) if test_registry_home else None

    class _TestIsolationLoader(abc.Loader):
        def __init__(self, wrapped: abc.Loader, fullname: str) -> None:
            self._wrapped = wrapped
            self._fullname = fullname

        def create_module(self, spec):
            return self._wrapped.create_module(spec)

        def exec_module(self, module) -> None:
            self._wrapped.exec_module(module)
            if self._fullname == "tianshu.maintenance" and _test_lock_root is not None:
                module._lock_root = lambda: _test_lock_root
            elif (
                self._fullname == "tianshu.restore_registry"
                and _test_registry_home is not None
            ):
                module._user_home = lambda: _test_registry_home

    class _TestIsolationFinder(abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "tianshu.maintenance" and _test_lock_root is not None:
                pass
            elif (
                fullname == "tianshu.restore_registry"
                and _test_registry_home is not None
            ):
                pass
            else:
                return None
            spec = machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and spec.loader is not None:
                spec.loader = _TestIsolationLoader(spec.loader, fullname)
            return spec

    if _test_lock_root is not None and "tianshu.maintenance" in sys.modules:
        sys.modules["tianshu.maintenance"]._lock_root = lambda: _test_lock_root
    if _test_registry_home is not None and "tianshu.restore_registry" in sys.modules:
        sys.modules["tianshu.restore_registry"]._user_home = lambda: _test_registry_home
    sys.meta_path.insert(0, _TestIsolationFinder())
