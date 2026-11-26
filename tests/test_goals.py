"""Goal 的真实控制面、模型输入与事件证据；不把 Run 成功当作 Goal 完成。"""

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
from tianshu.run_policy import EFFECTS, PURE_ONLY


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    plane = ControlPlane(stack, model=ScriptedModel([]), limits=Limits(max_turns=4))
    task = stack.tasks.create_task("整理资料", [str(ws)])
    yield plane, stack, task, ws
    log.close()


def create(env, **changes):
    plane, _, task, _ = env
    return plane.handle("goal.create", {
        "task_id": task.task_id, "goal_revision": task.goal_revision,
        "criteria": ["报告使用中文", "包含来源"], "max_rounds": 2,
        "execution_policy": PURE_ONLY.to_dict(), "request_id": "create-1",
        **changes,
    })["goal"]


def advance(env, goal, request_id="advance-1"):
    return env[0].handle("goal.advance", {
        "goal_id": goal["goal_id"], "request_id": request_id,
    })


def finish(env, run, text="已完成本轮整理"):
    class RecordingModel(ScriptedModel):
        def complete(self, messages, tools):
            self.messages = copy.deepcopy(messages)
            return super().complete(messages, tools)

    model = RecordingModel([ModelResponse("end_turn", content=text)])
    result = engine_for(env[1], model).resume_run(run["run_id"])
    assert result.state == "succeeded"
    return model


def get(env):
    return env[0].handle("goal.get", {"task_id": env[2].task_id})["goal"]


def close(env, goal, **changes):
    return env[0].handle("goal.close", {
        "goal_id": goal["goal_id"], "request_id": "close-1",
        "outcome": "completed", "confirmed_criteria": [0, 1], **changes,
    })["goal"]


def test_create_persists_goal_without_starting_model_or_job(env):
    goal = create(env)
    assert goal["status"] == "ready"
    assert goal["rounds_started"] == 0
    assert goal["criteria"] == ["报告使用中文", "包含来源"]
    assert goal == get(env)
    assert env[1].tasks.list_runs(env[2].task_id) == []
    assert create(env) == goal
    with pytest.raises(ControlError) as error:
        create(env, criteria=["换一个条件"])
    assert error.value.code == "conflict"


def test_round_is_queued_once_and_success_does_not_complete_goal(env):
    goal = create(env)
    started = advance(env, goal)
    assert advance(env, goal)["run"]["run_id"] == started["run"]["run_id"]
    assert started["run"]["state"] == "created"
    assert started["goal"]["rounds_started"] == 1
    model = finish(env, started["run"], "全部完成")
    assert "报告使用中文" in model.messages[1]["content"]
    assert "包含来源" in model.messages[1]["content"]
    assert get(env)["status"] == "awaiting_review"
    assert replay(env[1].log, started["run"]["run_id"]).ok


def test_next_round_has_frozen_prior_result_and_maximum_rounds(env):
    goal = create(env)
    first = advance(env, goal)["run"]
    finish(env, first, "来源 A 尚待补充")
    second = advance(env, goal, "advance-2")["run"]
    model = finish(env, second)
    assert "来源 A 尚待补充" in model.messages[1]["content"]
    assert get(env)["status"] == "exhausted"
    with pytest.raises(ControlError) as error:
        advance(env, goal, "advance-3")
    assert error.value.code == "conflict"
    assert len(env[1].tasks.list_runs(env[2].task_id)) == 2
    assert replay(env[1].log, second["run_id"]).ok


def test_complete_requires_all_human_confirmations_and_successful_round(env):
    goal = create(env)
    with pytest.raises(ControlError):
        close(env, goal)
    run = advance(env, goal)["run"]
    with pytest.raises(ControlError):
        close(env, goal)
    finish(env, run)
    for indices in ([], [0], [0, 0, 1], [True, 0], [0, 1, 2]):
        with pytest.raises(ControlError):
            close(env, goal, confirmed_criteria=indices)
    result = close(env, goal)
    assert result["status"] == "completed"
    assert result["confirmed_criteria"] == [0, 1]
    assert close(env, goal) == result
    with pytest.raises(ControlError):
        advance(env, goal, "after-close")


