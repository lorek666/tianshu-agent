"""任务图、确定性验收及真实人工控制面，不以模型自述代替证据。"""

from __future__ import annotations

import copy
import hashlib

import pytest
from support import stack_for

from tianshu.canonical import jcs_sha256
from tianshu.events import EventLog


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "work"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    task = stack.tasks.create_task("生成一份可核对的报告", [str(ws)])
    mission = {
        "mission_id": "mission_test", "task_id": task.task_id, "goal_revision": 1,
        "criteria": [{"criterion_id": "report", "description": "报告存在", "mode": "machine"}],
    }
    manifest = {
        "format_version": 1, "mission_id": mission["mission_id"],
        "mission_digest": jcs_sha256(mission),
        "checks": [{
            "format_version": 1, "check_id": "exists", "criterion_ids": ["report"],
            "required": True, "kind": "file_exists",
            "inputs": {"workspace_binding_id": "0", "relative_path": "report.md",
                       "file_kind": "regular"},
        }],
    }
    document = {
        "format_version": 1, "plan_id": "plan_test", "revision": 1,
        "mission_id": mission["mission_id"], "mission_digest": jcs_sha256(mission),
        "previous_digest": None,
        "verification_manifest": {
            "content_ref": "content://sha256:" + jcs_sha256(manifest),
            "digest": jcs_sha256(manifest),
        },
        "steps": [{
            "step_id": "write", "title": "生成报告", "instruction": "创建 report.md",
            "depends_on": [], "criterion_ids": ["report"],
            "expected_artifacts": [{"artifact_id": "report", "kind": "file",
                                    "locator": "report.md"}],
            "verification_ids": ["exists"],
        }],
    }
    yield stack, task, ws, mission, document, manifest
    log.close()


def store(env):
    from tianshu.plans import PlanStore
    return PlanStore(env[0])


def test_valid_graph_stays_unconfirmed_and_does_not_create_run(env):
    _, task, _, mission, document, manifest = env
    view = store(env).create(task.task_id, mission, document, manifest, request_id="create")
    assert not view["confirmed"]
    assert view["steps"][0]["state"] == "pending"
    assert not env[0].tasks.list_runs(task.task_id)


@pytest.mark.parametrize("mutation", [
    lambda d: d["steps"][0].update(depends_on=["write"]),
    lambda d: d["steps"][0].update(depends_on=["missing"]),
    lambda d: d["steps"][0].update(verification_ids=[]),
    lambda d: d["steps"][0].update(status="satisfied"),
    lambda d: d.update(mission_digest="f" * 64),
])
def test_invalid_graph_rejected_without_event(env, mutation):
    stack, task, _, mission, document, manifest = env
    mutation(document)
    before = stack.log.seq_head()
    with pytest.raises(ValueError):
        store(env).create(task.task_id, mission, document, manifest, request_id="create")
    assert stack.log.seq_head() == before


def test_graph_confirmation_is_cas_and_idempotent(env):
    _, task, _, mission, document, manifest = env
    plans = store(env)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    confirmed = plans.confirm(view["plan_id"], expected_revision=1,
                              document_digest=view["document_digest"], request_id="confirm")
    assert confirmed["confirmed"]
    assert confirmed["steps"][0]["state"] == "ready"
    assert plans.confirm(view["plan_id"], expected_revision=1,
                         document_digest=view["document_digest"], request_id="confirm") == confirmed
    with pytest.raises(ValueError):
        plans.confirm(view["plan_id"], expected_revision=2,
                      document_digest=view["document_digest"], request_id="other")


def test_file_hash_observes_raw_bytes_and_rechecks_freshness(env):
    from tianshu.verification import observe_check
    stack, task, ws, _, _, manifest = env
    raw = "证据\r\n".encode()
    path = ws / "report.md"
    path.write_bytes(raw)
    check = {**manifest["checks"][0], "kind": "file_hash",
             "inputs": {**manifest["checks"][0]["inputs"], "algorithm": "sha256",
                        "expected_digest": hashlib.sha256(raw).hexdigest()}}
    first = observe_check(stack, task.task_id, check)
    assert first["result"] == "passed"
    path.write_bytes(b"changed")
    second = observe_check(stack, task.task_id, check)
    assert second["result"] == "failed"
    assert first["input_fingerprint"] != second["input_fingerprint"]


def test_file_check_rejects_symlink_outside_workspace(env, tmp_path):
    from tianshu.verification import observe_check
    stack, task, ws, _, _, manifest = env
    secret = tmp_path / "secret"
    secret.write_text("private")
    (ws / "report.md").symlink_to(secret)
    result = observe_check(stack, task.task_id, manifest["checks"][0])
    assert result["result"] != "passed"
    assert "private" not in str(result)


