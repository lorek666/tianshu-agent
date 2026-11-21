"""手动压缩与计划的真实控制面/执行器/回放契约，不把模型输出当授权。"""

from __future__ import annotations

import copy
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import engine_for, stack_for

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay
from tianshu.run_policy import PURE_ONLY

PLAN = "计划：先阅读 notes.md，再修改其中的标题，最后运行检查。"
SUMMARY = "任务：整理笔记。约束：只用中文。已阅读 notes.md，下一步修改标题。"


class RecordingModel(ScriptedModel):
    def __init__(self, responses):
        super().__init__(responses)
        self.inputs = []

    def complete(self, messages, tools):
        self.inputs.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return super().complete(messages, tools)


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.md").write_bytes("原始标题\n".encode())
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=4))
    task = stack.tasks.create_task("整理笔记，只用中文。", [str(ws)])
    yield plane, stack, task, ws
    log.close()


def start(plane, task, mode="plan", request_id="plan-1"):
    return plane.handle("run.start", {
        "task_id": task.task_id, "mode": mode, "request_id": request_id,
    })["run"]


def finish(stack, run_id, text=PLAN):
    model = RecordingModel([ModelResponse("end_turn", content=text)])
    result = engine_for(stack, model).resume_run(run_id)
    assert result.state == "succeeded"
    return model


def confirm(plane, task, plan_id, request_id="execute-1"):
    return plane.handle("plan.execute", {
        "plan_run_id": plan_id,
        "goal_revision": task.goal_revision,
        "request_id": request_id,
    })["run"]


def test_plan_forces_readonly_despite_requested_effects(env):
    plane, stack, task, _ = env
    run = start(plane, task)
    assert stack.tasks.run_execution_policy(run["run_id"]) == PURE_ONLY
    assert run["state"] == "created"  # RPC 没有同步调用模型
    model = finish(stack, run["run_id"])
    assert "计划" in model.inputs[0][0][1]["content"]
    assert all(t["name"] != "modify_text" for t in model.inputs[0][1])
    assert replay(stack.log, run["run_id"]).ok


def test_malicious_plan_cannot_write_even_if_model_proposes_it(env):
    plane, stack, task, ws = env
    run = start(plane, task)
    model = RecordingModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("bad", "modify_text", {"path": "notes.md", "content": "篡改"}),
        ]),
        ModelResponse("end_turn", content=PLAN),
    ])
    engine_for(stack, model).resume_run(run["run_id"])
    assert (ws / "notes.md").read_bytes() == "原始标题\n".encode()
    assert not any(e.type == "tool.intent" for e in stack.log.read_run(run["run_id"]))
    with pytest.raises(Exception, match="pure|仅读|仅允许"):
        stack.tasks.require_effects_allowed(run["run_id"])


