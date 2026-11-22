"""独立验证 Run 真实经过 Worker，不能调用 provider 或绕过审批。"""

from __future__ import annotations

import pytest
from support import stack_for

from tianshu.canonical import jcs_sha256
from tianshu.events import EventLog
from tianshu.mission import prepare_mission
from tianshu.model import ScriptedModel
from tianshu.plans import PlanStore
from tianshu.worker import Worker


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "work"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    task = stack.tasks.create_task("检查产物", [str(ws)])
    mission = prepare_mission(stack.tasks, task.task_id, criteria=[
        {"criterion_id": "report", "description": "文件存在", "mode": "machine"},
    ]).to_dict()
    check = {"format_version": 1, "check_id": "exists", "criterion_ids": ["report"],
             "required": True, "kind": "file_exists",
             "inputs": {"workspace_binding_id": "0", "relative_path": "report.md",
                        "file_kind": "regular"}}
    manifest = {"format_version": 1, "mission_id": mission["mission_id"],
                "mission_digest": jcs_sha256(mission), "checks": [check]}
    document = {
        "format_version": 1, "plan_id": "plan_driver", "revision": 1,
        "mission_id": mission["mission_id"], "mission_digest": jcs_sha256(mission),
        "previous_digest": None,
        "verification_manifest": {"content_ref": "content://sha256:" + jcs_sha256(manifest),
                                  "digest": jcs_sha256(manifest)},
        "steps": [{"step_id": "write", "title": "写入", "instruction": "创建文件",
                   "depends_on": [], "criterion_ids": ["report"], "expected_artifacts": [],
                   "verification_ids": ["exists"]}],
    }
    plans = PlanStore(stack)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm")
    plans.change_step(view["plan_id"], "write", "running", request_id="running")
    view = plans.change_step(view["plan_id"], "write", "verifying", request_id="verifying")
    yield stack, ws, view
    log.close()


def test_worker_runs_readonly_verification_without_provider_and_reuses_result(env):
    from tianshu.verification_driver import start_verification
    stack, ws, view = env
    (ws / "report.md").write_bytes(b"report")
    run = start_verification(stack, view, "write", request_id="verify")
    assert start_verification(stack, view, "write", request_id="verify").run_id == run.run_id
    with Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=0) as worker:
        assert worker.run_once()
        assert not worker.errors
    assert stack.tasks.get_run(run.run_id).state == "succeeded"
    assert PlanStore(stack).get(view["plan_id"])["steps"][0]["state"] == "satisfied"
    assert not stack.log.read_run(run.run_id, event_types=("model.intent",))
    assert len(stack.log.read_run(run.run_id, event_types=("verification.result",))) == 1


def test_worker_failed_check_cannot_satisfy_step(env):
    from tianshu.verification_driver import start_verification
    stack, _, view = env
    run = start_verification(stack, view, "write", request_id="verify")
    with Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=0) as worker:
        worker.run_once()
        assert not worker.errors
    result = stack.log.read_run(run.run_id, event_types=("verification.result",))[0]
    assert result.payload["result"] == "failed"
    assert PlanStore(stack).get(view["plan_id"])["steps"][0]["state"] == "failed"