def test_plan_revision_preserves_history_and_invalidates_changed_step(env):
    _, task, _, mission, document, manifest = env
    plans = store(env)
    first = plans.create(task.task_id, mission, document, manifest, request_id="create")
    new = copy.deepcopy(document)
    new.update(revision=2, previous_digest=first["document_digest"])
    new["steps"][0]["instruction"] = "生成更完整的报告"
    second = plans.replan(first["plan_id"], mission, new, manifest,
                          expected_revision=1, request_id="replan")
    assert second["revision"] == 2 and not second["confirmed"]
    assert plans.get(first["plan_id"], revision=1)["document"] == document
    with pytest.raises(ValueError):
        plans.replan(first["plan_id"], mission, new, manifest,
                     expected_revision=1, request_id="stale")


def human_plan(env):
    from tianshu.verification import request_check
    stack, task, ws, mission, document, manifest = env
    mission["criteria"][0]["mode"] = "human"
    manifest["mission_digest"] = document["mission_digest"] = jcs_sha256(mission)
    manifest["checks"][0].update(kind="human", inputs={
        "question": "报告是否满足目标？", "required_artifact_ids": ["report"],
        "required_check_ids": [],
    })
    document["verification_manifest"] = {
        "content_ref": "content://sha256:" + jcs_sha256(manifest),
        "digest": jcs_sha256(manifest),
    }
    (ws / "report.md").write_text("待人审阅的报告")
    plans = store(env)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm")
    plans.change_step(view["plan_id"], "write", "running", request_id="running")
    view = plans.change_step(view["plan_id"], "write", "verifying", request_id="verifying")
    request = request_check(stack, view, "write", "exists", run_id="", request_id="request")
    return view, request


def test_human_acceptance_unlocks_step_and_is_idempotent(env):
    from tianshu.permissions import HumanIdentity
    from tianshu.verification import decide_check
    view, request = human_plan(env)
    params = {
        **{k: request[k] for k in (
            "plan_id", "revision", "step_id", "check_id", "check_digest",
            "attempt_id", "input_fingerprint", "evidence_refs",
        )},
        "format_version": 1, "request_id": "accept",
        "expected_state_revision": view["state_revision"],
        "task_id": view["task_id"], "mission_digest": jcs_sha256(view["mission"]),
        "plan_digest": view["document_digest"], "decision": "accept", "reason": "已审阅产物",
    }
    result = decide_check(env[0], params, actor=HumanIdentity.local_cli())
    assert result["result"] == "passed"
    assert decide_check(env[0], params, actor=HumanIdentity.local_cli()) == result
    assert store(env).get(view["plan_id"])["steps"][0]["state"] == "satisfied"
    with pytest.raises(ValueError):
        decide_check(env[0], {**params, "decision": "reject"}, actor=HumanIdentity.local_cli())
    with pytest.raises(ValueError):
        decide_check(env[0], {**params, "request_id": "model"}, actor="human")


def test_human_acceptance_rejects_changed_inputs(env):
    from tianshu.permissions import HumanIdentity
    from tianshu.verification import decide_check
    view, request = human_plan(env)
    (env[2] / "report.md").write_text("后来被改写")
    params = {
        **{k: request[k] for k in (
            "plan_id", "revision", "step_id", "check_id", "check_digest",
            "attempt_id", "input_fingerprint", "evidence_refs",
        )},
        "format_version": 1, "request_id": "accept",
        "expected_state_revision": view["state_revision"],
        "task_id": view["task_id"], "mission_digest": jcs_sha256(view["mission"]),
        "plan_digest": view["document_digest"], "decision": "accept", "reason": "旧页面",
    }
    with pytest.raises(ValueError):
        decide_check(env[0], params, actor=HumanIdentity.local_cli())


def test_human_request_freezes_actual_artifact_and_rejects_missing(env):
    from tianshu.plans import read_material
    view, request = human_plan(env)
    evidence = read_material(env[0], view["task_id"], request["evidence_refs"][0])
    assert evidence["kind"] == "human_decision"
    assert evidence["task_id"] == view["task_id"]
    assert evidence["plan_digest"] == view["document_digest"]
    material = read_material(env[0], view["task_id"], evidence["content_refs"][0])
    assert material["artifacts"][0]["artifact_id"] == "report"
    assert material["artifacts"][0]["sha256"] == hashlib.sha256(
        (env[2] / "report.md").read_bytes()).hexdigest()
    (env[2] / "report.md").unlink()
    from tianshu.verification import request_check
    with pytest.raises(ValueError):
        request_check(env[0], view, "write", "exists", run_id="", request_id="missing")


def test_verification_rejects_cross_step_and_unconfirmed_requests(env):
    from tianshu.verification import request_check
    stack, task, _, mission, document, manifest = env
    view = store(env).create(task.task_id, mission, document, manifest, request_id="create")
    before = stack.log.seq_head()
    with pytest.raises(ValueError):
        request_check(stack, view, "write", "exists", run_id="", request_id="bad")
    assert stack.log.seq_head() == before
    with pytest.raises(ValueError):
        request_check(stack, view, "other", "exists", run_id="", request_id="bad")
