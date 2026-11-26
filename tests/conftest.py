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

import os
from copy import deepcopy
from pathlib import Path

import pytest

from tianshu import maintenance, restore_registry, sandbox

#: 用户级权威根的环境变量名。测试里必须钉死到 tmp_path：默认实现是
#: `~/.tianshu/authority`，不钉的话跑一次测试就会往开发者真实家目录写阻断
#: 记录，用例之间还会互相串味（A 用例挡住的目录在 B 用例里仍然挡着）。
AUTHORITY_ROOT_ENV = "TIANSHU_AUTHORITY_ROOT"
TEST_LOCK_ROOT_ENV = "TIANSHU_TEST_LOCK_ROOT"
TEST_REGISTRY_HOME_ENV = "PYTEST_TIANSHU_REGISTRY_HOME"
TEST_STARTUP_DIR = Path(__file__).resolve().parent / "_startup"
SOURCE_DIR = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(scope="session")
def _scripted_eval_samples():
    # Only the empty container has session scope; execution needs test isolation.
    return []


@pytest.fixture
def scripted_eval_suite(tmp_path, _scripted_eval_samples):
    """Provide private copies of real full-suite, three-repeat observations."""
    from tianshu.eval import CASES, run_suite
    from tianshu.model import ScriptedModel

    def samples():
        if not _scripted_eval_samples:
            _scripted_eval_samples.append(run_suite(
                CASES,
                model_for=lambda case: ScriptedModel(list(case.script)),
                repeat=3,
                workdir=tmp_path / "scripted-eval",
                identity={"adapter": "scripted", "vendor": None, "model": ""},
            ))
        return deepcopy(_scripted_eval_samples[0])

    return samples


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


@pytest.fixture(autouse=True)
def _pin_tool_registry():
    """进程级工具注册表在用例之间必须复位。

    MCP 阶段把"内置 SPECS + 运行时准入"放进了一个进程级注册表（与
    `sandbox` 后端的进程级状态同一类）。一个用例装了动态工具，下一个用例不该
    继承它——否则"工具面没变"的判据会在别人的残留上假绿。
    """
    from tianshu.tool_registry import reset_registry

    reset_registry()
    try:
        yield
    finally:
        reset_registry()


@pytest.fixture(autouse=True)
def _net_rate_limit_off(monkeypatch):
    """网络速率下限在测试里默认关掉（TIANSHU_NET_MIN_INTERVAL_S=0）。

    限速是**进程级**状态：不关的话，同进程里先后跑的两个 fetch 用例会互相
    等待默认 1.0s，拖慢全套并让墙钟类判据互相串味。限速自身的判据在
    test_net_resilience.py 里显式 setenv 打开并自证（显式 NetLimits 构造
    参数优先于 env）。
    """
    monkeypatch.setenv("TIANSHU_NET_MIN_INTERVAL_S", "0")


@pytest.fixture(autouse=True)
def _pin_maintenance_lock_root(tmp_path_factory):
    """普通测试只使用用例私有的维护锁根。

    restore gate 是用户级生产锁：两个独立工作树并行跑全量时会按设计互斥。
    普通测试不应把这种跨套件争用误报成产品失败；专门验证 OS 用户级锁身份的
    模块在导入时冻结生产函数，并在用例内显式恢复它。
    """
    # 独立于当前用例的 `tmp_path`：部分测试会快照该目录并断言失败路径不产生
    # 任何文件，测试自身的锁文件不能混入被测目录。
    lock_root = tmp_path_factory.mktemp("maintenance-lock-root")
    registry_home = tmp_path_factory.mktemp("restore-registry-home")
    original_lock_root = maintenance._lock_root
    original_user_home = restore_registry._user_home
    previous_test_root = os.environ.get(TEST_LOCK_ROOT_ENV)
    previous_registry_home = os.environ.get(TEST_REGISTRY_HOME_ENV)
    previous_pythonpath = os.environ.get("PYTHONPATH")
    maintenance._lock_root = lambda: lock_root
    restore_registry._user_home = lambda: registry_home
    os.environ[TEST_LOCK_ROOT_ENV] = str(lock_root)
    os.environ[TEST_REGISTRY_HOME_ENV] = str(registry_home)
    existing = previous_pythonpath
    startup_paths = [str(TEST_STARTUP_DIR), str(SOURCE_DIR)]
    if existing:
        startup_paths.append(existing)
    os.environ["PYTHONPATH"] = os.pathsep.join(startup_paths)
    try:
        yield
    finally:
        # 这是测试套件的基础隔离，不能被测试内部的 `monkeypatch.undo()` 一并
        # 撤销；否则同一用例在 undo 之后又会撞到生产用户级 gate。
        maintenance._lock_root = original_lock_root
        restore_registry._user_home = original_user_home
        if previous_test_root is None:
            os.environ.pop(TEST_LOCK_ROOT_ENV, None)
        else:
            os.environ[TEST_LOCK_ROOT_ENV] = previous_test_root
        if previous_registry_home is None:
            os.environ.pop(TEST_REGISTRY_HOME_ENV, None)
        else:
            os.environ[TEST_REGISTRY_HOME_ENV] = previous_registry_home
        if previous_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = previous_pythonpath
