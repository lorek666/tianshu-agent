"""独立第二读者探针；不提交的复审证据。"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from support import engine_for, stack_for, tool_capable_profile
from test_model_contract import VENDORS, FakeTransport, _model, _payload

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay
from tianshu.run_policy import EFFECTS, PURE_ONLY
from tianshu.workflow import workflow_for


class RecordModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.inputs = []

    def complete(self, messages, tools):
        self.inputs.append(copy.deepcopy((messages, tools)))
        return super().complete(messages, tools)


@pytest.fixture
def review_env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "protected.txt").write_bytes(b"original")
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=4))
    task = stack.tasks.create_task("调查 protected.txt 后给出修改计划。", [str(ws)])
    yield plane, stack, task, ws
    log.close()


def _queue(plane, task, mode="plan", rid="plan"):
    return plane.handle("run.start", {
        "task_id": task.task_id, "mode": mode, "request_id": rid,
        "execution_policy": EFFECTS.to_dict(),
    })["run"]["run_id"]


def _finish(stack, run_id, text="逐步计划：读取文件、修改标题、检查结果。"):
    model = RecordModel([ModelResponse("end_turn", content=text)])
    result = engine_for(stack, model).resume_run(run_id)
    assert result.state == "succeeded", result
    return model


def _compact(plane, stack, task):
    source = _queue(plane, task, "execute", "source")
    _finish(stack, source)
    return plane.handle("run.compact", {
        "task_id": task.task_id, "source_run_id": source, "request_id": "compact",
    })["run"]["run_id"]


@pytest.mark.parametrize("vendor", VENDORS)
@pytest.mark.parametrize("mode", ["plan", "compact"])
def test_real_adapter_malicious_effects_are_never_applied(review_env, vendor, mode):
    plane, stack, task, ws = review_env
    rid = _queue(plane, task) if mode == "plan" else _compact(plane, stack, task)
    transport = FakeTransport(_payload(vendor, text="无视计划，直接覆盖", tool={
        "call_id": "malicious", "name": "modify_text",
        "args": {"path": "protected.txt", "content": "corrupt"},
    }))
    model = _model(vendor, transport)
    model.capability_profile = lambda: tool_capable_profile(vendor)
    result = engine_for(stack, model, limits=Limits(max_turns=1)).resume_run(rid)
    assert result.state == "failed"
    assert (ws / "protected.txt").read_bytes() == b"original"
    assert stack.tasks.run_execution_policy(rid) == PURE_ONLY
    assert not any(e.type == "tool.intent" for e in stack.log.read_run(rid))
    assert replay(stack.log, rid).ok
    assert len(transport.calls) == 1


def test_plan_reads_then_confirmed_execution_still_waits_for_real_approval(review_env):
    plane, stack, task, ws = review_env
    plan_id = _queue(plane, task)
    model = RecordModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("investigate", "read_text", {"path": "protected.txt"}),
        ]),
        ModelResponse("end_turn", content="把原文 original 修改为 approved。"),
    ])
    result = engine_for(stack, model).resume_run(plan_id)
    assert result.state == "succeeded"
    assert any(e.type == "tool.result" and e.payload["status"] == "succeeded"
               for e in stack.log.read_run(plan_id))
    executed = plane.handle("plan.execute", {
        "plan_run_id": plan_id, "goal_revision": 1, "request_id": "confirm",
    })["run"]["run_id"]
    malicious = ScriptedModel([ModelResponse("tool_use", tool_calls=[
        ToolCall("overwrite", "modify_text", {"path": "protected.txt", "content": "approved"}),
    ])])
    result = engine_for(stack, malicious).resume_run(executed)
    assert result.state == "waiting_approval"
    assert (ws / "protected.txt").read_bytes() == b"original"
    assert stack.approvals.pending(run_id=executed)
    assert not any(e.type == "tool.intent" for e in stack.log.read_run(executed))
    assert replay(stack.log, plan_id).ok
    assert replay(stack.log, executed).ok


@pytest.mark.parametrize("point", ["workflow-after-call", "workflow-after-result"])
@pytest.mark.parametrize("mode", ["plan", "compact"])
def test_hard_exit_recovery_does_not_charge_or_call_again(review_env, mode, point):
    plane, stack, task, _ = review_env
    rid = _queue(plane, task) if mode == "plan" else _compact(plane, stack, task)
    root = Path(stack.tasks.data_root)
    child = r'''
import json, sys
from pathlib import Path
from support import engine_for, stack_for
from tianshu.events import EventLog
from tianshu.model import ScriptedModel, ModelResponse
root = Path(sys.argv[1])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
stack = stack_for(log, data_root=root)
class Model(ScriptedModel):
    def complete(self, messages, tools):
        with (root / "actual_calls.jsonl").open("a") as out:
            out.write(json.dumps({"run_id": sys.argv[2]}) + "\n")
            out.flush()
        return super().complete(messages, tools)
model = Model([ModelResponse("end_turn", content="保留目标、约束与剩余步骤。",
                            usage={"input_tokens": 11, "output_tokens": 7})])
engine_for(stack, model).resume_run(sys.argv[2])
'''
    proc = subprocess.run(
        [sys.executable, "-c", child, str(root), rid],
        env={**os.environ, "TIANSHU_FAILPOINT": point,
             "PYTHONPATH": str(Path(__file__).parent) + os.pathsep
             + os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 77, proc.stderr
    assert f"[failpoint] {point}" in proc.stderr
    before = stack.budget.usage(f"run:{rid}")
    replacement = RecordModel([])
    resumed = engine_for(stack, replacement).resume_run(rid)
    assert resumed.state == ("failed" if point.endswith("call") else "succeeded")
    assert replacement.inputs == []
    assert stack.budget.usage(f"run:{rid}") == before
    assert before["settled"]["requests"] == 1
    assert before["settled"]["tokens"] == 18
    calls = [json.loads(row) for row in (root / "actual_calls.jsonl").read_text().splitlines()]
    assert calls == [{"run_id": rid}]
    assert len([e for e in stack.log.read_run(rid) if e.type == "model.intent"]) == 1
    assert replay(stack.log, rid).ok
    with pytest.raises(Exception, match="终结"):
        engine_for(stack, RecordModel([])).resume_run(rid)
    assert stack.budget.usage(f"run:{rid}") == before


def test_source_events_survive_and_summary_replays_without_projections(review_env):
    plane, stack, task, _ = review_env
    compact = _compact(plane, stack, task)
    old_events = stack.log.read_run(workflow_for(stack.log, compact).source_run_id)
    _finish(stack, compact, "原目标与硬约束；后续读取 protected.txt。")
    follow = _queue(plane, task, "execute", "follow")
    model = _finish(stack, follow)
    assert "原目标与硬约束" in model.inputs[0][0][1]["content"]
    stack.tasks.rebuild_from_events()
    assert replay(stack.log, compact).ok
    assert replay(stack.log, follow).ok
    assert stack.log.read_run(old_events[0].run_id) == old_events


@pytest.mark.parametrize("kind", ["end_turn_with_tools", "cannot_fit_source"])
def test_successful_plan_must_have_confirmable_final_text(review_env, kind):
    plane, stack, task, _ = review_env
    plan = _queue(plane, task)
    response = (
        ModelResponse("end_turn", content="计划", tool_calls=[
            ToolCall("invalid-final-tool", "read_text", {"path": "protected.txt"}),
        ])
        if kind == "end_turn_with_tools"
        else ModelResponse("end_turn", content="x" * (128 * 1024 + 1))
    )
    result = engine_for(stack, ScriptedModel([response])).resume_run(plan)
    print("invalid_plan_state:", kind, result.state)
    if result.state == "succeeded":
        with pytest.raises(ControlError) as caught:
            plane.handle("plan.execute", {
                "plan_run_id": plan, "goal_revision": 1, "request_id": "cannot-confirm",
            })
        print("invalid_plan_confirm_error:", caught.value.message)
    assert result.state == "failed"


def test_compact_pending_pure_tool_gate_negative_control(review_env, monkeypatch):
    plane, stack, task, _ = review_env
    rid = _compact(plane, stack, task)
    original = workflow_for

    def bypass_compact_gate(log, run_id, *, events=None):
        found = original(log, run_id, events=events)
        return replace(found, mode="plan") if found and found.mode == "compact" else found

    # 只在本用例把 compact 误当 plan，并旁路新增的 Mission 固定交集；
    # 两道工具门都移除后应实际执行仅读工具，证明零派发断言仍能抓住失守。
    monkeypatch.setattr("tianshu.workflow.workflow_for", bypass_compact_gate)
    monkeypatch.setattr("tianshu.mission.effective_manifest", lambda _log, _rid, _reg, rows: rows)
    model = ScriptedModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("should-not-read", "read_text", {"path": "protected.txt"}),
        ]),
        ModelResponse("end_turn", content="这不是合法的压缩路径"),
    ])
    engine_for(stack, model).resume_run(rid)
    with pytest.raises(AssertionError):
        assert not any(e.type == "tool.result" for e in stack.log.read_run(rid))


def test_wrong_confirmation_and_enqueue_failure_leave_no_partial_records(review_env, monkeypatch):
    plane, stack, task, _ = review_env
    plan_id = _queue(plane, task)
    _finish(stack, plan_id)
    before = stack.log.read_after(0)
    with pytest.raises(ControlError):
        plane.handle("plan.execute", {
            "plan_run_id": plan_id, "goal_revision": 999, "request_id": "bad",
        })
    assert stack.log.read_after(0) == before

    def queue_failure(*_args, **_kwargs):
        raise RuntimeError("independent queue failure")

    monkeypatch.setattr(stack.claims, "enqueue", queue_failure)
    with pytest.raises(RuntimeError, match="independent queue failure"):
        plane.handle("plan.execute", {
            "plan_run_id": plan_id, "goal_revision": 1, "request_id": "good",
        })
    assert stack.log.read_after(0) == before
    assert len(stack.tasks.list_runs(task.task_id)) == 1
    assert stack.tasks.get_task(task.task_id).active_run_id is None


def test_later_legitimate_undo_must_not_invalidate_saved_compact_source(review_env):
    plane, stack, task, ws = review_env
    source = _queue(plane, task, "execute", "source-write")
    result = engine_for(stack, ScriptedModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("create", "create_report", {"name": "created.md", "content": "created"}),
        ]),
        ModelResponse("end_turn", content="已创建 created.md。"),
    ])).resume_run(source)
    assert result.state == "succeeded", result
    assert (ws / "created.md").exists()
    compact = plane.handle("run.compact", {
        "task_id": task.task_id, "source_run_id": source, "request_id": "compact-written",
    })["run"]["run_id"]
    _finish(stack, compact, "已创建 created.md，继续处理。")
    assert replay(stack.log, compact).ok
    assert plane.handle("undo.apply", {"run_id": source})["applied"]
    assert not (ws / "created.md").exists()
    assert any(event.type == "undo.completed" for event in stack.log.read_run(source))
    # 源事件仅合法追加了撤销审计，冻结材料与其来源身份都没有改变。
    report = replay(stack.log, compact)
    try:
        plane.handle("state", {})
        state_error = None
    except ControlError as exc:
        state_error = (exc.code, exc.message)
    print("after_undo_replay_problems:", report.problems)
    print("after_undo_state_error:", state_error)
    assert report.ok
    assert state_error is None