def test_stale_goal_refuses_continuation_or_completion(env):
    goal = create(env)
    env[1].tasks.revise_task(env[2].task_id, goal="新目标")
    assert get(env)["status"] == "stale"
    with pytest.raises(ControlError):
        advance(env, goal)
    with pytest.raises(ControlError):
        close(env, goal)
    revised = env[1].tasks.get_task(env[2].task_id)
    new = create(env, goal_revision=revised.goal_revision, request_id="create-new")
    assert new["goal_id"] != goal["goal_id"]


@pytest.mark.parametrize("changes", [
    {"criteria": []}, {"criteria": [""]}, {"criteria": ["x" * 1025]},
    {"criteria": ["x"] * 13}, {"criteria": "text"},
    {"max_rounds": True}, {"max_rounds": 1.0}, {"max_rounds": 0}, {"max_rounds": 11},
    {"request_id": ""}, {"request_id": "有空 格"}, {"goal_revision": True},
    {"execution_policy": {"format_version": 1, "mode": "unlimited"}},
])
def test_bad_configuration_creates_no_events_or_runs(env, changes):
    head = env[1].log.seq_head()
    with pytest.raises(ControlError) as error:
        create(env, **changes)
    assert error.value.code in ("bad_params", "conflict")
    assert env[1].log.seq_head() == head


def test_failure_or_empty_output_blocks_advancement_and_completion(env):
    goal = create(env)
    run = advance(env, goal)["run"]
    finish(env, run, "   ")
    assert get(env)["status"] == "blocked"
    with pytest.raises(ControlError):
        advance(env, goal, "advance-2")
    with pytest.raises(ControlError):
        close(env, goal)


def test_goal_keeps_pure_only_execution_enforcement(env):
    goal = create(env)
    run = advance(env, goal)["run"]
    model = ScriptedModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("write", "create_file", {"path": "bad.txt", "content": "bad"}),
        ]),
        ModelResponse("end_turn", content="本轮只读"),
    ])
    engine_for(env[1], model).resume_run(run["run_id"])
    assert not (env[3] / "bad.txt").exists()
    assert not any(e.type == "tool.intent" for e in env[1].log.read_run(run["run_id"]))


def test_cancel_does_not_complete_goal_and_reopen_rebuilds_view(env):
    goal = create(env)
    result = close(env, goal, outcome="cancelled", confirmed_criteria=[])
    assert result["status"] == "cancelled"
    plane = ControlPlane(env[1], model=ScriptedModel([]))
    assert plane.handle("goal.get", {"task_id": env[2].task_id})["goal"] == result


def test_reopen_database_and_rebuild_keep_goal_evidence(env):
    goal = create(env)
    started = advance(env, goal)
    finish(env, started["run"])
    expected = get(env)
    root = env[1].tasks.data_root
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        stack.tasks.rebuild_from_events()
        plane = ControlPlane(stack, model=ScriptedModel([]))
        assert plane.handle("goal.get", {"task_id": env[2].task_id})["goal"] == expected
        assert replay(log, started["run"]["run_id"]).ok
    finally:
        log.close()


def test_unknown_effect_blocks_review_and_continuation(env):
    goal = create(env)
    started = advance(env, goal)
    finish(env, started["run"])
    env[1].tasks.note_effect(started["run"]["run_id"], effect_state="unknown", n_unknown=1)
    assert get(env)["status"] == "blocked"
    with pytest.raises(ControlError):
        advance(env, goal, "next")
    with pytest.raises(ControlError):
        close(env, goal)


