"""测试级公共夹具。

后端是**进程级**状态（见 `tianshu/sandbox.py`）：一个用例选了 memfs，下一个
用例不该继承它，否则会出现"审批读 local、写入写 memfs"这类串扰，而且症状
取决于测试顺序、极难复现。所以每个用例前后各复位一次。
"""

from __future__ import annotations

import pytest

from tianshu import sandbox


@pytest.fixture(autouse=True)
def _reset_sandbox_backend():
    sandbox.reset_backend()
    try:
        yield
    finally:
        sandbox.reset_backend()
