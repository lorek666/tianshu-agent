"""真实 RPC 的 Plan/人工验收及有限授权装配。"""

from __future__ import annotations

import pytest
from test_agent_plans import env as env
from test_agent_plans import human_plan, store

from tianshu.control import ControlError, ControlPlane
from tianshu.model import ScriptedModel


def test_graph_confirmation_and_human_decision_via_real_rpc(env):
    plane = ControlPlane(env[0], model=ScriptedModel([]))
    view, request = human_plan(env)
    read = plane.handle("plan.graph.get", {"plan_id": view["plan_id"]})
    assert read["plan"]["document_digest"] == view["document_digest"]
    assert read["verification_requests"][0]["attempt_id"] == request["attempt_id"]
    params = {
        **{k: request[k] for k in (
            "plan_id", "revision", "step_id", "check_id", "check_digest",
            "attempt_id", "input_fingerprint", "evidence_refs",
        )},
        "format_version": 1, "request_id": "accept",
        "expected_state_revision": view["state_revision"],
        "task_id": view["task_id"], "mission_digest": view["document"]["mission_digest"],
        "plan_digest": view["document_digest"], "decision": "accept", "reason": "已逐项核对",
    }
    assert plane.handle("verification.decide", params)["result"]["result"] == "passed"
    assert store(env).get(view["plan_id"])["steps"][0]["state"] == "satisfied"
    with pytest.raises(ControlError):
        plane.handle("verification.decide", {**params, "actor": "human"})


@pytest.mark.parametrize("level", ["evidence", "review", "artifact"])
@pytest.mark.parametrize("damage", ["missing", "corrupt", "ungranted"])
def test_human_decision_refuses_damaged_frozen_material_before_writing(env, level, damage):
    from tianshu.plans import read_material

    plane = ControlPlane(env[0], model=ScriptedModel([]))
    stack = env[0]
    view, request = human_plan(env)
    evidence_ref = request["evidence_refs"][0]
    evidence = read_material(stack, view["task_id"], evidence_ref)
    review_ref = evidence["content_refs"][0]
    review = read_material(stack, view["task_id"], review_ref)
    ref = {"evidence": evidence_ref, "review": review_ref,
           "artifact": review["artifacts"][0]["content_ref"]}[level]
    digest = ref.rsplit(":", 1)[1]
    path = stack.contents.blob_dir / digest
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b'{"fabricated":true}')
    else:
        stack.log.conn.execute(
            "DELETE FROM content_grants WHERE digest=? AND task_id=?", (digest, view["task_id"]),
        )
    params = {
        **{k: request[k] for k in (
            "plan_id", "revision", "step_id", "check_id", "check_digest",
            "attempt_id", "input_fingerprint", "evidence_refs",
        )},
        "format_version": 1, "request_id": "accept_without_material",
        "expected_state_revision": view["state_revision"],
        "task_id": view["task_id"], "mission_digest": view["document"]["mission_digest"],
        "plan_digest": view["document_digest"], "decision": "accept", "reason": "已逐项核对",
    }
    before = stack.log.seq_head()
    with pytest.raises(ControlError):
        plane.handle("verification.decide", params)
    assert stack.log.seq_head() == before
    assert not stack.log.read_types(("verification.decided", "verification.result"))


def test_production_consent_store_rejects_run_without_confirmed_mission_plan(env):
    stack = env[0]
    plane = ControlPlane(stack, model=ScriptedModel([]))
    assert stack.dispatcher._consents is not None
    run = stack.tasks.start_run(env[1].task_id)
    with pytest.raises(ControlError):
        plane.handle("consent.prepare", {"run_id": run.run_id, "kind": "directory_overwrite",
                                         "directory": str(env[2]), "recursive": False})


def test_directory_consent_preview_uses_writable_workspace_binding(env, tmp_path):
    from tianshu.mission import MissionSpecV1, freeze_mission

    stack, task, ws, mission, document, manifest = env
    plane = ControlPlane(stack, model=ScriptedModel([]))
    plans = store(env)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm")
    run = stack.tasks.start_run(task.task_id)
    freeze_mission(stack.log, run.run_id, mission=MissionSpecV1.of(mission))
    plans.change_step(view["plan_id"], "write", "running", run_id=run.run_id,
                      request_id="execute")
    params = {"run_id": run.run_id, "kind": "directory_overwrite",
              "directory": str(ws), "recursive": False}
    before = stack.log.seq_head()
    preview = plane.handle("consent.prepare", params)["preview"]
    assert preview["matcher"]["directory"] == str(ws.resolve())
    assert preview["matcher"]["recursive"] is False
    assert preview["action"] == "overwrite"
    assert preview["tool_binding"]["tool_id"] == "modify_text"
    assert stack.log.seq_head() == before, "预览不能签发授权"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ControlError):
        plane.handle("consent.prepare", {**params, "directory": str(outside)})