@pytest.mark.parametrize("action", ["advance", "close"])
def test_partial_real_mcp_effects_block_review_and_continuation(tmp_path, action):
    """已生效 + 超时未知的真实工具结果不能被 Goal 当成可验收轮次。"""
    from support import approve_pending

    from tianshu.loop import build_stack
    from tianshu.mcp import McpClients, McpServerConfig

    fixture = Path(__file__).parent / "fixtures/fake_mcp_server.py"
    clients = McpClients({
        "fast": McpServerConfig(name="fast", command=(sys.executable, str(fixture))),
        "slow": McpServerConfig(
            name="slow", command=(sys.executable, str(fixture), "--mode", "hang_on_call"),
            call_timeout_ms=200,
        ),
    })
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    try:
        stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
        ws = tmp_path / "workspace"
        ws.mkdir()
        task = stack.tasks.create_task("核对 MCP 结果", [str(ws)], mcp_servers=["fast", "slow"])
        plane = ControlPlane(stack, model=ScriptedModel([]))
        env = (plane, stack, task, ws)
        goal = create(env, execution_policy=EFFECTS.to_dict())
        run = advance(env, goal)["run"]
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[
                ToolCall("fast-call", "mcp__fast__echo", {"message": "hi"}),
            ]),
            ModelResponse("tool_use", tool_calls=[
                ToolCall("slow-call", "mcp__slow__echo", {"message": "hi"}),
            ]),
            ModelResponse("end_turn", content="一项已执行，一项结果未知。"),
        ])
        engine = engine_for(stack, model)
        result = engine.resume_run(run["run_id"])
        for _ in range(3):
            if result.state != "waiting_approval":
                break
            assert approve_pending(stack, run["run_id"])
            result = engine.resume_run(run["run_id"])
        assert result.state == "succeeded"
        assert result.effect_state == "partial"
        effects = [e.payload["effect_state"] for e in log.read_run(run["run_id"])
                   if e.type == "tool.result"]
        assert effects == ["applied", "unknown"]
        head = log.seq_head()
        with pytest.raises(ControlError) as error:
            advance(env, goal, "unsafe-next") if action == "advance" else close(env, goal)
        assert error.value.code == "conflict"
        assert log.seq_head() == head
        assert get(env)["status"] == "blocked"
    finally:
        clients.close_all()
        log.close()


def test_queue_failure_rolls_back_round_run_and_event(env, monkeypatch):
    goal = create(env)
    head = env[1].log.seq_head()

    def fail(*args, **kwargs):
        raise OSError("injected queue failure")

    with monkeypatch.context() as m:
        m.setattr(env[1].claims, "enqueue", fail)
        with pytest.raises(ControlError):
            advance(env, goal)
    assert env[1].log.seq_head() == head
    assert env[1].tasks.list_runs(env[2].task_id) == []
    assert advance(env, goal)["goal"]["rounds_started"] == 1


def _advance_process(root, goal_id, barrier, output, rid):
    root = Path(root)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        plane = ControlPlane(stack_for(log, data_root=root), model=ScriptedModel([]))
        barrier.wait(timeout=30)
        try:
            result = plane.handle("goal.advance", {"goal_id": goal_id, "request_id": rid})
            output.put(("ok", result["run"]["run_id"]))
        except ControlError as exc:
            output.put((exc.code, None))
    finally:
        log.close()


@pytest.mark.parametrize("same_request", [True, False])
def test_two_processes_only_start_one_round(env, same_request):
    goal = create(env)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(3), ctx.Queue()
    children = [ctx.Process(target=_advance_process, args=(
        str(env[1].tasks.data_root), goal["goal_id"], barrier, output,
        "concurrent" if same_request else f"concurrent-{i}",
    )) for i in range(2)]
    try:
        for child in children:
            child.start()
        barrier.wait(timeout=30)
        results = [output.get(timeout=30) for _ in children]
        for child in children:
            child.join(timeout=30)
            assert child.exitcode == 0
        if same_request:
            assert all(r[0] == "ok" for r in results), results
            assert results[0][1] == results[1][1]
        else:
            assert sorted(r[0] for r in results) == ["conflict", "ok"]
        assert len(env[1].tasks.list_runs(env[2].task_id)) == 1
        assert get(env)["rounds_started"] == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=10)
        output.close()


@pytest.mark.parametrize("point", ["goal-after-call", "goal-after-result"])
def test_crash_does_not_resend_goal_model_call(env, point):
    from test_workflow import _crash_workflow

    goal = create(env)
    started = advance(env, goal)
    _crash_workflow(env[1], started["run"]["run_id"], "已提交最终结果", point)
    model = ScriptedModel([])
    result = engine_for(env[1], model).resume_run(started["run"]["run_id"])
    assert result.state == ("failed" if point == "goal-after-call" else "succeeded")
    events = env[1].log.read_run(started["run"]["run_id"])
    assert len([e for e in events if e.type == "model.intent"]) == 1
    if point == "goal-after-result":
        assert get(env)["status"] == "awaiting_review"
        assert replay(env[1].log, started["run"]["run_id"]).ok
    else:
        assert get(env)["status"] == "blocked"
        assert "未知" in result.error


