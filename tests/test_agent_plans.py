"""任务图、确定性验收及真实人工控制面，不以模型自述代替证据。"""

from __future__ import annotations

import copy
import hashlib

import pytest
from support import stack_for

from tianshu.canonical import jcs_sha256
from tianshu.events import EventLog
from tianshu.mission import prepare_mission


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "work"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    task = stack.tasks.create_task("生成一份可核对的报告", [str(ws)])
    mission = prepare_mission(stack.tasks, task.task_id, criteria=[
        {"criterion_id": "report", "description": "报告存在", "mode": "machine"},
    ]).to_dict()
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


def test_partial_replan_carries_only_unchanged_fresh_evidence(env):
    from tianshu.verification import request_check, step_verified
    stack, task, ws, mission, document, manifest = env
    document["steps"].append({
        **copy.deepcopy(document["steps"][0]), "step_id": "follow",
        "depends_on": ["write"], "expected_artifacts": [],
    })
    plans = store(env)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm")
    (ws / "report.md").write_bytes(b"report")
    plans.change_step(view["plan_id"], "write", "running", request_id="running")
    view = plans.change_step(view["plan_id"], "write", "verifying", request_id="verifying")
    request_check(stack, view, "write", "exists", run_id="", request_id="check")
    view = plans.change_step(view["plan_id"], "write", "satisfied", request_id="satisfied")
    new = copy.deepcopy(document)
    new.update(revision=2, previous_digest=view["document_digest"])
    new["steps"][1]["instruction"] = "修改后续步骤"
    replanned = plans.replan(view["plan_id"], mission, new, manifest,
                             expected_revision=1, request_id="replan")
    plans.confirm(view["plan_id"], expected_revision=2,
                  document_digest=replanned["document_digest"], request_id="confirm2")
    latest = plans.get(view["plan_id"])
    assert latest["steps"][0]["state"] == "satisfied"
    assert step_verified(stack, latest, "write")
    assert latest["steps"][1]["state"] == "ready"
    (ws / "report.md").write_bytes(b"changed")
    assert not step_verified(stack, latest, "write")
    assert plans.get(view["plan_id"])["steps"][1]["state"] != "ready"


def test_plan_rejects_incomplete_mission(env):
    stack, task, _, mission, document, manifest = env
    del mission["prompt"]
    manifest["mission_digest"] = document["mission_digest"] = jcs_sha256(mission)
    document["verification_manifest"] = {
        "digest": jcs_sha256(manifest), "content_ref": "content://sha256:" + jcs_sha256(manifest),
    }
    with pytest.raises(Exception, match="Mission"):
        store(env).create(task.task_id, mission, document, manifest, request_id="create")
    assert not stack.log.read_types(("plan.created",))


@pytest.mark.parametrize("inputs", [
    {"workspace_binding_id": True},
    {"workspace_binding_id": "-1"},
    {"workspace_binding_id": "00"},
    {"relative_path": "."},
    {"relative_path": "report\x00.md"},
])
def test_manifest_rejects_invalid_file_binding(env, inputs):
    from tianshu.verification import validate_manifest
    _, _, _, mission, _, manifest = env
    manifest["checks"][0]["inputs"].update(inputs)
    with pytest.raises(ValueError):
        validate_manifest(mission, manifest)


def test_step_transition_is_idempotent_and_checks_run_ownership(env):
    stack, task, ws, mission, document, manifest = env
    plans = store(env)
    view = plans.create(task.task_id, mission, document, manifest, request_id="create")
    plans.confirm(view["plan_id"], expected_revision=1,
                  document_digest=view["document_digest"], request_id="confirm")
    other = stack.tasks.create_task("其他任务", [str(ws)])
    foreign = stack.tasks.start_run(other.task_id)
    with pytest.raises(ValueError):
        plans.change_step(view["plan_id"], "write", "running",
                          run_id=foreign.run_id, request_id="foreign")
    running = plans.change_step(view["plan_id"], "write", "running", request_id="running")
    assert plans.change_step(
        view["plan_id"], "write", "running", request_id="running") == running
    with pytest.raises(ValueError):
        plans.change_step(view["plan_id"], "write", "blocked", request_id="running")


def test_source_reference_reads_raw_content_and_exact_event_reference(env):
    from tianshu.contents import make_ref
    from tianshu.event_schema import minimal_payload
    from tianshu.fsutil import atomic_write_bytes
    from tianshu.verification import observe_check
    stack, task, _, _, _, manifest = env
    raw = b"plain source\r\n\xff"
    digest = hashlib.sha256(raw).hexdigest()
    ref = make_ref(digest)
    atomic_write_bytes(stack.contents.blob_dir / digest, raw)
    stack.contents.record(digest, raw, task_id=task.task_id)
    run = stack.tasks.start_run(task.task_id)
    payload = minimal_payload("tool.result")
    payload["output"] = {"source": {"ref": ref}}
    seq = stack.log.append(run.run_id, "tool.result", payload, task_id=task.task_id)
    check = {**manifest["checks"][0], "kind": "source_reference", "inputs": {
        "content_ref": ref, "source_event_seq": seq, "cited_digest": digest,
        "locator": "paragraph 1",
    }}
    assert observe_check(stack, task.task_id, check)["result"] == "passed"
    payload["output"] = {"text": "unbound prefix" + ref}
    seq = stack.log.append(run.run_id, "tool.result", payload, task_id=task.task_id)
    check["inputs"]["source_event_seq"] = seq
    assert observe_check(stack, task.task_id, check)["result"] == "inconclusive"


def test_document_schemas_match_runtime_contracts():
    import json
    from pathlib import Path

    from tianshu.agent_documents import EVIDENCE_SCHEMA, MANIFEST_SCHEMA, PLAN_SCHEMA
    root = Path(__file__).parents[1] / "schemas"
    for name, schema in (("plan", PLAN_SCHEMA), ("verification", MANIFEST_SCHEMA),
                         ("evidence", EVIDENCE_SCHEMA)):
        assert json.loads((root / f"{name}.json").read_text())["$defs"][name] == schema


def test_fingerprint_rejects_directory_changes_during_scan(env, monkeypatch):
    import tianshu.verification as module
    stack, task, ws, *_ = env
    (ws / "report.md").write_text("report")
    real = module._file_bytes

    def changing(path):
        data = real(path)
        (ws / "late.txt").write_text("not in the original enumeration")
        return data
    monkeypatch.setattr(module, "_file_bytes", changing)
    with pytest.raises(ValueError, match="输入"):
        module.input_fingerprint(stack, task.task_id)
