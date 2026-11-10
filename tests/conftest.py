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

#: 用户级权威根的环境变量名。测试里必须钉死到 tmp_path：默认实现是
#: `~/.tianshu/authority`，不钉的话跑一次测试就会往开发者真实家目录写阻断
#: 记录，用例之间还会互相串味（A 用例挡住的目录在 B 用例里仍然挡着）。
AUTHORITY_ROOT_ENV = "TIANSHU_AUTHORITY_ROOT"


@pytest.fixture(autouse=True)
def _pin_authority_root(monkeypatch, tmp_path):
    """每个用例一个独立的用户级权威根；子进程继承同一个环境变量。

    `tmp_path` 由 pytest 自动清理，权威库不会残留。子进程用例（真进程接管、
    crashpoint）通过继承环境拿到同一个根，否则"另一个进程看不见阻断"会被
    误判成通过。
    """
    monkeypatch.setenv(AUTHORITY_ROOT_ENV, str(tmp_path / "authority"))
    yield


@pytest.fixture(autouse=True)
def _pin_local_backend(monkeypatch):
    monkeypatch.setenv(sandbox.ENV_VAR, sandbox.DEFAULT_BACKEND)
    sandbox.configure_backend(sandbox.DEFAULT_BACKEND)
    try:
        yield
    finally:
        # reset 同时清掉 memfs 单例的虚拟状态，避免跨用例残留。
        sandbox.reset_backend()
