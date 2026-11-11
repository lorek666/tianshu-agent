"""F2 执行后端注册表验收。

要钉死的四件事：

1. **默认 = local，选择可配置**：`exec_backend()` 默认解析到本地后端，
   `TIANSHU_EXEC_BACKEND` 能换到已注册的后端。
2. **做不到 = `unsupported`，绝不回落**：`app_container` / `container` /
   `seatbelt` / `landlock` 与拼错的名字都在**创建进程之前**抛
   `PolicyUnsupported`，不会悄悄换成 local 跑。
3. **产品路径真的经过注册表**：dispatcher 的预检与 `run_command` 的执行
   用的是同一个后端对象；构造期配置错误就在启动时炸，不拖到第一条命令。
4. **策略判定留在调用方**：后端不核策略时，调用方仍然拒绝；后端自己抛错
   不会被兜成"换本地重试"。

本文件不测隔离（那是 `test_isolation_gate.py` 与 `test_isolation.py` 的事）：
注册表能证明"交给谁"，不能证明"它隔离了什么"。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tianshu import tools
from tianshu.dispatcher import Dispatcher, build_request
from tianshu.errors import TianshuError, ToolNotAllowed
from tianshu.events import EventLog
from tianshu.exec import LOCAL_EXEC_BACKEND, ExecResult
from tianshu.exec_backends import (
    EXEC_BACKEND_ENV,
    exec_backend,
    register_exec_backend,
    reset_exec_backends,
)
from tianshu.execpolicy import ExecPolicy, PolicyUnsupported
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.permissions import ApprovalStore, GrantStore, HumanIdentity, PolicyEngine
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.tools import FileExecutor
from tianshu.workspace import Workspace

PY = sys.executable
WS = "ws"


# ---------------------------------------------------------------- 测试后端


def _ok_result() -> ExecResult:
    return ExecResult(
        exit_code=0,
        signal=None,
        timed_out=False,
        stdout="ok\n",
        stderr="",
        truncated=False,
        stdout_ref=None,
        stderr_ref=None,
        duration_ms=1,
    )


class RecordingBackend:
    """真执行（委托本地后端），同时记录被调用了几次、参数是什么。"""

    def __init__(self, *, capabilities=None, fail: Exception | None = None, name=None):
        caps = capabilities or LOCAL_EXEC_BACKEND.capabilities
        if name is not None:
            caps = replace(caps, name=name)
        self.capabilities = caps
        self.calls: list[tuple[tuple[str, ...], str, bool]] = []
        self._fail = fail
        self._delegate = LOCAL_EXEC_BACKEND

    def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
        self.calls.append((tuple(argv), str(cwd), pty))
        if self._fail is not None:
            raise self._fail
        return self._delegate.spawn(
            argv, cwd=cwd, env=env, policy=policy, blob_dir=blob_dir, pty=pty
        )


class SloppyBackend:
    """不核策略、直接"成功"的坏后端：用来证明调用方自己的判定没被删掉。"""

    capabilities = LOCAL_EXEC_BACKEND.capabilities

    def __init__(self) -> None:
        self.calls = 0

    def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
        self.calls += 1
        return _ok_result()


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """每个用例一份干净注册表 + 干净 env，避免互相污染。"""
    reset_exec_backends()
    monkeypatch.delenv(EXEC_BACKEND_ENV, raising=False)
    yield
    reset_exec_backends()


def _policy(ws: Path, **overrides) -> ExecPolicy:
    base = dict(
        argv_allowlist=(PY,),
        cwd_roots=(ws,),
        env_allowlist=("PATH",),
        max_output_bytes=64 * 1024,
        timeout_ms=10_000,
    )
    base.update(overrides)
    return ExecPolicy(**base)


@dataclass
class Env:
    ws: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def new_run(self, *, exec_policy: ExecPolicy | None):
        task = self.tasks.create_task(
            "跑一条命令", [{"root_path": str(self.ws)}], exec_policy=exec_policy
        )
        run = self.tasks.start_run(task.task_id)
        self.leases.acquire([self.ws], run.run_id)
        job = self.claims.enqueue(run.run_id)
        claim = self.claims.claim(job.job_id)
        self.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        self.snapshots.init_run(run.run_id)
        return task, run, claim


def _make_env(tmp_path: Path, *, exec_backend=None) -> Env:
    ws = tmp_path / WS
    ws.mkdir()
    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
    try:
        leases = LeaseStore(log)
        claims = JobStore(log)
        tasks = TaskStore(log, leases=leases, claims=claims, data_root=data)
        approvals = ApprovalStore(log)
        grants = GrantStore(log)
        snapshots = SnapshotStore(tmp_path / "artifacts")
        dispatcher = Dispatcher(
            log,
            tasks,
            leases,
            PolicyEngine(),
            approvals,
            grants=grants,
            claims=claims,
            snapshots=snapshots,
            data_root=data,
            exec_backend=exec_backend,
        )
    except Exception:
        log.close()
        raise
    return Env(ws, log, leases, claims, tasks, approvals, grants, snapshots, dispatcher)


@pytest.fixture
def env_factory(tmp_path):
    built: list[Env] = []

    def make(*, exec_backend=None) -> Env:
        env = _make_env(tmp_path, exec_backend=exec_backend)
        built.append(env)
        return env

    yield make
    for env in built:
        env.log.close()


def _dispatch_run_command(env: Env, *, policy: ExecPolicy, args: dict):
    task, run, claim = env.new_run(exec_policy=policy)
    request = build_request(
        task=task,
        run=run,
        claim=claim,
        call_id="c1",
        tool_id="run_command",
        target=env.ws,
        args=args,
    )
    decision = env.dispatcher.prepare(request, claim, args=args)
    if decision.is_rejected:
        return decision, None
    assert decision.is_waiting, decision
    env.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )
    result = env.dispatcher.dispatch(decision.request_id, claim)
    return decision, result


# ---------------------------------------------------------------- 1. 默认与选择


def test_default_backend_is_the_local_backend():
    assert exec_backend() is LOCAL_EXEC_BACKEND
    assert exec_backend("local") is LOCAL_EXEC_BACKEND


def test_env_selects_a_registered_backend(monkeypatch):
    fake = RecordingBackend()
    register_exec_backend(fake, name="fake")
    monkeypatch.setenv(EXEC_BACKEND_ENV, "fake")

    assert exec_backend() is fake


def test_empty_env_falls_back_to_local(monkeypatch):
    monkeypatch.setenv(EXEC_BACKEND_ENV, "   ")
    assert exec_backend() is LOCAL_EXEC_BACKEND


# ---------------------------------------------------------------- 2. 绝不回落


@pytest.mark.parametrize(
    "name", ["app_container", "container", "seatbelt", "landlock"]
)
def test_unimplemented_backend_is_unsupported_not_local(name):
    with pytest.raises(PolicyUnsupported) as excinfo:
        exec_backend(name)

    exc = excinfo.value
    assert exc.backend == name
    assert exc.feature == "exec_backend"
    # 理由必须说清"这台机器没有"，不能是含糊的"被拒绝"
    assert "v1" in exc.reason


def test_unimplemented_backend_selected_by_env_is_also_refused(monkeypatch):
    monkeypatch.setenv(EXEC_BACKEND_ENV, "container")
    with pytest.raises(PolicyUnsupported) as excinfo:
        exec_backend()
    assert excinfo.value.backend == "container"


def test_unknown_backend_is_refused_with_available_names():
    with pytest.raises(PolicyUnsupported) as excinfo:
        exec_backend("lcoal")
    assert "local" in str(excinfo.value)


def test_bad_backend_config_fails_at_startup_not_first_command(tmp_path, monkeypatch):
    """配置错误必须在装配期炸：拖到第一条命令才报，用户已经批过一条注定跑不了的命令。"""
    monkeypatch.setenv(EXEC_BACKEND_ENV, "container")
    with pytest.raises(PolicyUnsupported):
        _make_env(tmp_path)


def test_registering_a_known_unimplemented_name_requires_explicit_replace():
    """`TIANSHU_EXEC_BACKEND=app_container` 不能靠"注册一个别名"静默跑成别的后端。"""
    with pytest.raises(TianshuError, match="replace=True"):
        register_exec_backend(RecordingBackend(), name="container")

    # 即使用 replace=True，本地能力的后端也不能自报 container：名字与能力表
    # 必须一致，否则 F2-c2 的"如实分级"可以被一个注册名绕过。
    with pytest.raises(TianshuError, match="自报同一名字"):
        register_exec_backend(RecordingBackend(), name="container", replace=True)

    impl = RecordingBackend(name="container")
    register_exec_backend(impl, name="container", replace=True)

    from tianshu.exec_backends import UNIMPLEMENTED_EXEC_BACKENDS

    assert "container" not in UNIMPLEMENTED_EXEC_BACKENDS
    assert exec_backend("container") is impl


# ---------------------------------------------------------------- 3. 注册纪律


def test_registering_a_bad_backend_is_refused():
    with pytest.raises(TianshuError):
        register_exec_backend(object(), name="broken")

    class NoSpawn:
        capabilities = LOCAL_EXEC_BACKEND.capabilities

    with pytest.raises(TianshuError):
        register_exec_backend(NoSpawn(), name="no-spawn")

    class BadCaps:
        capabilities = {"name": "x", "isolation": "none"}

        def spawn(self, *args, **kwargs):  # pragma: no cover - 不该被调用
            raise AssertionError

    with pytest.raises(TianshuError):
        register_exec_backend(BadCaps(), name="bad-caps")

    class WrongShape:
        capabilities = LOCAL_EXEC_BACKEND.capabilities

        def spawn(self, argv):  # 少了 cwd/env/policy
            raise AssertionError

    with pytest.raises(TianshuError, match="cwd"):
        register_exec_backend(WrongShape(), name="wrong-shape")


def test_duplicate_registration_is_refused_unless_replaced():
    first, second = RecordingBackend(), RecordingBackend()
    register_exec_backend(first, name="dup")

    with pytest.raises(TianshuError):
        register_exec_backend(second, name="dup")

    register_exec_backend(second, name="dup", replace=True)
    assert exec_backend("dup") is second


# ---------------------------------------------------------------- 4. 产品路径


def test_run_command_uses_registered_backend(env_factory):
    recorded = RecordingBackend(name="recording")
    env = env_factory(exec_backend=recorded)

    decision, result = _dispatch_run_command(
        env, policy=_policy(env.ws), args={"argv": [PY, "-c", "print(1)"]}
    )

    assert result is not None, decision
    assert result.status == "succeeded"
    # 走的是注册表给的那个后端，不是模块里的本地函数
    assert len(recorded.calls) == 1
    assert result.output["policy"]["backend"] == "recording"


def test_precheck_uses_configured_backend_capabilities(env_factory):
    """后端声明没有 PTY 时，pty 请求必须在**审批之前**就报 unsupported。"""
    no_pty = replace(LOCAL_EXEC_BACKEND.capabilities, supports_pty=False)
    recorded = RecordingBackend(capabilities=no_pty)
    env = env_factory(exec_backend=recorded)

    decision, result = _dispatch_run_command(
        env,
        policy=_policy(env.ws, allow_pty=True),
        args={"argv": [PY, "-c", "print(1)"], "pty": True},
    )

    assert result is None
    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_unsupported"
    assert recorded.calls == []          # 一次都没执行


def test_backend_failure_is_not_retried_against_local(monkeypatch):
    """后端自己抛错时，异常照原样传出去；不换成 local 再试一次。

    把本地后端的 `spawn` 换成计数器：如果实现"先跑一次 local 再抛原异常"，
    `local_calls` 会变成 1，这条用例就红。
    """
    from tianshu.exec import LocalExecBackend

    local_calls: list[int] = []
    real_spawn = LocalExecBackend.spawn

    def spy(self, *args, **kwargs):
        local_calls.append(1)
        return real_spawn(self, *args, **kwargs)

    monkeypatch.setattr(LocalExecBackend, "spawn", spy)

    broken = RecordingBackend(fail=RuntimeError("后端坏了"))
    ws = Workspace(Path("/tmp"))  # 只用来解参数，不会真写
    ctx = tools.ToolContext(exec_policy=_policy(Path("/tmp")), exec_backend=broken)

    with pytest.raises(RuntimeError, match="后端坏了"):
        tools._run_command(ws, {"argv": [PY, "-c", "print(1)"]}, ctx)

    assert len(broken.calls) == 1
    assert local_calls == []


def test_pipeline_command_survives_policy_allow_pty_with_pty_less_backend(env_factory):
    """`allow_pty=True` 只是许可；本次 pty=False 时后端没有 PTY 也应当走管道。

    复审抓到的真实回归：无条件 `check_capabilities()` 会把"策略允许 PTY"读成
    "本次要用 PTY"，于是把一条正常命令拒掉。
    """
    no_pty = replace(LOCAL_EXEC_BACKEND.capabilities, supports_pty=False)
    recorded = RecordingBackend(capabilities=no_pty)
    env = env_factory(exec_backend=recorded)

    decision, result = _dispatch_run_command(
        env,
        policy=_policy(env.ws, allow_pty=True),
        args={"argv": [PY, "-c", "print(42)"]},
    )

    assert result is not None, decision
    assert result.status == "succeeded"
    assert result.output["stdout"].strip() == "42"
    assert result.output["policy"]["pty"] is False
    assert len(recorded.calls) == 1


def test_policy_is_checked_by_caller_even_if_backend_skips_it():
    """后端不核策略（SloppyBackend）时，调用方仍然先拒绝，且一次都没执行。"""
    ws = Workspace(Path("/tmp"))
    sloppy = SloppyBackend()
    ctx = tools.ToolContext(exec_policy=ExecPolicy(), exec_backend=sloppy)

    result = tools._run_command(ws, {"argv": [PY, "-c", "print(1)"]}, ctx)

    # 拒绝走的是 C2 的结果形状（rejected），不是把异常抛给模型
    assert result.status == "rejected"
    assert result.output["rule_id"] == "f2.argv_not_allowed"
    assert sloppy.calls == 0


def test_caller_check_order_matches_spawn():
    """argv 与 pty 同时非法时，先报的规则必须与 `exec.spawn()` 内部一致。"""
    sloppy = SloppyBackend()
    ctx = tools.ToolContext(exec_policy=ExecPolicy(), exec_backend=sloppy)
    ws = Workspace(Path("/tmp"))

    result = tools._run_command(
        ws, {"argv": ["/bin/sh", "-c", "true"], "pty": True}, ctx
    )

    # 空策略先拒绝 argv（deny 优先）；不能因为带了 pty 就先报 f2.pty_not_allowed
    assert result.status == "rejected"
    assert result.output["rule_id"] == "f2.argv_not_allowed"
    assert sloppy.calls == 0


def test_dispatcher_rejects_bad_argv_before_backend_runs(env_factory):
    """派发层（含审批前预检）的拒绝也发生在后端创建进程之前。"""
    sloppy = SloppyBackend()
    env = env_factory(exec_backend=sloppy)
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    args = {"argv": ["/bin/sh", "-c", "true"]}      # 不在 allowlist 里
    request = build_request(
        task=task, run=run, claim=claim, call_id="c1",
        tool_id="run_command", target=env.ws, args=args,
    )

    decision = env.dispatcher.prepare(request, claim, args=args)

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_policy_denied"
    assert sloppy.calls == 0


def test_pty_policy_is_checked_by_caller_even_if_backend_skips_it():
    ws = Workspace(Path("/tmp"))
    sloppy = SloppyBackend()
    ctx = tools.ToolContext(
        exec_policy=_policy(Path("/tmp")), exec_backend=sloppy
    )

    result = tools._run_command(
        ws, {"argv": [PY, "-c", "print(1)"], "pty": True}, ctx
    )

    assert result.status == "rejected"
    assert result.output["rule_id"] == "f2.pty_not_allowed"
    assert sloppy.calls == 0


def test_executor_refuses_after_default_exec_backend_changes(env_factory, monkeypatch):
    """默认解析出来的执行后端在执行前被换掉：拒绝，不混用两个世界。

    装配层显式指定的后端（dispatcher 传给 FileExecutor 的那个）不在此检查范围：
    预检与执行拿到的是同一个对象，由装配层保证。
    """
    env = env_factory()
    executor = FileExecutor(
        tasks=env.tasks,
        leases=env.leases,
        grants=env.grants,
        approvals=env.approvals,
        claims=env.claims,
        dispatch_lookup=env.dispatcher.dispatch_record,
        dispatch_mark=env.dispatcher.mark_dispatch,
        clock=lambda: 0,
        log=env.log,
        snapshots=env.snapshots,
        exec_backend=None,          # 自己解析：构造时是 local
    )

    register_exec_backend(RecordingBackend(), name="fake")
    monkeypatch.setenv(EXEC_BACKEND_ENV, "fake")

    with pytest.raises(ToolNotAllowed, match="执行后端在 FileExecutor 构造后被替换"):
        executor.execute(object(), None)


def test_dispatcher_refuses_executor_with_another_exec_backend(env_factory):
    """Dispatcher(executor=...) 的旁路也要核：预检与执行不能是两个后端。"""
    env = env_factory()
    other_executor = FileExecutor(
        tasks=env.tasks,
        leases=env.leases,
        grants=env.grants,
        approvals=env.approvals,
        claims=env.claims,
        dispatch_lookup=env.dispatcher.dispatch_record,
        dispatch_mark=env.dispatcher.mark_dispatch,
        clock=lambda: 0,
        log=env.log,
        snapshots=env.snapshots,
        exec_backend=RecordingBackend(name="other"),
    )

    with pytest.raises(TianshuError, match="执行器"):
        Dispatcher(
            env.log,
            env.tasks,
            env.leases,
            PolicyEngine(),
            env.approvals,
            grants=env.grants,
            claims=env.claims,
            snapshots=env.snapshots,
            data_root=env.tasks.data_root,
            executor=other_executor,
        )
