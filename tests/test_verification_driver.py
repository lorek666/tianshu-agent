"""独立验证 Run 真实经过 Worker，不能调用 provider 或绕过审批。"""

from __future__ import annotations

import pytest
from support import stack_for

from tianshu.canonical import jcs_sha256
from tianshu.events import EventLog
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
    mission = {"mission_id": "mission_test", "task_id": task.task_id, "goal_revision": 1,
               "criteria": [{"criterion_id": "report", "description": "文件存在",
                             "mode": "machine"}]}
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
