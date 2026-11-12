"""F2 隔离请求与实测门验收（PRD §8.1.1 / §8.1）。

钉死的东西：

1. **没有要求 = 不探测**：默认策略不跑探针（执行路径 0 成本）；
2. **有要求 = 只认实测**：`unknown` 与 `not_enforced` 一样拒绝，声明不能发通行证；
3. **失败发生在 spawn 之前**：拒绝时后端一次都没被调用；
4. **报告必须绑定现场**：换 `data_root`/`workspace` 或塞别的现场的报告都拒绝；
5. **视图如实**：`level=none` 但有部分边界 enforced 时不能只说"受信任执行"；
6. **审批前可见**：`dispatch.requested` 与 `approval.requested` 都带同一视图，
   非 exec 的 auto 路径不带；
7. **契约**：边界名是封闭枚举，非法值写入前被拒；旧 payload（无字段）仍合法。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tianshu import isolation, tools
from tianshu.dispatcher import Dispatcher, build_request
from tianshu.errors import TianshuError
from tianshu.event_schema import PayloadContractError, validate_payload
from tianshu.events import EventLog
from tianshu.exec import LOCAL_EXEC_BACKEND, ExecResult, LocalExecBackend
from tianshu.exec_backends import (
    ExecBackend,
    exec_backend,
    register_exec_backend,
    reset_exec_backends,
)
from tianshu.execpolicy import (
    ISOLATION_BOUNDARIES,
    ExecBackendCapabilities,
    ExecPolicy,
)
from tianshu.isolation import (
    IsolationFinding,
    IsolationRefused,
    IsolationReport,
    isolation_view,
    probe_backend_cached,
    require_isolation_requirements,
    reset_probe_cache,
)
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.permissions import (
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore
from tianshu.workspace import Workspace

PY = sys.executable
WS = "ws"


# ---------------------------------------------------------------- 替身与报告


def _ok_result() -> ExecResult:
    return ExecResult(
        exit_code=0, signal=None, timed_out=False, stdout="ok\n", stderr="",
        truncated=False, stdout_ref=None, stderr_ref=None, duration_ms=1,
    )


class RecordingBackend:
    def __init__(self, *, capabilities=None, name=None):
        caps = capabilities or LOCAL_EXEC_BACKEND.capabilities
        if name is not None:
            caps = replace(caps, name=name)
        self.capabilities = caps
        self.calls = 0

    def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
        self.calls += 1
        return LOCAL_EXEC_BACKEND.spawn(
            argv, cwd=cwd, env=env, policy=policy, blob_dir=blob_dir, pty=pty
        )


def _report(
    workspace: Path | str,
    data_root: Path | str,
    *,
    files: str = "not_enforced",
    network: str = "not_enforced",
    credentials: str = "not_enforced",
    tree: str = "enforced",
    backend: str | None = None,
) -> IsolationReport:
    return IsolationReport(
        backend=backend or LOCAL_EXEC_BACKEND.capabilities.name,
        platform=isolation.platform_label(),
        findings=(
            IsolationFinding("files_authority", files, "evidence: files"),
            IsolationFinding("network", network, "evidence: network"),
            IsolationFinding("credentials", credentials, "evidence: credentials"),
            IsolationFinding("process_tree", tree, "evidence: tree"),
        ),
        workspace=str(Path(workspace).resolve()),
        data_root=str(Path(data_root).resolve()),
    )


def _policy(root: Path, **overrides) -> ExecPolicy:
    base = dict(
        argv_allowlist=(PY,),
        cwd_roots=(root,),
        env_allowlist=("PATH",),
        max_output_bytes=64 * 1024,
        timeout_ms=10_000,
    )
    base.update(overrides)
    return ExecPolicy(**base)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_exec_backends()
    reset_probe_cache()
    monkeypatch.delenv("TIANSHU_EXEC_BACKEND", raising=False)
    yield
    reset_exec_backends()
    reset_probe_cache()


# ---------------------------------------------------------------- 1. 默认 0 成本


def test_default_policy_requires_nothing(monkeypatch, tmp_path):
    """没有隔离要求时探针一次都不跑（默认执行路径没有这项成本）。"""
    calls = []
    monkeypatch.setattr(
        isolation, "probe_backend_cached",
        lambda *a, **k: calls.append(1) or pytest.fail("不该探测"),
    )

    assert require_isolation_requirements(
        _policy(tmp_path), LOCAL_EXEC_BACKEND,
        workspace=tmp_path, data_root=tmp_path,
    ) is None
    assert calls == []


def test_requirement_without_data_root_is_refused_without_probing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        isolation, "probe_backend_cached",
        lambda *a, **k: calls.append(1) or pytest.fail("没有数据根时不该探测"),
    )
    policy = _policy(tmp_path, isolation_requires=("files_authority",))

    with pytest.raises(IsolationRefused, match="数据根"):
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND, workspace=tmp_path, data_root=None
        )
    assert calls == []


# ---------------------------------------------------------------- 2. 只认实测


def test_requirement_passes_only_with_enforced_report(tmp_path):
    policy = _policy(tmp_path, isolation_requires=("process_tree",))
    report = _report(tmp_path, tmp_path)

    passed = require_isolation_requirements(
        policy, LOCAL_EXEC_BACKEND,
        workspace=tmp_path, data_root=tmp_path, report=report,
    )
    assert passed is report


@pytest.mark.parametrize("status", ["not_enforced", "unknown"])
def test_unproven_boundary_is_refused(tmp_path, status):
    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = _report(tmp_path, tmp_path, files=status)

    with pytest.raises(IsolationRefused) as excinfo:
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND,
            workspace=tmp_path, data_root=tmp_path, report=report,
        )
    assert excinfo.value.status == status


def test_declared_isolation_is_not_evidence(tmp_path):
    """后端自述 app_container，实测不是 → 门按实测拒绝（红线 9）。"""
    fake = RecordingBackend(
        capabilities=ExecBackendCapabilities(
            name="fake-container", isolation="app_container"
        )
    )
    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = _report(tmp_path, tmp_path, files="not_enforced", backend="fake-container")

    with pytest.raises(IsolationRefused):
        require_isolation_requirements(
            policy, fake, workspace=tmp_path, data_root=tmp_path, report=report
        )


def test_report_from_another_site_is_refused(tmp_path):
    """绕过缓存塞进来另一个数据根的报告：门必须拒（报告不是这次的证据）。"""
    other = tmp_path / "other-data"
    other.mkdir()
    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = _report(tmp_path, other)          # 现场是 other

    with pytest.raises(IsolationRefused, match="不属于本次探测现场"):
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND,
            workspace=tmp_path, data_root=tmp_path, report=report,
        )


# ---------------------------------------------------------------- 3. 视图


def test_partial_boundary_view_is_not_just_trusted(tmp_path):
    """门只要求 process_tree、实测 tree enforced：level 仍是 none，
    但视图必须带 enforced_boundaries（UI 不能只说"受信任执行"）。"""
    policy = _policy(tmp_path, isolation_requires=("process_tree",))
    report = _report(tmp_path, tmp_path)

    require_isolation_requirements(
        policy, LOCAL_EXEC_BACKEND,
        workspace=tmp_path, data_root=tmp_path, report=report,
    )
    view = isolation_view(LOCAL_EXEC_BACKEND, report)

    assert view["level"] == "none"
    assert view["enforced_boundaries"] == ["process_tree"]
    assert view["boundaries"]["files_authority"] == "not_enforced"
    assert view["unverified"] is False


def test_unverified_view_keeps_unknown_boundaries(tmp_path):
    view = isolation_view(LOCAL_EXEC_BACKEND)

    assert view["unverified"] is True
    assert view["level"] == "none"
    assert set(view["boundaries"].values()) == {"unknown"}


def test_contradicted_flag_is_report_wins(tmp_path):
    fake = RecordingBackend(
        capabilities=ExecBackendCapabilities(
            name="fake-container", isolation="app_container"
        )
    )
    report = _report(tmp_path, tmp_path, backend="fake-container")

    view = isolation_view(fake, report)
    assert view["declared"] == "app_container"
    assert view["level"] == "none"
    assert view["contradicted"] is True


# ---------------------------------------------------------------- 4. 缓存


def test_probe_cache_reuses_same_site(monkeypatch, tmp_path):
    calls = []

    def fake_probe(backend, *, workspace, data_root, timeout_ms):
        calls.append((str(workspace), str(data_root)))
        return _report(workspace, data_root)

    monkeypatch.setattr(isolation, "probe_backend", fake_probe)

    first = probe_backend_cached(
        LOCAL_EXEC_BACKEND, workspace=tmp_path, data_root=tmp_path
    )
    second = probe_backend_cached(
        LOCAL_EXEC_BACKEND, workspace=tmp_path, data_root=tmp_path
    )
    assert first is second
    assert len(calls) == 1


def test_probe_cache_separates_data_roots(monkeypatch, tmp_path):
    calls = []

    def fake_probe(backend, *, workspace, data_root, timeout_ms):
        calls.append(str(data_root))
        return _report(workspace, data_root)

    monkeypatch.setattr(isolation, "probe_backend", fake_probe)
    other = tmp_path / "other"
    other.mkdir()

    probe_backend_cached(LOCAL_EXEC_BACKEND, workspace=tmp_path, data_root=tmp_path)
    probe_backend_cached(LOCAL_EXEC_BACKEND, workspace=tmp_path, data_root=other)

    assert len(calls) == 2
    assert calls[0] != calls[1]


def test_probe_cache_is_invalidated_when_backend_object_is_replaced(monkeypatch, tmp_path):
    """同名 replace 之后解析到的是另一个对象：不能继承旧对象的 enforced。"""
    calls = []

    def fake_probe(backend, *, workspace, data_root, timeout_ms):
        calls.append(backend.capabilities.name)
        return _report(workspace, data_root, backend=backend.capabilities.name)

    monkeypatch.setattr(isolation, "probe_backend", fake_probe)

    first = RecordingBackend(name="fake")
    register_exec_backend(first, name="fake")
    probe_backend_cached(exec_backend("fake"), workspace=tmp_path, data_root=tmp_path)

    second = RecordingBackend(name="fake")
    register_exec_backend(second, name="fake", replace=True)
    probe_backend_cached(exec_backend("fake"), workspace=tmp_path, data_root=tmp_path)

    assert calls == ["fake", "fake"]


# ---------------------------------------------------------------- 5. 执行路径


def test_requirement_blocks_before_spawn(monkeypatch, tmp_path):
    backend = RecordingBackend()
    workspace = tmp_path / WS
    workspace.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setattr(
        isolation, "probe_backend_cached",
        lambda *a, **k: _report(workspace, data_root, files="not_enforced"),
    )
    policy = _policy(workspace, isolation_requires=("files_authority",))
    ctx = tools.ToolContext(
        exec_policy=policy, exec_backend=backend, data_root=data_root
    )

    result = tools._run_command(
        Workspace(workspace), {"argv": [PY, "-c", "print(1)"]}, ctx
    )

    assert result.status == "rejected"
    assert result.output["rule_id"] == "c4.exec_unsupported"
    assert backend.calls == 0
    view = result.output["isolation"]
    assert view["unverified"] is True          # 拒绝时没有报告 → 不冒充"已实测"
    assert view["level"] == "none"


def test_run_command_output_carries_isolation_view(tmp_path):
    backend = RecordingBackend()
    workspace = tmp_path / WS
    workspace.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    ctx = tools.ToolContext(
        exec_policy=_policy(workspace), exec_backend=backend, data_root=data_root
    )

    result = tools._run_command(
        Workspace(workspace), {"argv": [PY, "-c", "print(1)"]}, ctx
    )

    assert result.status == "succeeded"
    view = result.output["isolation"]
    assert view["backend"] == backend.capabilities.name
    assert view["level"] == "none"
    assert view["unverified"] is True
    assert set(view["boundaries"]) == set(ISOLATION_BOUNDARIES)


# ---------------------------------------------------------------- 6. 审批前可见


@dataclass
class Env:
    ws: Path
    data: Path
    log: EventLog
    leases: LeaseStore
    claims: JobStore
    tasks: TaskStore
    approvals: ApprovalStore
    grants: GrantStore
    snapshots: SnapshotStore
    dispatcher: Dispatcher

    def new_run(self, *, exec_policy):
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


def _make_env(tmp_path: Path, *, exec_backend: ExecBackend | None = None) -> Env:
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
            log, tasks, leases, PolicyEngine(), approvals,
            grants=grants, claims=claims, snapshots=snapshots,
            data_root=data, exec_backend=exec_backend,
        )
    except Exception:
        log.close()
        raise
    return Env(ws, data, log, leases, claims, tasks, approvals, grants, snapshots, dispatcher)


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


def _exec_request(env: Env, run, claim, *, args, task):
    return build_request(
        task=task, run=run, claim=claim, call_id="c1",
        tool_id="run_command", target=env.ws, args=args,
    )


def test_approval_before_sees_isolation_view(env_factory):
    backend = RecordingBackend()
    env = env_factory(exec_backend=backend)
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    args = {"argv": [PY, "-c", "print(1)"]}

    decision = env.dispatcher.prepare(
        _exec_request(env, run, claim, args=args, task=task), claim, args=args
    )

    assert decision.is_waiting, decision
    events = env.log.read_run(run.run_id)
    approval_payload = next(
        event.payload for event in events if event.type == "approval.requested"
    )
    dispatch_payload = next(
        event.payload for event in events if event.type == "dispatch.requested"
    )
    assert approval_payload["rule_id"] == decision.rule_id
    assert approval_payload["isolation"]["level"] == "none"
    assert approval_payload["isolation"]["unverified"] is True
    assert dispatch_payload["isolation"] == approval_payload["isolation"]


def test_non_exec_auto_dispatch_has_no_isolation_field(env_factory):
    env = env_factory()
    task, run, claim = env.new_run(exec_policy=None)
    args = {"name": "a.md", "content": "x"}
    request = build_request(
        task=task, run=run, claim=claim, call_id="c2",
        tool_id="create_report", target=env.ws / "a.md", args=args,
    )

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_admitted, decision
    payload = next(
        event.payload
        for event in env.log.read_run(run.run_id)
        if event.type == "dispatch.requested"
    )
    assert "isolation" not in payload


def test_precheck_rejects_unproven_requirement_before_approval(env_factory, monkeypatch):
    backend = RecordingBackend()
    env = env_factory(exec_backend=backend)
    monkeypatch.setattr(
        isolation, "probe_backend_cached",
        lambda *a, **k: pytest.fail("没有数据根就不该探测"),
    )
    monkeypatch.setattr(
        "tianshu.dispatcher.require_isolation_requirements",
        lambda policy, backend, *, workspace, data_root: (_ for _ in ()).throw(
            IsolationRefused(
                backend=backend.capabilities.name,
                boundary="files_authority",
                status="not_enforced",
                evidence="probe says no",
            )
        ),
    )
    policy = _policy(env.ws, isolation_requires=("files_authority",))
    task, run, claim = env.new_run(exec_policy=policy)
    args = {"argv": [PY, "-c", "print(1)"]}

    decision = env.dispatcher.prepare(
        _exec_request(env, run, claim, args=args, task=task), claim, args=args
    )

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_unsupported"
    assert backend.calls == 0
    types = [event.type for event in env.log.read_run(run.run_id)]
    assert "approval.requested" not in types
    assert "tool.intent" not in types


# ---------------------------------------------------------------- 7. 契约


def _task_payload(root: Path, **policy_overrides) -> dict:
    policy = _policy(root, **policy_overrides)
    return {
        "task_id": "task_1",
        "goal": "g",
        "goal_revision": 1,
        "lease_epoch": 1,
        "workspaces": [],
        "exec_policy": policy.to_dict(),
        "control": False,
        "memory_scopes": [],
        # G2 之后 task.created 的必填字段（与本轮无关，但契约要齐全）：
        # outbound_allow（网络轮）、mcp_servers（MCP 轮）、max_subtasks（委派 D1）。
        "outbound_allow": [],
        "mcp_servers": [],
        "max_subtasks": 0,
    }


def test_task_created_accepts_both_policy_shapes(tmp_path):
    old_policy = _policy(tmp_path).to_dict()
    old_policy.pop("isolation_requires")
    old = _task_payload(tmp_path)
    old["exec_policy"] = old_policy

    validate_payload("task.created", old)          # 旧 payload 仍合法
    validate_payload("task.created", _task_payload(tmp_path))  # 新 payload 合法


def _valid_isolation_view(**overrides) -> dict:
    view = {
        "backend": "local-posix",
        "declared": "none",
        "measured": None,
        "level": "none",
        "unverified": True,
        "contradicted": False,
        "boundaries": {
            "files_authority": "unknown",
            "network": "unknown",
            "credentials": "unknown",
            "process_tree": "unknown",
        },
        "enforced_boundaries": [],
    }
    view.update(overrides)
    return view


def test_isolation_view_contract_is_strict():
    base = {
        "request_id": "req_1",
        "request_digest": "0" * 64,
        "action": "exec",
        "resources": [],
        "decision": "approve",
        "rule_id": "c4.exec",
    }
    validate_payload("dispatch.requested", {**base, "isolation": _valid_isolation_view()})

    # 缺 measured：契约要求"没有报告"显式写 null，不允许缺字段。
    incomplete = _valid_isolation_view()
    incomplete.pop("measured")
    with pytest.raises(PayloadContractError):
        validate_payload("dispatch.requested", {**base, "isolation": incomplete})

    # 四边界必须齐全。
    missing_boundary = _valid_isolation_view()
    missing_boundary["boundaries"].pop("network")
    with pytest.raises(PayloadContractError):
        validate_payload("dispatch.requested", {**base, "isolation": missing_boundary})

    # 假边界名/假级别/假已证明边界都要在写入前拒绝。
    for bad in (
        {"boundaries": {**_valid_isolation_view()["boundaries"], "bogus": "unknown"}},
        {"declared": "super-safe"},
        {"enforced_boundaries": ["bogus"]},
        {"level": "partial"},
    ):
        with pytest.raises(PayloadContractError):
            validate_payload(
                "dispatch.requested",
                {**base, "isolation": _valid_isolation_view(**bad)},
            )


def test_illegal_boundary_is_rejected_before_write(tmp_path):
    payload = _task_payload(tmp_path)
    payload["exec_policy"]["isolation_requires"] = ["bogus"]

    with pytest.raises(PayloadContractError):
        validate_payload("task.created", payload)


# ---------------------------------------------------------------- 8. 契约/口往返


def test_require_isolation_roundtrips_through_exec_policy(tmp_path):
    policy = _policy(tmp_path, isolation_requires=("process_tree", "process_tree"))
    assert policy.isolation_requires == ("process_tree",)     # 去重保序

    again = ExecPolicy.from_dict(policy.to_dict())
    assert again is not None
    assert again.isolation_requires == ("process_tree",)

    with pytest.raises(ValueError, match="未知隔离边界"):
        ExecPolicy(isolation_requires=("bogus",))


def test_cli_policy_shape_carries_isolation_requires(tmp_path):
    from tianshu.cli import _exec_policy as cli_policy

    class Args:
        allow_exec_cmd = [PY]
        require_isolation = ["process_tree"]
        workspace = [tmp_path]
        readonly = None

    policy = cli_policy(Args())
    assert policy is not None
    assert policy.isolation_requires == ("process_tree",)

    class NoExec:
        allow_exec_cmd = None
        require_isolation = ["process_tree"]
        workspace = [tmp_path]
        readonly = None

    with pytest.raises(ValueError, match="allow-exec-cmd"):
        cli_policy(NoExec())


# ---------------------------------------------------------------- 9. 控制面往返


def test_control_task_create_carries_isolation_requirements(tmp_path):
    from tianshu.control import ControlError, ControlPlane

    policy = ControlPlane._exec_policy(
        {"allow_exec_cmd": [PY], "require_isolation": ["process_tree"]}, []
    )
    assert policy is not None
    assert policy.isolation_requires == ("process_tree",)

    with pytest.raises(ControlError) as excinfo:
        ControlPlane._exec_policy({"require_isolation": ["process_tree"]}, [])
    assert excinfo.value.code == "bad_params"

    with pytest.raises(ControlError) as excinfo:
        ControlPlane._exec_policy(
            {"allow_exec_cmd": [PY], "require_isolation": ["bogus"]}, []
        )
    assert excinfo.value.code == "bad_params"


def test_approval_view_shows_measured_report_when_requirement_passes(
    env_factory, monkeypatch
):
    """有实测报告时，审批前视图必须是报告的（unverified=False），不是声明的。"""
    backend = RecordingBackend()
    env = env_factory(exec_backend=backend)
    report = _report(env.ws, env.data)
    monkeypatch.setattr(
        "tianshu.dispatcher.require_isolation_requirements",
        lambda policy, backend_, *, workspace, data_root: report,
    )
    policy = _policy(env.ws, isolation_requires=("process_tree",))
    task, run, claim = env.new_run(exec_policy=policy)
    args = {"argv": [PY, "-c", "print(1)"]}

    decision = env.dispatcher.prepare(
        _exec_request(env, run, claim, args=args, task=task), claim, args=args
    )

    assert decision.is_waiting, decision
    payload = next(
        event.payload
        for event in env.log.read_run(run.run_id)
        if event.type == "approval.requested"
    )
    view = payload["isolation"]
    assert view["unverified"] is False
    assert view["enforced_boundaries"] == ["process_tree"]
    assert view["level"] == "none"


def test_control_approval_view_carries_isolation(tmp_path):
    """控制面 approvals.list 的视图里能读到审批时的隔离视图（PRD §8.1）。"""
    from support import stack_for

    from tianshu.control import ControlPlane
    from tianshu.loop import Limits
    from tianshu.model import ScriptedModel

    ws = tmp_path / WS
    ws.mkdir()
    log = EventLog(tmp_path / "events.db")
    try:
        stack = stack_for(log, data_root=tmp_path)
        task = stack.tasks.create_task(
            "跑一条命令", [{"root_path": str(ws)}], exec_policy=_policy(ws)
        )
        run = stack.tasks.start_run(task.task_id)
        stack.leases.acquire([ws], run.run_id)
        job = stack.claims.enqueue(run.run_id)
        claim = stack.claims.claim(job.job_id)
        stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        args = {"argv": [PY, "-c", "print(1)"]}
        request = build_request(
            task=task, run=run, claim=claim, call_id="c1",
            tool_id="run_command", target=ws, args=args,
        )
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.is_waiting, decision

        plane = ControlPlane(
            stack, model=ScriptedModel([]), limits=Limits(max_turns=3)
        )
        approvals = plane.handle(
            "approvals.list", {"run_id": run.run_id}
        )["approvals"]
        assert len(approvals) == 1
        isolation = approvals[0]["isolation"]
        assert isolation is not None
        assert isolation["level"] == "none"
        assert isolation["unverified"] is True
    finally:
        log.close()


# ---------------------------------------------------------------- 10. 报告必须属于这个后端/平台


def test_report_from_another_backend_is_refused(tmp_path):
    other = RecordingBackend(name="another-backend")
    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = _report(tmp_path, tmp_path, backend="local-posix")

    with pytest.raises(IsolationRefused, match="不属于本次探测现场"):
        require_isolation_requirements(
            policy, other, workspace=tmp_path, data_root=tmp_path, report=report
        )


def test_report_from_another_platform_is_refused(tmp_path):
    from dataclasses import replace as dc_replace

    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    other_platform = "macos-arm64" if sys.platform == "win32" else "windows-x64"
    report = dc_replace(_report(tmp_path, tmp_path), platform=other_platform)

    with pytest.raises(IsolationRefused, match="不属于本次探测现场"):
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND,
            workspace=tmp_path, data_root=tmp_path, report=report,
        )


def test_old_probe_version_is_refused(tmp_path):
    from dataclasses import replace as dc_replace

    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = dc_replace(_report(tmp_path, tmp_path), probe_version="v0")

    with pytest.raises(IsolationRefused, match="不属于本次探测现场"):
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND,
            workspace=tmp_path, data_root=tmp_path, report=report,
        )


# ---------------------------------------------------------------- 11. 无数据根也能构造/执行


def test_file_executor_without_data_root_constructs_and_runs_command(tmp_path):
    """没有数据根：构造不崩；**不声明隔离要求**的 run_command 真能跑。

    `Dispatcher` 执行文件工具时另有一道 data_root 必需的门（TOCTOU 串行化），
    与 F2 无关；这里测的是 F2 的路径（`_run_command` + FileExecutor 接线），
    避免把两件事混成一条"通过"。
    """
    from tianshu.tools import FileExecutor

    workspace = tmp_path / WS
    workspace.mkdir()
    log = EventLog(tmp_path / "events.db")
    try:
        leases = LeaseStore(log)
        claims = JobStore(log)
        tasks = TaskStore(log, leases=leases, claims=claims, data_root=None)
        approvals = ApprovalStore(log)
        grants = GrantStore(log)
        executor = FileExecutor(
            tasks=tasks,
            leases=leases,
            grants=grants,
            approvals=approvals,
            claims=claims,
            dispatch_lookup=lambda run_id, call_id: None,
            dispatch_mark=lambda run_id, call_id, state: None,
            clock=lambda: 0,
            log=log,
            exec_backend=LocalExecBackend(),
            data_root=None,
        )
        assert executor.data_root is None

        ctx = tools.ToolContext(
            exec_policy=_policy(workspace),
            exec_backend=LocalExecBackend(),
            data_root=None,
        )
        result = tools._run_command(
            Workspace(workspace), {"argv": [PY, "-c", "print(7)"]}, ctx
        )
        assert result.status == "succeeded"
        assert result.output["stdout"].strip() == "7"
        assert result.output["isolation"]["level"] == "none"
        assert result.output["isolation"]["unverified"] is True
    finally:
        log.close()


def test_precheck_and_execution_probe_the_same_workspace(env_factory, monkeypatch):
    """cwd 是子目录时，审批视图与执行视图必须来自同一个探测现场。"""
    backend = RecordingBackend()
    env = env_factory(exec_backend=backend)
    sub = env.ws / "sub"
    sub.mkdir()
    seen: list[tuple[str, str]] = []

    def fake_gate(policy, backend_, *, workspace, data_root):
        seen.append((str(Path(workspace).resolve()), str(Path(data_root).resolve())))
        return None

    monkeypatch.setattr("tianshu.dispatcher.require_isolation_requirements", fake_gate)
    monkeypatch.setattr("tianshu.tools.require_isolation_requirements", fake_gate)
    policy = _policy(env.ws, isolation_requires=("process_tree",))
    task, run, claim = env.new_run(exec_policy=policy)
    args = {"argv": [PY, "-c", "print(1)"], "cwd": "sub"}
    request = _exec_request(env, run, claim, args=args, task=task)

    decision = env.dispatcher.prepare(request, claim, args=args)
    assert decision.is_waiting, decision

    env.approvals.decide(
        decision.approval_id,
        decision="approved",
        actor=HumanIdentity.local_cli(),
    )
    env.dispatcher.dispatch(decision.request_id, claim)

    workspaces = {item[0] for item in seen}
    assert workspaces == {str(sub.resolve())}


# ---------------------------------------------------------------- 12. 预检路径错误与能力来源


def test_cwd_outside_workspace_is_a_rejection_not_a_run_failure(env_factory):
    """越界 cwd 必须是可解释的拒绝（工具结果/决策），不能让 prepare 抛裸异常。"""
    env = env_factory()
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    args = {"argv": [PY, "-c", "print(1)"], "cwd": "../outside"}
    request = _exec_request(env, run, claim, args=args, task=task)

    decision = env.dispatcher.prepare(request, claim, args=args)

    assert decision.is_rejected
    assert decision.rule_id == "c4.outside_workspace"
    assert "执行目录不可用" in decision.reason


def test_bogus_backend_isolation_level_is_rejected_at_construction():
    """能力声明与输出契约共用同一套级别枚举：bogus 不能进注册表。"""
    with pytest.raises(ValueError, match="未知隔离级别"):
        ExecBackendCapabilities(name="fake", isolation="bogus")
    with pytest.raises(ValueError, match="未知隔离级别"):
        ExecBackendCapabilities(name="fake", isolation="super-safe")


def test_dispatcher_refuses_executor_with_another_data_root(env_factory, tmp_path):
    from tianshu.tools import FileExecutor

    env = env_factory()
    other_root = tmp_path / "other-root"
    other_root.mkdir()
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
        data_root=other_root,
    )

    with pytest.raises(TianshuError, match="数据根"):
        Dispatcher(
            env.log,
            env.tasks,
            env.leases,
            PolicyEngine(),
            env.approvals,
            grants=env.grants,
            claims=env.claims,
            snapshots=env.snapshots,
            data_root=env.data,
            executor=other_executor,
        )


def test_cwd_with_wrong_type_is_a_rejection_not_a_crash(env_factory):
    """直接调 prepare 的旁路传非法 cwd 类型：也必须可判拒绝（TypeError 被映射）。"""
    env = env_factory()
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    args = {"argv": [PY, "-c", "print(1)"], "cwd": 123}
    request = _exec_request(env, run, claim, args=args, task=task)

    decision = env.dispatcher.prepare(request, claim, args=args)

    assert decision.is_rejected
    assert decision.rule_id == "c4.outside_workspace"


def test_report_with_another_timeout_is_refused(tmp_path):
    """证据强度绑定探测预算：换更短的超时重测得到的报告不能冒用。"""
    policy = _policy(tmp_path, isolation_requires=("files_authority",))
    report = replace(_report(tmp_path, tmp_path), timeout_ms=1234)

    with pytest.raises(IsolationRefused, match="不属于本次探测现场"):
        require_isolation_requirements(
            policy, LOCAL_EXEC_BACKEND,
            workspace=tmp_path, data_root=tmp_path, report=report,
        )