def test_confirm_plan_creates_new_bound_run_once(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    finish(stack, plan["run_id"])
    run = confirm(plane, task, plan["run_id"])
    again = confirm(plane, task, plan["run_id"])
    assert run["run_id"] == again["run_id"]
    assert run["run_id"] != plan["run_id"]
    assert stack.tasks.run_execution_policy(run["run_id"]).mode == "effects"
    assert len(stack.tasks.list_runs(task.task_id)) == 2
    model = finish(stack, run["run_id"], "按计划完成")
    assert PLAN in model.inputs[0][0][1]["content"]
    assert replay(stack.log, run["run_id"]).ok


def test_confirm_rejects_unfinished_or_stale_plan_without_new_job(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    with pytest.raises(ControlError):
        confirm(plane, task, plan["run_id"])
    finish(stack, plan["run_id"])
    stack.tasks.revise_task(task.task_id, goal="替换目标，不要修改笔记")
    with pytest.raises(ControlError):
        confirm(plane, task, plan["run_id"])
    assert len(stack.tasks.list_runs(task.task_id)) == 1


def test_same_request_id_with_different_mode_is_rejected(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    assert start(plane, task)["run_id"] == plan["run_id"]
    with pytest.raises(ControlError):
        start(plane, task, "execute")
    assert len(stack.tasks.list_runs(task.task_id)) == 1


def test_same_request_id_cannot_change_execution_policy_or_output(env):
    plane, stack, task, _ = env
    run = start(plane, task, "execute")
    for changed in (
        {"out_name": "different.md"},
        {"execution_policy": PURE_ONLY.to_dict()},
    ):
        with pytest.raises(ControlError):
            plane.handle("run.start", {
                "task_id": task.task_id, "mode": "execute",
                "request_id": "plan-1", **changed,
            })
    assert len(stack.tasks.list_runs(task.task_id)) == 1
    assert stack.tasks.get_run(run["run_id"]) is not None


def test_unknown_mode_fails_before_run_or_job_is_created(env):
    plane, stack, task, _ = env
    with pytest.raises(ControlError):
        start(plane, task, "surprise")
    assert stack.tasks.list_runs(task.task_id) == []


def test_compact_is_background_model_work_without_tools_and_reuses_summary(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"], "已阅读 notes.md。\n" + "过程细节。" * 300)
    original_events = stack.log.read_run(source["run_id"])
    compact = plane.handle("run.compact", {
        "task_id": task.task_id, "source_run_id": source["run_id"],
        "request_id": "compact-1",
    })["run"]
    assert compact["state"] == "created"
    assert stack.tasks.run_execution_policy(compact["run_id"]) == PURE_ONLY
    model = finish(stack, compact["run_id"], SUMMARY)
    assert model.inputs[0][1] == []
    assert "notes.md" in model.inputs[0][0][1]["content"]
    assert stack.log.read_run(source["run_id"]) == original_events
    assert replay(stack.log, compact["run_id"]).ok

    stack.tasks.revise_task(
        task.task_id, goal=task.goal + "\n\n补充要求：接着检查标题",
    )
    next_run = start(plane, task, "execute", "after-compact")
    model = finish(stack, next_run["run_id"], "完成")
    prompt = model.inputs[0][0][1]["content"]
    assert SUMMARY in prompt
    assert "接着检查标题" in prompt
    assert "过程细节。" not in prompt
    assert replay(stack.log, next_run["run_id"]).ok


def test_compact_rejects_cross_task_and_active_source(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    with pytest.raises(ControlError):
        plane.handle("run.compact", {
            "task_id": task.task_id, "source_run_id": source["run_id"],
            "request_id": "busy",
        })
    finish(stack, source["run_id"])
    other = stack.tasks.create_task("别人的任务", [])
    with pytest.raises(ControlError):
        plane.handle("run.compact", {
            "task_id": other.task_id, "source_run_id": source["run_id"],
            "request_id": "cross",
        })
    assert stack.tasks.list_runs(other.task_id) == []


def test_compact_never_dispatches_even_pure_tool_proposals(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"], "源内容" * 100)
    compact = plane.handle("run.compact", {
        "task_id": task.task_id, "request_id": "compact",
    })["run"]
    model = RecordingModel([
        ModelResponse("tool_use", tool_calls=[ToolCall("bad", "read_text", {
            "path": "notes.md",
        })]),
    ])
    result = engine_for(stack, model).resume_run(compact["run_id"])
    assert result.state == "failed"
    assert not any(
        e.type in ("tool.result", "tool.intent")
        for e in stack.log.read_run(compact["run_id"])
    )


def test_empty_compact_output_is_not_used_as_context(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {
        "task_id": task.task_id, "request_id": "compact",
    })["run"]
    result = engine_for(
        stack, ScriptedModel([ModelResponse("end_turn", content="  ")]),
    ).resume_run(compact["run_id"])
    assert result.state == "failed"
    following = start(plane, task, "execute", "following")
    model = finish(stack, following["run_id"])
    assert "已压缩上下文" not in model.inputs[0][0][1]["content"]


def test_direct_loop_start_also_reuses_compact(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {"task_id": task.task_id})["run"]
    finish(stack, compact["run_id"], SUMMARY)
    model = RecordingModel([ModelResponse("end_turn", content="继续完成")])
    result = engine_for(stack, model).start(task.task_id)
    assert SUMMARY in model.inputs[0][0][1]["content"]
    assert replay(stack.log, result.run_id).ok


def test_confirm_cannot_change_to_readonly_policy(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    finish(stack, plan["run_id"])
    with pytest.raises(ControlError):
        plane.handle("plan.execute", {
            "plan_run_id": plan["run_id"], "goal_revision": 1,
            "execution_policy": PURE_ONLY.to_dict(),
        })
    assert len(stack.tasks.list_runs(task.task_id)) == 1


@pytest.mark.parametrize("text", [PLAN, "", "超长" * 4097])
def test_compact_committed_response_recovery_never_calls_model_again(env, text):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {"task_id": task.task_id})["run"]
    _crash_workflow(stack, compact["run_id"], text, "workflow-after-result")
    model = RecordingModel([])
    result = engine_for(stack, model).resume_run(compact["run_id"])
    assert result.state == ("succeeded" if text == PLAN else "failed")
    assert model.inputs == []
    assert replay(stack.log, compact["run_id"]).ok


def _crash_workflow(stack, run_id, text, point, tool=False):
    root = Path(stack.tasks.data_root)
    cfg = root / "probe.json"
    cfg.write_text(json.dumps({
        "root": str(root), "run_id": run_id, "text": text, "tool": tool,
    }), encoding="utf-8")
    child = """
import json, sys
from pathlib import Path
from support import stack_for, engine_for
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(cfg["root"])
log = EventLog(root / "events.db", blob_dir=root / "blobs")
stack = stack_for(log, data_root=root)
response = (ModelResponse("tool_use", tool_calls=[
                ToolCall("bad", "read_text", {"path": "notes.md"})])
            if cfg["tool"] else ModelResponse("end_turn", content=cfg["text"]))
engine_for(stack, ScriptedModel([response])).resume_run(cfg["run_id"])
"""
    proc = subprocess.run(
        [sys.executable, "-c", child, str(cfg)], capture_output=True, text=True,
        encoding="utf-8", timeout=30,
        env={**os.environ, "TIANSHU_FAILPOINT": point,
             "PYTHONPATH": str(Path(__file__).parent) + os.pathsep
             + os.environ.get("PYTHONPATH", "")},
    )
    assert proc.returncode == 77, proc.stderr
    assert f"[failpoint] {point}" in proc.stderr


def test_unknown_compact_call_is_not_resent_after_crash(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {"task_id": task.task_id})["run"]
    _crash_workflow(stack, compact["run_id"], SUMMARY, "workflow-after-call")
    model = RecordingModel([])
    result = engine_for(stack, model).resume_run(compact["run_id"])
    assert result.state == "failed"
    assert "未知" in result.error
    assert result.model_attempts == 1
    assert len([e for e in stack.log.read_run(compact["run_id"])
                if e.type == "model.intent"]) == 1
    assert not any(e.type == "model.result" for e in stack.log.read_run(compact["run_id"]))
    assert model.inputs == []


def test_compact_tool_proposal_cannot_dispatch_after_crash(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {"task_id": task.task_id})["run"]
    _crash_workflow(stack, compact["run_id"], "", "workflow-after-result", tool=True)
    model = RecordingModel([])
    result = engine_for(stack, model).resume_run(compact["run_id"])
    assert result.state == "failed"
    assert model.inputs == []
    assert not any(e.type in ("tool.intent", "tool.result")
                   for e in stack.log.read_run(compact["run_id"]))


def _confirm_worker(root, plan_id, barrier, output, request_id):
    root = Path(root)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        plane = ControlPlane(stack, model=ScriptedModel([]))
        barrier.wait(timeout=30)
        run = plane.handle("plan.execute", {
            "plan_run_id": plan_id, "goal_revision": 1, "request_id": request_id,
        })["run"]
        output.put(("ok", run["run_id"]))
    except Exception as exc:
        output.put(("error", repr(exc)))
    finally:
        log.close()


def test_two_processes_confirm_same_plan_only_enqueue_once(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    finish(stack, plan["run_id"])
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    output = context.Queue()
    children = [context.Process(target=_confirm_worker, args=(
        str(stack.tasks.data_root), plan["run_id"], barrier, output, f"confirm-{i}",
    )) for i in range(2)]
    try:
        for child in children:
            child.start()
        barrier.wait(timeout=30)
        results = [output.get(timeout=30) for _ in children]
        for child in children:
            child.join(timeout=30)
            assert child.exitcode == 0
        assert all(item[0] == "ok" for item in results), results
        assert results[0][1] == results[1][1]
        assert len(stack.tasks.list_runs(task.task_id)) == 2
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
        output.close()


def test_older_plan_is_rejected_and_new_request_cannot_duplicate_confirmation(env):
    plane, stack, task, _ = env
    old = start(plane, task)
    finish(stack, old["run_id"])
    new = start(plane, task, request_id="new-plan")
    finish(stack, new["run_id"])
    with pytest.raises(ControlError):
        confirm(plane, task, old["run_id"])
    run = confirm(plane, task, new["run_id"])
    assert confirm(plane, task, new["run_id"], "different-id")["run_id"] == run["run_id"]


def test_enqueue_failure_rolls_back_workflow_and_run(env, monkeypatch):
    plane, stack, task, _ = env
    before = stack.log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    def reject(*args, **kwargs):
        raise RuntimeError("queue failed")
    monkeypatch.setattr(stack.claims, "enqueue", reject)
    with pytest.raises(RuntimeError, match="queue failed"):
        start(plane, task)
    assert stack.tasks.list_runs(task.task_id) == []
    assert stack.tasks.get_task(task.task_id).active_run_id is None
    assert stack.log.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_compact_can_be_repeated_and_replaced_goal_never_reuses_summary(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    for index in range(2):
        compact = plane.handle("run.compact", {
            "task_id": task.task_id, "request_id": f"compact-{index}",
        })["run"]
        finish(stack, compact["run_id"], SUMMARY)
    stack.tasks.revise_task(task.task_id, goal="完全不同的新目标")
    run = start(plane, task, "execute", "replacement")
    model = finish(stack, run["run_id"])
    assert SUMMARY not in model.inputs[0][0][1]["content"]


def test_workflow_missing_cursor_is_refused_without_reissuing_model_call(env):
    plane, stack, task, _ = env
    source = start(plane, task, "execute", "source")
    finish(stack, source["run_id"])
    compact = plane.handle("run.compact", {"task_id": task.task_id})["run"]
    _crash_workflow(stack, compact["run_id"], SUMMARY, "workflow-after-call")
    # 游标是派生投影，可以丢；历史事件保持 append-only，不伪造另一份结果。
    stack.log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (compact["run_id"],))
    model = RecordingModel([])
    with pytest.raises(Exception, match="游标缺失"):
        engine_for(stack, model).resume_run(compact["run_id"])
    assert model.inputs == []


def test_empty_plan_cannot_be_confirmed(env):
    plane, stack, task, _ = env
    plan = start(plane, task)
    result = engine_for(
        stack, ScriptedModel([ModelResponse("end_turn", content=" ")]),
    ).resume_run(plan["run_id"])
    assert result.state == "failed"
    with pytest.raises(ControlError):
        confirm(plane, task, plan["run_id"])


@pytest.mark.parametrize("response", [
    ModelResponse("end_turn", content=PLAN, tool_calls=[
        ToolCall("partial", "read_text", {"path": "notes.md"}),
    ]),
    ModelResponse("end_turn", content="过长的计划" * 16000),
])
def test_plan_without_a_confirmable_final_response_must_fail(env, response):
    plane, stack, task, _ = env
    plan = start(plane, task)
    result = engine_for(stack, ScriptedModel([response])).resume_run(plan["run_id"])
    assert result.state == "failed"
    with pytest.raises(ControlError):
        confirm(plane, task, plan["run_id"])


def test_workflow_modes_bound_to_real_event_schema():
    from tianshu.event_schema import schema_for
    from tianshu.workflow import WORKFLOW_MODES
    assert set(schema_for("run.workflow", 1)["properties"]["mode"]["enum"]) == set(WORKFLOW_MODES)


def test_later_undo_does_not_invalidate_frozen_compact_source(env):
    plane, stack, task, ws = env
    source = start(plane, task, "execute", "source-write")
    result = engine_for(stack, ScriptedModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("create", "create_report", {"name": "created.md", "content": "created"}),
        ]),
        ModelResponse("end_turn", content="已创建 created.md。"),
    ])).resume_run(source["run_id"])
    assert result.state == "succeeded"
    assert (ws / "created.md").exists()
    compact = plane.handle("run.compact", {
        "task_id": task.task_id, "source_run_id": source["run_id"],
        "request_id": "compact-written",
    })["run"]
    finish(stack, compact["run_id"], SUMMARY)
    assert replay(stack.log, compact["run_id"]).ok
    assert plane.handle("undo.apply", {"run_id": source["run_id"]})["applied"]
    assert not (ws / "created.md").exists()
    assert any(e.type == "undo.completed" for e in stack.log.read_run(source["run_id"]))
    assert replay(stack.log, compact["run_id"]).ok
    assert any(r["run_id"] == compact["run_id"] for r in plane.handle("state", {})["runs"])


def test_question_requires_human_answer_and_cannot_be_executed_as_plan(env):
    plane, stack, task, _ = env
    source = start(plane, task)
    finish(stack, source['run_id'], 'TIANSHU_QUESTION: 报告给哪类读者？')
    status = plane.handle('run.status', {'run_id': source['run_id']})['run']
    assert status['clarification'] == {'question': '报告给哪类读者？'}
    with pytest.raises(ControlError):
        confirm(plane, task, source['run_id'])
    with pytest.raises(ControlError):
        plane.handle('run.follow_up', {'task_id': task.task_id,
                                       'source_run_id': source['run_id'], 'message': '新同事'})
    params = {'task_id': task.task_id, 'source_run_id': source['run_id'],
              'message': '新同事', 'request_id': 'clarification-1'}
    run = plane.handle('run.answer_clarification', params)['run']
    assert plane.handle('run.answer_clarification', params)['run']['run_id'] == run['run_id']
    assert run['workflow']['mode'] == 'plan'
    assert stack.tasks.run_execution_policy(run['run_id']) == PURE_ONLY
    model = finish(stack, run['run_id'])
    assert model.inputs[0][0][-2:] == [
        {'role': 'assistant', 'content': 'TIANSHU_QUESTION: 报告给哪类读者？'},
        {'role': 'user', 'content': '新同事'},
    ]
    assert len(stack.tasks.list_runs(task.task_id)) == 2
    assert replay(stack.log, run['run_id']).ok
