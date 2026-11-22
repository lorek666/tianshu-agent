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


def test_production_consent_store_rejects_run_without_confirmed_mission_plan(env):
    stack = env[0]
    plane = ControlPlane(stack, model=ScriptedModel([]))
    assert stack.dispatcher._consents is not None
    run = stack.tasks.start_run(env[1].task_id)
    with pytest.raises(ControlError):
        plane.handle("consent.prepare", {"run_id": run.run_id, "kind": "directory_overwrite",
                                         "directory": str(env[2]), "recursive": False})
