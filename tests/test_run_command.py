"""run_command 验收：C2 工具 + C4 exec 动作 + 任务级 ExecPolicy + 审批。

要钉死的几件事：

1. **默认全拒**：任务没配置 ExecPolicy 时，模型提案 run_command 会得到
   `c4.exec_not_configured`——不是"工具不存在"，也不是"默认放行 python/bash"。
2. **显式开启才可能执行**：`task create --allow-exec-cmd` 等价路径；
   且每次执行仍需人工审批（`c4.exec`）。
3. **未批准 = 没执行**：不得创建子进程、不得改文件系统。
4. 结果字段完整：exit_code / signal / timed_out / truncated / stdout_ref /
   stderr_ref / stderr_merged / duration_ms 都在 `tool.result` 里。
5. **replay 不得重跑**：执行是副作用，回放只读事件。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.dispatcher import Dispatcher, build_request
from tianshu.events import EventLog
from tianshu.execpolicy import ExecPolicy
from tianshu.jobs import JobStore
from tianshu.leases import LeaseStore
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import (
    ApprovalStore,
    GrantStore,
    HumanIdentity,
    PolicyEngine,
)
from tianshu.receipt import receipts_of, verify_receipts
from tianshu.replay import replay
from tianshu.snapshot import SnapshotStore
from tianshu.tasks import RUN_RUNNING, TaskStore

PY = sys.executable
WS = "ws"


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


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / WS
    ws.mkdir()
    data = tmp_path / "authority"
    data.mkdir()
    log = EventLog(tmp_path / "events.db")
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
    )
    built = Env(ws, log, leases, claims, tasks, approvals, grants, snapshots, dispatcher)
    try:
        yield built
    finally:
        log.close()


def _request(env: Env, task, run, claim, *, args, call_id="c1", cwd=None):
    return build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id,
        tool_id="run_command",
        target=env.ws if cwd is None else cwd,
        args=args,
    )


def _prepare(env: Env, task, run, claim, *, args, cwd=None, call_id="c1"):
    request = _request(
        env, task, run, claim, args=args, cwd=cwd, call_id=call_id
    )
    return request, env.dispatcher.prepare(request, claim, args=args)


def _approve(env: Env, approval_id: str) -> None:
    env.approvals.decide(
        approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )


def _run_command(env: Env, *, args, policy: ExecPolicy | None, approve: bool = True):
    task, run, claim = env.new_run(exec_policy=policy)
    request, decision = _prepare(env, task, run, claim, args=args)
    if decision.is_rejected:
        return run, decision, None, request
    assert decision.is_waiting, decision
    if approve:
        _approve(env, decision.approval_id)
        result = env.dispatcher.dispatch(decision.request_id, claim)
        return run, decision, result, request
    return run, decision, None, request


def _types(log: EventLog, run_id: str) -> list[str]:
    return [event.type for event in log.read_run(run_id)]


# ---------------------------------------------------------------- 默认全拒


def test_default_policy_rejects_with_a_policy_reason(env):
    """没配置 exec 策略：拒绝理由必须指向策略，而不是"工具不存在"。"""
    task, run, claim = env.new_run(exec_policy=None)
    request, decision = _prepare(
        env, task, run, claim, args={"argv": [PY, "-c", "print(1)"]}
    )

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_not_configured"
    assert "执行策略" in decision.reason
    assert "工具" not in decision.reason.replace("run_command", "")
    # 连审批都不该发起：用户不该为一条注定跑不了的命令签字
    assert env.approvals.pending(run_id=run.run_id) == []
    assert "tool.intent" not in _types(env.log, run.run_id)
    assert env.dispatcher.dispatch_record(run.run_id, request.call_id) is None


def test_empty_policy_denies_every_command(env):
    """空 ExecPolicy 也要全拒：白名单为空、cwd 根为空、上限未声明。"""
    empty = ExecPolicy()
    task, run, claim = env.new_run(exec_policy=empty)
    _, decision = _prepare(env, task, run, claim, args={"argv": [PY, "-c", "print(1)"]})

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_policy_denied"


# ---------------------------------------------------------------- 显式开启


def test_configured_policy_runs_and_reports_all_fields(env):
    policy = _policy(env.ws)
    run, _, result, _ = _run_command(env, args={"argv": [PY, "-c", "print(1)"]}, policy=policy)

    assert result is not None
    assert result.status == "succeeded"
    assert result.effect_state == "applied"
    out = result.output
    for field in (
        "exit_code",
        "signal",
        "timed_out",
        "truncated",
        "stdout_ref",
        "stderr_ref",
        "stderr_merged",
        "duration_ms",
    ):
        assert field in out, f"tool.result 缺审计字段：{field}"
    assert out["exit_code"] == 0
    assert out["signal"] is None
    assert out["timed_out"] is False
    # Windows 的 Python 文本模式会把 \n 写成 \r\n；这是子进程的行为，
    # 不是我们的管道做了转换。按行归一化后比较内容。
    assert out["stdout"].replace("\r\n", "\n") == "1\n"
    assert out["stderr_merged"] is False
    assert out["path"] == "."

    # 事件里记下了 policy 判定结果
    results = [e.payload for e in env.log.read_run(run.run_id) if e.type == "tool.result"]
    assert results[-1]["output"]["policy"]["decision"] == "allowed"
    requested = [e.payload for e in env.log.read_run(run.run_id) if e.type == "dispatch.requested"]
    assert requested[-1]["rule_id"] == "c4.exec"


def test_exec_requires_approval(env):
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    _, decision = _prepare(env, task, run, claim, args={"argv": [PY, "-c", "print(1)"]})

    assert decision.is_waiting
    assert env.approvals.pending(run_id=run.run_id)


def test_nonzero_exit_is_reported_not_swallowed(env):
    run, _, result, _ = _run_command(
        env, args={"argv": [PY, "-c", "import sys; sys.exit(3)"]}, policy=_policy(env.ws)
    )

    assert result is not None
    assert result.status == "failed"
    assert result.output["exit_code"] == 3
    assert result.output["timed_out"] is False


def test_timeout_is_reported_as_timeout(env):
    policy = _policy(env.ws, timeout_ms=300)
    run, _, result, _ = _run_command(
        env, args={"argv": [PY, "-c", "import time; time.sleep(30)"]}, policy=policy
    )

    assert result is not None
    assert result.output["timed_out"] is True
    assert result.output["exit_code"] is None
    assert result.output["duration_ms"] < 10_000


def test_truncated_output_is_persisted_as_blob(env):
    policy = _policy(env.ws, max_output_bytes=100)
    _, _, result, _ = _run_command(
        env,
        args={"argv": [PY, "-c", "import sys; sys.stdout.buffer.write(b'x' * 5000)"]},
        policy=policy,
    )

    assert result is not None
    out = result.output
    assert out["truncated"] is True
    assert out["stdout_ref"] is not None
    blob = env.snapshots.blob_dir / out["stdout_ref"]
    assert blob.read_bytes() == b"x" * 5000


def test_env_whitelist_hides_unlisted_variables(env, monkeypatch):
    monkeypatch.setenv("TIANSHU_TEST", "yes")
    monkeypatch.setenv("SECRET_SAUCE", "leak")
    policy = _policy(env.ws, env_allowlist=("PATH", "TIANSHU_TEST"))
    _, _, result, _ = _run_command(
        env,
        args={"argv": [PY, "-c", "import os; print(','.join(sorted(os.environ)))"]},
        policy=policy,
    )

    keys = set(result.output["stdout"].strip().split(","))
    assert "TIANSHU_TEST" in keys
    assert "SECRET_SAUCE" not in keys


# ---------------------------------------------------------------- 拒绝路径


def test_string_argv_is_rejected_by_policy_not_shell(env):
    """字符串命令不是被拆词，而是被 exec 策略拒绝。"""
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    _, decision = _prepare(
        env, task, run, claim, args={"argv": f"{PY} -c print(1)"}
    )

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_policy_denied"
    assert "argv" in decision.reason or "数组" in decision.reason


def test_executable_outside_allowlist_is_rejected(env):
    policy = _policy(env.ws, argv_allowlist=("/usr/bin/echo",))
    task, run, claim = env.new_run(exec_policy=policy)
    _, decision = _prepare(env, task, run, claim, args={"argv": [PY, "-c", "print(1)"]})

    assert decision.is_rejected
    assert decision.rule_id == "c4.exec_policy_denied"


def test_cwd_outside_workspace_is_rejected(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    _, decision = _prepare(
        env, task, run, claim, args={"argv": [PY, "-c", "print(1)"]}, cwd=outside
    )

    assert decision.is_rejected
    assert decision.rule_id in {
        "dispatch.lease_lost",
        "c4.outside_workspace_unsupported",
        "c4.exec_policy_denied",
    }


# ---------------------------------------------------------------- 未批准


def test_unapproved_command_is_not_executed(env):
    marker = env.ws / "marker.txt"
    code = f"open({str(marker)!r}, 'w').write('ran')"

    # 第一条 Run：只 prepare、不批准，然后尝试派发 → 必须在执行前被拒
    task, run, claim = env.new_run(exec_policy=_policy(env.ws))
    _, decision = _prepare(env, task, run, claim, args={"argv": [PY, "-c", code]})
    assert decision.is_waiting

    result = env.dispatcher.dispatch(decision.request_id, claim)

    assert result.status == "rejected"
    assert result.effect_state == "none_applied"
    assert not marker.exists(), "未批准却执行了命令"
    assert "tool.intent" not in _types(env.log, run.run_id)

    # 批准之后才会真的执行：重新拿一张请求（旧请求已被上一步拒掉）
    _, decision2 = _prepare(
        env, task, run, claim, args={"argv": [PY, "-c", code]}, call_id="c2"
    )
    assert decision2.is_waiting
    _approve(env, decision2.approval_id)
    result = env.dispatcher.dispatch(decision2.request_id, claim)
    assert result.status == "succeeded"
    assert marker.read_text(encoding="utf-8") == "ran"


# ---------------------------------------------------------------- 凭据


def test_exec_receipt_is_auditable_and_verifies(env):
    run, _, result, _ = _run_command(
        env, args={"argv": [PY, "-c", "print(1)"]}, policy=_policy(env.ws)
    )

    payloads = receipts_of(env.log, run.run_id)
    assert len(payloads) == 1
    receipt = payloads[0]
    assert receipt["action"] == "exec"
    assert receipt["before_state"] == "unknown"
    assert receipt["after_state"].startswith("exec:")
    assert receipt["resource"] == str(env.ws.resolve())
    assert verify_receipts(env.log, run.run_id) == []


# ---------------------------------------------------------------- 整轮 + replay


def test_run_command_survives_replay_without_rerunning(tmp_path):
    ws = tmp_path / WS
    ws.mkdir()
    marker = ws / "count.txt"
    code = f"open({str(marker)!r}, 'a').write('x')"

    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path / "authority")
    task = stack.tasks.create_task(
        "执行一次命令", [{"root_path": str(ws)}], exec_policy=_policy(ws)
    )
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("c1", "run_command", {"argv": [PY, "-c", code]})],
            ),
            ModelResponse("end_turn", content="命令执行完了。"),
        ]
    )
    engine = engine_for(stack, model, limits=Limits(6, 20_000))

    result = engine.start(task.task_id, out_name="out.md")
    assert result.state == "waiting_approval"
    assert approve_pending(stack, result.run_id)

    result = engine.resume_run(result.run_id)
    assert result.outcome in ("succeeded", "noop")
    assert marker.read_text(encoding="utf-8") == "x"

    report = replay(log, result.run_id)
    assert report.ok, report.problems
    assert marker.read_text(encoding="utf-8") == "x", "replay 重跑了命令"
    log.close()


def test_exec_policy_is_persisted_on_the_task(tmp_path):
    """任务级策略必须能落库读回：审批绑的是它，重开进程后也得在。"""
    ws = tmp_path / WS
    ws.mkdir()
    log = EventLog(tmp_path / "events.db")
    tasks = TaskStore(
        log, leases=LeaseStore(log), claims=JobStore(log), data_root=tmp_path
    )
    policy = _policy(ws)
    task = tasks.create_task("t", [{"root_path": str(ws)}], exec_policy=policy)
    log.close()

    log2 = EventLog(tmp_path / "events.db")
    tasks2 = TaskStore(
        log2, leases=LeaseStore(log2), claims=JobStore(log2), data_root=tmp_path
    )
    reloaded = tasks2.get_task(task.task_id)
    assert reloaded is not None
    assert reloaded.exec_policy is not None
    assert reloaded.exec_policy.to_dict() == policy.to_dict()
    log2.close()

def test_model_proposal_without_exec_policy_is_rejected(tmp_path):
    """模型主动提案 run_command，但任务没开 exec：得到的是策略拒绝，不是崩溃。"""
    ws = tmp_path / WS
    ws.mkdir()
    log = EventLog(tmp_path / "events.db")
    stack = stack_for(log, data_root=tmp_path / "authority")
    task = stack.tasks.create_task("别执行命令", [{"root_path": str(ws)}])
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        "c1", "run_command", {"argv": [PY, "-c", "print(1)"]}
                    )
                ],
            ),
            ModelResponse("end_turn", content="命令没被允许执行。"),
        ]
    )
    engine = engine_for(stack, model, limits=Limits(6, 20_000))

    result = engine.start(task.task_id, out_name="out.md")

    assert result.outcome in ("succeeded", "noop")
    results = [
        e.payload for e in log.read_run(result.run_id) if e.type == "tool.result"
    ]
    assert results[-1]["status"] == "rejected"
    assert results[-1]["output"]["rule_id"] == "c4.exec_not_configured"
    log.close()
