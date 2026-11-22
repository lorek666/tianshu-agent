"""Goal V2 的持久推进、累计边界及真实 Worker 阶段闭环。"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import stack_for

from tianshu.canonical import jcs_sha256
from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.plans import PlanStore
from tianshu.run_policy import EFFECTS, PURE_ONLY
from tianshu.worker import Worker


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    task = stack.tasks.create_task("检查报告", [str(ws)])
    plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=3))
    yield plane, stack, task, ws
    log.close()


def create(env, **changes):
    return env[0].handle("goal.v2.create", {
        "task_id": env[2].task_id, "goal_revision": env[2].goal_revision,
        "criteria": [{"criterion_id": "report", "description": "报告存在", "mode": "machine"}],
        "execution_policy": PURE_ONLY.to_dict(), "request_id": "create",
        "deadline_ms": int(time.time() * 1000) + 600_000,
        "budget_limits": {"requests": 8}, "max_rounds": 3,
        **changes,
    })["goal"]


def get(env, goal):
    return env[0].handle("goal.v2.get", {"goal_id": goal["goal_id"]})["goal"]


def advance(env, goal, request_id="advance"):
    return env[0].handle("goal.v2.advance", {
        "goal_id": goal["goal_id"], "request_id": request_id,
        "expected_state_revision": goal["state_revision"],
        "orchestration_epoch": goal["orchestration_epoch"],
    })["goal"]


def candidate(mission, *, human=False, steps=1):
    criteria = mission["criteria"]
    check = {"format_version": 1, "check_id": "exists", "criterion_ids": ["report"],
             "required": True, "kind": "file_exists",
             "inputs": {"workspace_binding_id": "0", "relative_path": "report.md",
                        "file_kind": "regular"}}
    if human:
        check.update(kind="human", inputs={"question": "报告是否符合要求？",
                                         "required_artifact_ids": [], "required_check_ids": []})
    assert len(criteria) == 1
    manifest = {"format_version": 1, "mission_id": mission["mission_id"],
                "mission_digest": jcs_sha256(mission), "checks": [check]}
    plan = {
        "format_version": 1, "plan_id": "plan_v2", "revision": 1,
        "mission_id": mission["mission_id"], "mission_digest": jcs_sha256(mission),
        "previous_digest": None,
        "verification_manifest": {"content_ref": "content://sha256:" + jcs_sha256(manifest),
                                  "digest": jcs_sha256(manifest)},
        "steps": [{"step_id": f"step_{i}", "title": f"检查 {i}", "instruction": "读取报告",
                   "depends_on": [f"step_{i - 1}"] if i else [],
                   "criterion_ids": ["report"], "expected_artifacts": [],
                   "verification_ids": ["exists"]} for i in range(steps)],
    }
    return {"plan": plan, "verification_manifest": manifest}


def drive(stack, *texts):
    model = ScriptedModel([ModelResponse(
        "end_turn", content=t, usage={"input_tokens": 10, "output_tokens": 10},
    ) for t in texts])
    with Worker(stack, model=model, heartbeat_interval_ms=0) as worker:
        worker.drain(max_jobs=10)
        assert not worker.errors
    return model


def test_v2_create_is_idempotent_and_does_not_start_runs(env):
    goal = create(env)
    assert goal["format_version"] == 2
    assert goal["status"] == "ready"
    assert goal["phase"] == "plan"
    assert goal["rounds_started"] == 0
    assert env[1].tasks.list_runs(env[2].task_id) == []
    assert create(env, deadline_ms=goal["config"]["deadline_ms"]) == goal
    account = env[1].budget.account("goal:" + goal["goal_id"])
    assert account["parent_id"] is None
    assert env[1].budget.usage(account["account_id"])["limits"] == {"requests": 8}


def test_v2_worker_plan_execute_verify_and_human_finish_are_distinct(env):
    from tianshu.goal_v2 import GoalCoordinator
    goal = create(env)
    first = advance(env, goal)
    assert advance(env, goal)["phase_run_id"] == first["phase_run_id"]
    mission = GoalCoordinator(env[1]).mission(goal["goal_id"])
    drive(env[1], json.dumps(candidate(mission)))
    planned = get(env, goal)
    assert planned["status"] == "awaiting_review"
    assert planned["rounds_started"] == 1
    assert len(env[1].tasks.list_runs(env[2].task_id)) == 1
    plan = PlanStore(env[1]).get(planned["plan_id"])
    PlanStore(env[1]).confirm(plan["plan_id"], expected_revision=1,
                            document_digest=plan["document_digest"], request_id="confirm")
    (env[3] / "report.md").write_bytes(b"report")
    advance(env, planned, "execute")
    drive(env[1], "报告存在")
    execution_finished = get(env, goal)
    advance(env, execution_finished, "verify")
    drive(env[1])
    result = get(env, goal)
    assert result["status"] == "awaiting_review"
    assert result["phase"] == "finish"
    assert result["rounds_started"] == 1
    runs = env[1].tasks.list_runs(env[2].task_id)
    assert len(runs) == 3
    assert all(env[1].budget.account("run:" + r.run_id)["parent_id"] ==
               "goal:" + goal["goal_id"] for r in runs)
    assert env[1].budget.usage("goal:" + goal["goal_id"])["settled"]["requests"] == 2
    assert not env[1].log.read_run(runs[-1].run_id, event_types=("model.intent",))
    closed = env[0].handle("goal.v2.close", {
        "goal_id": goal["goal_id"], "request_id": "close", "outcome": "completed",
        "expected_state_revision": result["state_revision"],
    })["goal"]
    assert closed["status"] == "completed"


def test_advance_binds_request_parameters_and_rejects_stale_cas(env):
    goal = create(env)
    params = {
        "goal_id": goal["goal_id"], "request_id": "cas_advance",
        "expected_state_revision": goal["state_revision"],
        "orchestration_epoch": goal["orchestration_epoch"],
    }
    first = env[0].handle("goal.v2.advance", params)["goal"]
    assert env[0].handle("goal.v2.advance", params)["goal"] == first
    for changes in (
        {"expected_state_revision": first["state_revision"]},
        {"orchestration_epoch": first["orchestration_epoch"] + 1},
        {"request_id": "new_advance"},
    ):
        before = env[1].log.seq_head()
        with pytest.raises(ControlError):
            env[0].handle("goal.v2.advance", {**params, **changes})
        assert env[1].log.seq_head() == before


@pytest.mark.parametrize("action", ["advance", "pause", "resume", "recover", "close"])
@pytest.mark.parametrize("value", ["missing", None, True, False, -1, 0, 1.5, "1", 2**63])
def test_mutating_rpc_requires_positive_integer_state_cas(env, action, value):
    goal = create(env)
    params = {
        "goal_id": goal["goal_id"], "request_id": "invalid_cas",
        "expected_state_revision": goal["state_revision"],
    }
    if action == "advance":
        params["orchestration_epoch"] = goal["orchestration_epoch"]
    elif action == "recover":
        params.update(expected_block_event_seq=goal["state_revision"],
                      resolution="retry", evidence_refs=[])
    elif action == "close":
        params["outcome"] = "cancelled"
    if value == "missing":
        del params["expected_state_revision"]
    else:
        params["expected_state_revision"] = value
    before = env[1].log.seq_head()
    with pytest.raises(ControlError) as error:
        env[0].handle("goal.v2." + action, params)
    assert error.value.code == "bad_params"
    assert env[1].log.seq_head() == before
    assert not env[1].tasks.list_runs(env[2].task_id)


@pytest.mark.parametrize("value", ["missing", None, True, False, -1, 0, 1.5, "1", 2**63])
@pytest.mark.parametrize("action,field", [
    ("advance", "orchestration_epoch"), ("recover", "expected_block_event_seq"),
])
def test_rpc_rejects_invalid_epoch_and_block_cas(env, action, field, value):
    goal = create(env)
    params = {
        "goal_id": goal["goal_id"], "request_id": "invalid_epoch",
        "expected_state_revision": goal["state_revision"],
        field: value,
    }
    if action == "recover":
        params.update(resolution="retry", evidence_refs=[])
    if value == "missing":
        del params[field]
    before = env[1].log.seq_head()
    with pytest.raises(ControlError) as error:
        env[0].handle("goal.v2." + action, params)
    assert error.value.code == "bad_params"
    assert env[1].log.seq_head() == before


def test_get_requires_exactly_one_goal_or_task_selector(env):
    goal = create(env)
    for params in ({}, {"goal_id": goal["goal_id"], "task_id": env[2].task_id}):
        before = env[1].log.seq_head()
        with pytest.raises(ControlError) as error:
            env[0].handle("goal.v2.get", params)
        assert error.value.code == "bad_params"
        assert env[1].log.seq_head() == before
    assert env[0].handle("goal.v2.get", {"task_id": env[2].task_id})["goal"] == goal


def test_stale_control_never_refreshes_cas_and_exact_retry_stays_idempotent(env):
    goal = create(env)
    pause_params = {
        "goal_id": goal["goal_id"], "request_id": "pause",
        "expected_state_revision": goal["state_revision"],
    }
    paused = env[0].handle("goal.v2.pause", pause_params)["goal"]
    assert env[0].handle("goal.v2.pause", pause_params)["goal"] == paused
    for action in ("advance", "pause", "resume", "recover", "close"):
        params = {
            "goal_id": goal["goal_id"], "request_id": "stale_" + action,
            "expected_state_revision": goal["state_revision"],
        }
        if action == "advance":
            params["orchestration_epoch"] = goal["orchestration_epoch"]
        elif action == "recover":
            params.update(expected_block_event_seq=paused["state_revision"],
                          resolution="retry", evidence_refs=[])
        elif action == "close":
            params["outcome"] = "cancelled"
        before = env[1].log.seq_head()
        with pytest.raises(ControlError) as error:
            env[0].handle("goal.v2." + action, params)
        assert error.value.code == "conflict"
        assert env[1].log.seq_head() == before
    resume_params = {
        "goal_id": goal["goal_id"], "request_id": "resume",
        "expected_state_revision": paused["state_revision"],
    }
    resumed = env[0].handle("goal.v2.resume", resume_params)["goal"]
    assert env[0].handle("goal.v2.resume", resume_params)["goal"] == resumed
    assert resumed["status"] == "ready"


def test_invalid_auto_configuration_never_creates_goal_or_run(env):
    for changes in (
        {"mode": "unlimited"}, {"max_rounds": True}, {"max_rounds": 0},
        {"mode": "bounded_auto", "budget_limits": {}},
        {"deadline_ms": 0}, {"max_no_progress_rounds": 0},
        {"completion_policy": "trust_model"}, {"replan_policy": "anything"},
    ):
        before = env[1].log.seq_head()
        with pytest.raises(ControlError):
            create(env, **changes)
        assert env[1].log.seq_head() == before


def planned(env, *, step_count=1, **changes):
    from tianshu.goal_v2 import GoalCoordinator
    goal = create(env, **changes)
    advance(env, goal)
    mission = GoalCoordinator(env[1]).mission(goal["goal_id"])
    drive(env[1], json.dumps(candidate(mission, steps=step_count)))
    view = get(env, goal)
    plan = PlanStore(env[1]).get(view["plan_id"])
    PlanStore(env[1]).confirm(plan["plan_id"], expected_revision=1,
                            document_digest=plan["document_digest"], request_id="confirm")
    return view


def test_auto_verifies_and_completes_at_exact_request_limit(env):
    goal = planned(env, mode="bounded_auto", completion_policy="verified",
                   budget_limits={"requests": 2})
    (env[3] / "report.md").write_bytes(b"report")
    drive(env[1], "执行完成")
    result = get(env, goal)
    assert result["status"] == "completed"
    assert len(env[1].tasks.list_runs(env[2].task_id)) == 3
    assert env[1].budget.usage("goal:" + goal["goal_id"])["settled"]["requests"] == 2


def test_each_model_admission_rechecks_goal_monotonic_deadline(env, monkeypatch):
    from tianshu.goal_v2 import GoalCoordinator, ensure_phase_budget
    now = int(time.time() * 1000)
    coordinator = GoalCoordinator(env[1], clock=lambda: now, monotonic=lambda: 1000)
    goal = coordinator.create({
        "task_id": env[2].task_id, "goal_revision": env[2].goal_revision,
        "criteria": [{"criterion_id": "report", "description": "报告存在", "mode": "machine"}],
        "execution_policy": PURE_ONLY.to_dict(), "request_id": "clock",
        "deadline_ms": now + 50_000,
    })
    started = coordinator.tick(
        goal["goal_id"], requested=True, request_id="clock_start",
        expected_state_revision=goal["state_revision"],
        orchestration_epoch=goal["orchestration_epoch"])
    monkeypatch.setattr("tianshu.goal_v2.time.monotonic", lambda: 52.0)
    from tianshu.goal_v2 import GoalAdmissionRefused
    with pytest.raises(GoalAdmissionRefused, match="deadline_exceeded"):
        ensure_phase_budget(env[1].log, env[1].budget, started["phase_run_id"], {}, "estimate")
    assert not env[1].log.read_run(started["phase_run_id"], event_types=("model.intent",))


def test_clarification_resumes_same_goal_round_and_parent_budget(env):
    from tianshu.goal_v2 import GoalCoordinator
    goal = create(env)
    first = advance(env, goal)
    drive(env[1], "TIANSHU_QUESTION: 应生成哪种报告？")
    waiting = get(env, goal)
    assert waiting["status"] == "awaiting_input"
    assert waiting["phase"] == "ask"
    answer = env[0].handle("run.answer_clarification", {
        "task_id": env[2].task_id, "source_run_id": first["phase_run_id"],
        "message": "生成审计报告", "request_id": "answer",
    })
    assert answer["run"]["run_id"] != first["phase_run_id"]
    assert get(env, goal)["round"] == 1
    mission = GoalCoordinator(env[1]).mission(goal["goal_id"])
    drive(env[1], json.dumps(candidate(mission)))
    result = get(env, goal)
    assert result["status"] == "awaiting_review"
    assert result["round"] == 1
    repeated = env[0].handle("run.answer_clarification", {
        "task_id": env[2].task_id, "source_run_id": first["phase_run_id"],
        "message": "生成审计报告", "request_id": "answer",
    })
    assert repeated["run"]["run_id"] == answer["run"]["run_id"]
    with pytest.raises(ControlError):
        env[0].handle("run.answer_clarification", {
            "task_id": env[2].task_id, "source_run_id": first["phase_run_id"],
            "message": "换一个答案", "request_id": "answer",
        })
    for run in env[1].tasks.list_runs(env[2].task_id):
        assert env[1].budget.account("run:" + run.run_id)["parent_id"] == "goal:" + goal["goal_id"]


def test_unknown_usage_cannot_be_completed_by_auto_verification(env):
    goal = planned(env, mode="bounded_auto", completion_policy="verified",
                   budget_limits={"requests": 8, "tokens": 100_000})
    (env[3] / "report.md").write_bytes(b"report")
    model = ScriptedModel([ModelResponse("end_turn", content="完成")])
    with Worker(env[1], model=model, heartbeat_interval_ms=0) as worker:
        worker.drain(max_jobs=10)
        assert not worker.errors
    result = get(env, goal)
    assert result["status"] == "blocked"
    assert result["blocker"]["category"] == "usage_unknown"
    assert env[1].budget.usage("goal:" + goal["goal_id"])["reserved"]["tokens"] > 0


def test_expired_clarification_persists_block_without_creating_run(env, monkeypatch):
    goal = create(env)
    first = advance(env, goal)
    drive(env[1], "TIANSHU_QUESTION: 应生成哪种报告？")
    before = len(env[1].tasks.list_runs(env[2].task_id))
    monkeypatch.setattr(
        "tianshu.goal_v2.time.time", lambda: (goal["config"]["deadline_ms"] + 1) / 1000)
    with pytest.raises(ControlError, match="Goal 边界不允许新的澄清调用"):
        env[0].handle("run.answer_clarification", {
            "task_id": env[2].task_id, "source_run_id": first["phase_run_id"],
            "message": "生成审计报告", "request_id": "expired_answer",
        })
    result = get(env, goal)
    assert result["status"] == "blocked"
    assert result["blocker"]["category"] == "deadline_exceeded"
    assert len(env[1].tasks.list_runs(env[2].task_id)) == before


def test_budget_refusal_is_structured_and_never_calls_model(env):
    goal = create(env, budget_limits={"tokens": 1, "requests": 8})
    first = advance(env, goal)
    model = drive(env[1])
    assert not model.calls
    view = get(env, goal)
    assert view["status"] == "blocked"
    assert view["blocker"]["category"] == "budget_exhausted"
    facts = env[1].log.read_run(first["phase_run_id"],
                              event_types=("goal.v2_admission_refused",))
    assert facts[-1].payload["category"] == "budget_exhausted"


@pytest.mark.parametrize("failure,event_type,rule_id,category", [
    ("unconfigured", "policy.denied", "c4.exec_not_configured", "policy_denied"),
    ("policy", "dispatch.rejected", "c4.exec_policy_denied", "policy_denied"),
    ("isolation", "dispatch.rejected", "c4.exec_unsupported", "isolation_unavailable"),
])
def test_goal_classifies_real_dispatch_denial_facts(env, failure, event_type, rule_id, category):
    from tianshu.execpolicy import ExecPolicy
    from tianshu.model import ToolCall
    if failure != "unconfigured":
        policy = ExecPolicy() if failure == "policy" else ExecPolicy(
            argv_allowlist=(str(Path(sys.executable).resolve()),), cwd_roots=(env[3],),
            timeout_ms=2000, max_output_bytes=4096, isolation_requires=("network",))
        task = env[1].tasks.create_task("核对报告", [str(env[3])], exec_policy=policy)
        env = env[0], env[1], task, env[3]
    goal = planned(env, execution_policy=EFFECTS.to_dict())
    (env[3] / "report.md").write_bytes(b"report")
    started = advance(env, goal, "execute")
    model = ScriptedModel([
        ModelResponse("tool_use", tool_calls=[ToolCall("command", "run_command", {
            "argv": [str(Path(sys.executable).resolve()), "-c", "print('never executed')"],
            "cwd": str(env[3]),
        })], usage={"input_tokens": 1, "output_tokens": 1}),
        ModelResponse("end_turn", content="报告已完成",
                      usage={"input_tokens": 1, "output_tokens": 1}),
    ])
    with Worker(env[1], model=model, heartbeat_interval_ms=0) as worker:
        worker.drain(max_jobs=10)
        assert not worker.errors
    facts = env[1].log.read_run(started["phase_run_id"], event_types=(event_type,))
    assert facts[-1].payload["rule_id"] == rule_id
    assert not env[1].log.read_run(started["phase_run_id"], event_types=("tool.intent",))
    assert not env[1].approvals.pending(run_id=started["phase_run_id"])
    result = get(env, goal)
    assert result["status"] == "blocked"
    assert result["blocker"]["category"] == category
    assert result["blocker"]["source_event_seq"] == facts[-1].seq


def test_failed_verification_block_is_bound_to_real_result_event(env):
    goal = planned(env)
    advance(env, goal, "execute")
    drive(env[1], "口头完成")
    advance(env, get(env, goal), "verify")
    drive(env[1])
    result = get(env, goal)
    fact = env[1].log.read_run(
        result["phase_run_id"], event_types=("verification.result",))[-1]
    assert fact.payload["result"] == "failed"
    assert result["blocker"]["category"] == "verification_failed"
    assert result["blocker"]["source_event_seq"] == fact.seq


def test_model_failure_words_do_not_replace_structured_denial_facts(env):
    goal = planned(env)
    (env[3] / "report.md").write_bytes(b"report")
    advance(env, goal, "execute")
    drive(env[1], "policy_denied isolation_unavailable c4.exec_unsupported")
    advance(env, get(env, goal), "verify")
    drive(env[1])
    result = get(env, goal)
    assert result["phase"] == "finish"
    assert result["blocker"] is None


def test_worker_honors_frozen_phase_local_turn_limit(env):
    from tianshu.model import ToolCall
    goal = planned(env)
    (env[3] / "report.md").write_bytes(b"report")
    started = advance(env, goal, "execute")
    response = ModelResponse("tool_use", tool_calls=[
        ToolCall("read", "read_text", {"path": "report.md"}),
    ], usage={"input_tokens": 1, "output_tokens": 1})
    model = ScriptedModel([response] * 7)
    with Worker(env[1], model=model, heartbeat_interval_ms=0) as worker:
        worker.drain(max_jobs=10)
        assert not worker.errors
    assert model.calls == 3
    assert env[1].tasks.get_run(started["phase_run_id"]).state == "failed"


def recover(env, goal, *, resolution="retry", **changes):
    return env[0].handle("goal.v2.recover", {
        "goal_id": goal["goal_id"], "request_id": "recover_" + str(goal["state_revision"]),
        "expected_state_revision": goal["state_revision"],
        "expected_block_event_seq": goal["state_revision"],
        "resolution": resolution, "evidence_refs": [], **changes,
    })["goal"]


def test_no_progress_is_persistent_and_human_recovery_does_not_reset_round_limit(env):
    goal = planned(env, mode="bounded_auto", max_rounds=2)
    drive(env[1], "仅口头声称完成")
    first = get(env, goal)
    assert first["blocker"]["category"] == "verification_failed"
    assert first["no_progress_streak"] == 1
    recover(env, first)
    drive(env[1], "再次声称完成")
    second = get(env, goal)
    assert second["blocker"]["category"] == "no_progress"
    assert second["round"] == 2
    assert second["no_progress_streak"] == 2
    assert [a["no_progress_streak"] for a in second["assessments"]] == [1, 2]
    assert second["assessments"][-1]["blocker"]["category"] == "no_progress"
    with pytest.raises(ControlError):
        recover(env, second)
    recover(env, second, resolution="new_observation")
    drive(env[1])
    limit = get(env, goal)
    assert limit["blocker"]["category"] == "round_limit"
    assert limit["round"] == 2
    with pytest.raises(ControlError):
        recover(env, limit, resolution="new_observation")
    assert len(env[1].tasks.list_runs(env[2].task_id)) == 5


def test_auto_two_steps_are_serial_and_share_one_mission(env):
    goal = planned(env, mode="bounded_auto", completion_policy="verified", step_count=2)
    (env[3] / "report.md").write_bytes(b"report")
    drive(env[1], "第一步完成", "第二步完成")
    result = get(env, goal)
    assert result["status"] == "completed"
    assert result["round"] == 2
    assert [a["completed_step_ids"] for a in result["assessments"]] == [
        ["step_0"], ["step_0", "step_1"]]
    runs = env[1].tasks.list_runs(env[2].task_id)
    assert len(runs) == 5
    missions = [env[1].log.read_run(r.run_id, event_types=("run.mission",))[0].payload["mission"]
                for r in runs]
    assert all(m == missions[0] for m in missions)


def test_auto_two_real_file_writes_are_verified_and_complete(env):
    import copy

    from tianshu.goal_v2 import GoalCoordinator
    from tianshu.model import ToolCall

    goal = create(env, mode="bounded_auto", completion_policy="verified",
                  execution_policy=EFFECTS.to_dict())
    advance(env, goal)
    document = candidate(GoalCoordinator(env[1]).mission(goal["goal_id"]), steps=2)
    manifest, plan = document["verification_manifest"], document["plan"]
    manifest["checks"][0]["inputs"]["relative_path"] = "a.txt"
    other = copy.deepcopy(manifest["checks"][0])
    other["check_id"] = "exists_b"
    other["inputs"]["relative_path"] = "b.txt"
    manifest["checks"].append(other)
    plan["steps"][1]["verification_ids"] = ["exists_b"]
    plan["verification_manifest"] = {
        "content_ref": "content://sha256:" + jcs_sha256(manifest),
        "digest": jcs_sha256(manifest),
    }
    drive(env[1], json.dumps(document))
    goal = get(env, goal)
    stored = PlanStore(env[1]).get(goal["plan_id"])
    PlanStore(env[1]).confirm(stored["plan_id"], expected_revision=1,
                            document_digest=stored["document_digest"], request_id="confirm")
    model = ScriptedModel([
        response
        for name in ("a", "b")
        for response in (
            ModelResponse("tool_use", tool_calls=[
                ToolCall("create_" + name, "create_report",
                         {"name": name + ".txt", "content": name}),
            ], usage={"input_tokens": 10, "output_tokens": 10}),
            ModelResponse("end_turn", content="已写入 " + name,
                          usage={"input_tokens": 10, "output_tokens": 10}),
        )
    ])
    with Worker(env[1], model=model, heartbeat_interval_ms=0) as worker:
        worker.drain(max_jobs=10)
        assert not worker.errors
    completed = get(env, goal)
    assert completed["status"] == "completed", completed["blocker"]
    assert completed["round"] == 2
    assert all((env[3] / (name + ".txt")).read_bytes() == name.encode() for name in ("a", "b"))
    assert all(s["state"] == "satisfied"
               for s in PlanStore(env[1]).get(goal["plan_id"])["steps"])
    for run in env[1].tasks.list_runs(env[2].task_id):
        intents = env[1].log.read_run(run.run_id, event_types=("tool.intent",))
        results = env[1].log.read_run(run.run_id, event_types=("tool.result",))
        assert {e.payload["call_id"] for e in intents} <= {
            e.payload["call_id"] for e in results}


def test_pause_resume_reuses_queued_run_and_cancel_preserves_budget(env):
    goal = create(env)
    first = advance(env, goal)
    paused = env[0].handle("goal.v2.pause", {
        "goal_id": goal["goal_id"], "request_id": "pause",
        "expected_state_revision": first["state_revision"],
    })["goal"]
    assert paused["status"] == "paused"
    assert env[1].tasks.get_run(first["phase_run_id"]).state == "paused"
    assert drive(env[1]).calls == 0
    resumed = env[0].handle("goal.v2.resume", {
        "goal_id": goal["goal_id"], "request_id": "resume",
        "expected_state_revision": paused["state_revision"],
    })["goal"]
    assert resumed["phase_run_id"] == first["phase_run_id"]
    assert resumed["round"] == 1
    cancelled = env[0].handle("goal.v2.close", {
        "goal_id": goal["goal_id"], "request_id": "cancel", "outcome": "cancelled",
        "expected_state_revision": resumed["state_revision"],
    })["goal"]
    assert cancelled["status"] == "cancelled"
    assert env[1].tasks.get_run(first["phase_run_id"]).state == "cancelled"
    assert env[1].budget.usage("goal:" + goal["goal_id"])["cancelled"]
    assert drive(env[1]).calls == 0


def test_rejected_action_cannot_be_retried_until_exact_human_recovery(env):
    from tianshu.model import ToolCall
    from tianshu.permissions import HumanIdentity
    goal = planned(env, execution_policy=EFFECTS.to_dict())
    (env[3] / "report.md").write_bytes(b"old")
    started = advance(env, goal, "execute")
    model = ScriptedModel([ModelResponse("tool_use", tool_calls=[
        ToolCall("overwrite", "modify_text", {"path": "report.md", "content": "new"}),
    ], usage={"input_tokens": 1, "output_tokens": 1})])
    with Worker(env[1], model=model, heartbeat_interval_ms=0) as worker:
        worker.run_once()
    pending = env[1].approvals.pending(run_id=started["phase_run_id"])
    assert len(pending) == 1
    env[1].approvals.decide(pending[0].approval_id, decision="rejected",
                            actor=HumanIdentity.local_cli())
    # 原执行侧拒绝事实不依赖协调器先消费。
    from tianshu.goal_v2 import action_denied
    stored = env[1].dispatcher.get_request(pending[0].request_id)
    assert action_denied(env[1].log, stored.request)
    drive(env[1])
    blocked = get(env, goal)
    assert blocked["blocker"]["category"] == "approval_denied"
    assert (env[3] / "report.md").read_bytes() == b"old"
    with pytest.raises(ControlError):
        recover(env, blocked, resolution="change_decision", denied_content_digest="0" * 64)
    recovered = recover(
        env, blocked, resolution="change_decision",
        denied_content_digest=blocked["blocker"]["denied_content_digest"])
    assert recovered["status"] == "ready"
    assert env[1].tasks.get_run(started["phase_run_id"]).state == "cancelled"
    assert not action_denied(env[1].log, stored.request)
    assert env[1].approvals.get(pending[0].approval_id).state == "rejected"


def _advance_process(root, goal_id, barrier, output):
    root = Path(root)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        plane = ControlPlane(stack, model=ScriptedModel([]))
        goal = plane.handle("goal.v2.get", {"goal_id": goal_id})["goal"]
        barrier.wait(timeout=30)
        result = plane.handle("goal.v2.advance", {
            "goal_id": goal_id, "request_id": "race",
            "expected_state_revision": goal["state_revision"],
            "orchestration_epoch": goal["orchestration_epoch"],
        })["goal"]
        output.put(result["phase_run_id"])
    finally:
        log.close()


def test_two_real_processes_commit_one_phase_and_one_round(env):
    goal = create(env)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    children = [ctx.Process(target=_advance_process, args=(
        str(env[1].tasks.data_root), goal["goal_id"], barrier, output,
    )) for _ in range(2)]
    try:
        for child in children:
            child.start()
        barrier.wait(timeout=30)
        results = [output.get(timeout=30) for _ in children]
        for child in children:
            child.join(timeout=30)
            assert child.exitcode == 0
        assert results[0] == results[1]
        assert len(env[1].tasks.list_runs(env[2].task_id)) == 1
        assert get(env, goal)["round"] == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
        output.close()


@pytest.mark.parametrize("point,run_count", [
    ("goal-v2-before-phase-commit", 0), ("goal-v2-after-tick", 1),
])
def test_hard_crash_at_phase_transaction_boundary_is_idempotent(env, point, run_count):
    goal = create(env)
    script = """
