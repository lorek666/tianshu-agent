"""测试级公共夹具。

后端是**进程级**状态（见 `tianshu/sandbox.py`）：一个用例选了 memfs，下一个
用例不该继承它。所以每个用例都钉在 `local_fs` 上，做两件事：

1. 显式 `configure_backend(local_fs)`——不是"回到按 env 解析"。如果 CI 或
   本地环境恰好带着 `TIANSHU_SANDBOX_BACKEND=memfs`，不钉的话整套测试会
   集体撞 `WorkspaceViolation: memfs 后端只接受虚拟根`，看起来像 M1/M2
   大回归，实际只是测试环境串味。
2. 同时把 env 钉成 `local_fs`：部分用例会 spawn 子进程（worker / CLI /
   租约），子进程继承 `os.environ`，不钉的话它们会拿到 memfs 并在真实
   目录上失败。

要测 env 通道的用例（`test_sandbox_backend.py` 里的两条）自己显式
`setenv`，monkeypatch 会在用例结束后恢复正常。
"""

from __future__ import annotations

import pytest

from tianshu import sandbox


@pytest.fixture(autouse=True)
def _pin_local_backend(monkeypatch):
    monkeypatch.setenv(sandbox.ENV_VAR, sandbox.DEFAULT_BACKEND)
    sandbox.configure_backend(sandbox.DEFAULT_BACKEND)
    try:
        yield
    finally:
        # reset 同时清掉 memfs 单例的虚拟状态，避免跨用例残留。
        sandbox.reset_backend()