@pytest.mark.parametrize("mutate_before_spawn", [False, True])
def test_verification_command_waits_for_real_approval_and_checks_exit_code(
    env, monkeypatch, mutate_before_spawn,
):
    import copy
    import sys
    from pathlib import Path

    from support import approve_pending, engine_for

    from tianshu.execpolicy import ExecPolicy
    from tianshu.verification_driver import command_binding, start_verification

    stack, ws, _ = env
    executable = str(Path(sys.executable).resolve())
    task = stack.tasks.create_task("运行已确认检查", [str(ws)], exec_policy=ExecPolicy(
        argv_allowlist=(executable,), cwd_roots=(ws,), timeout_ms=2000,
        max_output_bytes=4096,
    ))
    mission = prepare_mission(stack.tasks, task.task_id, criteria=[
        {"criterion_id": "report", "description": "命令退出值正确", "mode": "machine"},
    ]).to_dict()
    _, _, base = env
    check = copy.deepcopy(base["manifest"]["checks"][0])
    check.update(kind="command_exit", inputs={
        **command_binding(stack, task.task_id, executable),
        "argv": [executable, "-c", "print('check complete')"],
        "cwd_binding_id": "0", "expected_exit_codes": [0], "timeout_ms": 2000,
    })
    manifest = {**base["manifest"], "mission_id": mission["mission_id"],
                "mission_digest": jcs_sha256(mission), "checks": [check]}
    document = copy.deepcopy(base["document"])
    document.update(plan_id="plan_command", mission_id=mission["mission_id"],
                    mission_digest=jcs_sha256(mission), verification_manifest={
                        "digest": jcs_sha256(manifest),
                        "content_ref": "content://sha256:" + jcs_sha256(manifest),
                    })
    plans = PlanStore(stack)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create_command")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm_command")
    plans.change_step(view["plan_id"], "write", "running", request_id="run_command")
    view = plans.change_step(view["plan_id"], "write", "verifying", request_id="verify_command")
    run = start_verification(stack, view, "write", request_id="verify")
    model = ScriptedModel([])
    with Worker(stack, model=model, heartbeat_interval_ms=0) as worker:
        worker.run_once()
        assert not worker.errors
    assert stack.tasks.get_run(run.run_id).state == "waiting_approval"
    assert not stack.log.read_run(run.run_id, event_types=("tool.intent", "model.intent"))
    assert approve_pending(stack, run.run_id)
    if mutate_before_spawn:
        import tianshu.tools as tools
        original = tools._HANDLERS["run_command"]

        def mutate(workspace, args, context):
            (ws / "raced.txt").write_text("input changed after approval and execution permit")
            return original(workspace, args, context)
        monkeypatch.setitem(tools._HANDLERS, "run_command", mutate)
    result = engine_for(stack, model).resume_run(run.run_id)
    assert result.state == "succeeded"
    assert plans.get(view["plan_id"])["steps"][0]["state"] == (
        "failed" if mutate_before_spawn else "satisfied")
    events = stack.log.read_run(run.run_id)
    assert any(e.type == "tool.intent" for e in events)
    check_result = next(e for e in events if e.type == "verification.result")
    if mutate_before_spawn:
        assert check_result.payload["result"] != "passed"
        tool_result = next(e for e in events if e.type == "tool.result")
        assert tool_result.payload["status"] == "rejected"
        assert "stdout" not in tool_result.payload["output"]
    else:
        assert any(e.type == "receipt" for e in events)
        assert check_result.payload["result"] == "passed"
    assert next(e for e in events if e.seq == check_result.payload["verified_event_seq"]).type == (
        "tool.result")
    from tianshu.agent_documents import EVIDENCE_SCHEMA, validate_document
    from tianshu.plans import read_material
    from tianshu.replay import replay
    evidence = read_material(stack, task.task_id, check_result.payload["evidence_refs"][0])
    validate_document(EVIDENCE_SCHEMA, evidence, "命令证据")
    assert evidence["kind"] == "command_result"
    assert evidence["event_seqs"] == [check_result.payload["verified_event_seq"]]
    assert replay(stack.log, run.run_id).ok


def test_human_check_after_dependencies_even_when_listed_first(env):
    import copy

    from tianshu.verification_driver import start_verification
    stack, ws, base = env
    task = stack.tasks.create_task("先检查文件再请人审阅", [str(ws)])
    mission = prepare_mission(stack.tasks, task.task_id, criteria=[
        {"criterion_id": "report", "description": "报告可读", "mode": "human"},
    ]).to_dict()
    document = copy.deepcopy(base["document"])
    manifest = copy.deepcopy(base["manifest"])
    human = {
        "format_version": 1, "check_id": "review", "criterion_ids": ["report"],
        "required": True, "kind": "human", "inputs": {
            "question": "请审阅", "required_artifact_ids": [], "required_check_ids": ["exists"],
        },
    }
    manifest["checks"].insert(0, human)
    manifest.update(mission_id=mission["mission_id"], mission_digest=jcs_sha256(mission))
    document.update(plan_id="plan_order", mission_id=mission["mission_id"],
                    mission_digest=jcs_sha256(mission), verification_manifest={
                        "digest": jcs_sha256(manifest),
                        "content_ref": "content://sha256:" + jcs_sha256(manifest),
                    })
    document["steps"][0]["verification_ids"] = ["review", "exists"]
    plans = PlanStore(stack)
    view = plans.create(task.task_id, mission, document, manifest, request_id="order")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm_order")
    plans.change_step(view["plan_id"], "write", "running", request_id="run_order")
    view = plans.change_step(view["plan_id"], "write", "verifying", request_id="verify_order")
    (ws / "report.md").write_text("report")
    run = start_verification(stack, view, "write", request_id="ordered")
    with Worker(stack, model=ScriptedModel([]), heartbeat_interval_ms=0) as worker:
        worker.run_once()
        assert not worker.errors
    assert stack.tasks.get_run(run.run_id).state == "succeeded"
    requests = stack.log.read_run(run.run_id, event_types=("verification.requested",))
    assert [e.payload["check_id"] for e in requests] == ["exists", "review"]