import sys
from pathlib import Path
from tianshu.control import ControlPlane
from tianshu.events import EventLog
from support import stack_for
root = Path(sys.argv[1])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
ControlPlane(stack_for(log, data_root=root), model=None).handle("goal.v2.advance", {
    "goal_id": sys.argv[2], "request_id": "crash",
    "expected_state_revision": int(sys.argv[3]), "orchestration_epoch": int(sys.argv[4])})
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(env[1].tasks.data_root), goal["goal_id"],
         str(goal["state_revision"]), str(goal["orchestration_epoch"])],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={**os.environ, "TIANSHU_FAILPOINT": point,
             "PYTHONPATH": str(Path(__file__).parent) + os.pathsep
             + os.environ.get("PYTHONPATH", "")})
    assert child.returncode == 77, child.stderr
    assert len(env[1].tasks.list_runs(env[2].task_id)) == run_count
    resumed = advance(env, goal, "crash")
    assert resumed["round"] == 1
    assert len(env[1].tasks.list_runs(env[2].task_id)) == 1
    assert env[1].claims.job_for_run(resumed["phase_run_id"]) is not None


@pytest.mark.parametrize("point", ["goal-after-call", "goal-after-result"])
def test_hard_crash_after_model_call_never_resends_planning(env, point):
    from support import engine_for
    from test_workflow import _crash_workflow

    from tianshu.goal_v2 import GoalCoordinator
    goal = create(env)
    started = advance(env, goal)
    mission = GoalCoordinator(env[1]).mission(goal["goal_id"])
    _crash_workflow(env[1], started["phase_run_id"], json.dumps(candidate(mission)), point)
    model = ScriptedModel([])
    result = engine_for(env[1], model).resume_run(started["phase_run_id"])
    assert model.calls == 0
    assert result.state == ("failed" if point == "goal-after-call" else "succeeded")
    drive(env[1])
    view = get(env, goal)
    assert view["status"] == ("blocked" if point == "goal-after-call" else "awaiting_review")
    assert len(env[1].log.read_run(started["phase_run_id"], event_types=("model.intent",))) == 1