@pytest.mark.parametrize("scenario,expected", [("veto", "failed"), ("children", "running")])
@pytest.mark.parametrize("crash", [False, True])
def test_recovered_final_preserves_veto_and_waits_for_children(env, scenario, expected, crash):
    """最终响应恢复不能把已否决或仍有未完成子任务的轮次变成人工可验收。"""
    task = env[1].tasks.create_task("核对资料", [str(env[3])], max_subtasks=2)
    env = (env[0], env[1], task, env[3])
    (env[3] / "notes.md").write_text("original\n", encoding="utf-8")
    goal = create(env, execution_policy=EFFECTS.to_dict())
    run = advance(env, goal)["run"]
    script = r"""
import os, sys
from pathlib import Path
from support import stack_for, engine_for
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import HumanIdentity
root, run_id, scenario, crash = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
log = EventLog(root / "events.db", blob_dir=root / "blobs")
stack = stack_for(log, data_root=root)
class FinalModel(ScriptedModel):
    def complete(self, messages, tools):
        response = super().complete(messages, tools)
        if response.finish_reason == "end_turn" and crash == "yes":
            os.environ["TIANSHU_FAILPOINT"] = "goal-after-result"
        return response
call = (
    ToolCall("write", "modify_text", {"path": "notes.md", "content": "changed"})
    if scenario == "veto" else
    ToolCall("child", "subtask.spawn", {"subtask_id": "reader", "goal": "读取 notes.md"})
)
model = FinalModel([
    ModelResponse("tool_use", tool_calls=[call]),
    ModelResponse("end_turn", content="本轮已有最终正文"),
])
engine = engine_for(stack, model)
assert engine.resume_run(run_id).state == "waiting_approval"
pending = stack.approvals.pending(run_id=run_id)
assert len(pending) == 1
stack.approvals.decide(
    pending[0].approval_id,
    decision="rejected" if scenario == "veto" else "approved",
    actor=HumanIdentity.local_cli(),
)
engine.resume_run(run_id)
log.close()
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(env[1].tasks.data_root), run["run_id"],
         scenario, "yes" if crash else "no"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        env={
            **{k: v for k, v in os.environ.items() if k != "TIANSHU_FAILPOINT"},
            "PYTHONPATH": str(Path(__file__).parent) + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
    )
    assert child.returncode == (77 if crash else 0), child.stderr
    if crash:
        assert "[failpoint] goal-after-result" in child.stderr
        # 无可用响应：恢复若重问模型会失败，必须消费已提交的最终正文。
        result = engine_for(env[1], ScriptedModel([])).resume_run(run["run_id"])
        assert result.state == expected
    assert env[1].tasks.get_run(run["run_id"]).state == expected
    assert get(env)["status"] == ("blocked" if scenario == "veto" else "running")
    assert len([e for e in env[1].log.read_run(run["run_id"])
                if e.type == "model.intent"]) == 2
    with pytest.raises(ControlError):
        close(env, goal)
    if scenario == "children":
        assert env[1].tasks.count_unfinished_subtasks(run["run_id"]) == 1
    else:
        assert (env[3] / "notes.md").read_text(encoding="utf-8") == "original\n"


def test_new_goal_events_do_not_accept_fake_legacy_payload(env):
    from tianshu import event_schema
    for name in ("goal.created", "goal.round_started", "goal.closed"):
        assert event_schema.known_versions(name) == frozenset({1})
        with pytest.raises(event_schema.PayloadContractError):
            env[1].log.append("", name, {}, legacy_write=True)


def test_goal_view_status_mirrors_closed_schema():
    from tianshu.goals import GOAL_STATUSES
    schema = json.loads((Path(__file__).parents[1] / "schemas/goal.json").read_text())
    assert set(schema["properties"]["status"]["enum"]) == set(GOAL_STATUSES)
