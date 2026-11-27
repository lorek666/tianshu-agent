"""Mission 的真实入口、不可变输入与执行侧路由；历史 Run 保持原解释。"""

from __future__ import annotations

import copy
import json
import queue
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.control import ControlError, ControlPlane
from tianshu.daemon import _Daemon
from tianshu.errors import TianshuError
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.replay import replay
from tianshu.restore_access import default_material_paths
from tianshu.run_policy import PURE_ONLY
from tianshu.tool_registry import ToolRegistry
from tianshu.tools import SPECS
from tianshu.worker import Worker


class RecordingModel(ScriptedModel):
    def __init__(self, responses=None):
        super().__init__(responses or [ModelResponse("end_turn", content="已完成文字答复。")])
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return super().complete(messages, tools)


@pytest.fixture
def env(tmp_path):
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    stack = stack_for(log, data_root=tmp_path)
    task = stack.tasks.create_task("解释这段观点，不需要文件工作区。")
    yield ControlPlane(stack, model=ScriptedModel([])), stack, task
    log.close()


def test_new_direct_run_freezes_generic_input_before_first_intent(env):
    _, stack, task = env
    model = RecordingModel()
    result = engine_for(stack, model).start(task.task_id)
    events = stack.log.read_run(result.run_id)
    facts = [e for e in events if e.type == "run.mission"]
    assert len(facts) == 1
    assert facts[0].seq < next(e.seq for e in events if e.type == "model.intent")
    assert "Markdown 与纯文本" not in model.requests[0][0][0]["content"]
    assert replay(stack.log, result.run_id).ok


def test_desktop_freezes_attachments_and_conversation_with_template(env, monkeypatch):
    from tianshu import mission

    plane, stack, task = env
    run_id = plane.handle("run.start", {
        "task_id": task.task_id, "attachments": [{
            "name": "note.txt", "media_type": "text/plain", "data_base64": "aGVsbG8=",
        }],
    })["run"]["run_id"]
    initial = mission.mission_messages(stack.log, run_id)
    monkeypatch.setattr(mission, "SYSTEM_PROMPT", "changed after the run was created")
    model = RecordingModel()
    engine_for(stack, model).resume_run(run_id)
    assert model.requests[0][0] == initial
    assert initial[-1]["attachments"][0]["data_base64"] == "aGVsbG8="
    assert replay(stack.log, run_id).ok


def test_historical_run_without_mission_keeps_legacy_prompt(env):
    from tianshu.loop import SYSTEM_PROMPT

    _, stack, task = env
    run = stack.tasks.start_run(task.task_id, payload={"task": task.goal, "out": ""})
    stack.claims.enqueue(run.run_id)
    model = RecordingModel()
    engine_for(stack, model).resume_run(run.run_id)
    assert not any(e.type == "run.mission" for e in stack.log.read_run(run.run_id))
    assert model.requests[0][0][0]["content"] == SYSTEM_PROMPT
    assert replay(stack.log, run.run_id).ok


def test_missing_snapshot_blob_blocks_resume_even_with_saved_cursor(env):
    from tianshu.mission import mission_for

    plane, stack, task = env
    rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
    assert mission_for(stack.log, rid) is not None
    class PausingModel(RecordingModel):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            stack.tasks.request_pause(rid)
            return response

    assert engine_for(stack, PausingModel()).resume_run(rid).state == "paused"
    assert stack.tasks.load_cursor(rid) is not None
    event = next(e for e in stack.log.read_run(rid) if e.type == "run.mission")
    digest = event.payload["messages_ref"].split(":")[-1]
    (stack.log.blob_dir / digest).unlink()
    model = RecordingModel()
    with pytest.raises(TianshuError, match="Mission|使命|快照"):
        engine_for(stack, model).resume_run(rid)
    assert model.requests == []
    assert not replay(stack.log, rid).ok


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("entry", ["engine", "control", "daemon"])
def test_chat_resume_without_cursor_does_not_send_or_claim_again(
    env, legacy, entry, tmp_path, capsys,
):
    plane, stack, task = env
    if legacy:
        from tianshu.workflow import prepare_workflow

        workflow = prepare_workflow(
            stack.tasks, stack.log, task, mode="execute", request_id="legacy-chat",
        )
        run_id = stack.tasks.start_run(
            task.task_id, payload={"task": task.goal, "out": ""}, workflow=workflow,
        ).run_id
        stack.claims.enqueue(run_id)
    else:
        run_id = plane.handle("run.start", {
            "task_id": task.task_id, "mode": "execute", "request_id": "chat-resume",
        })["run"]["run_id"]

    class PausingModel(RecordingModel):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            stack.tasks.request_pause(run_id)
            return response

    with Worker(stack, model=PausingModel(), heartbeat_interval_ms=None) as worker:
        assert worker.run_once()
        assert not worker.errors
    original_job = stack.claims.job_for_run(run_id)
    original_run = stack.tasks.get_run(run_id)
    assert original_job.state == "waiting"
    assert original_run.state == "paused"
    assert stack.tasks.load_cursor(run_id) is not None
    with stack.log.transaction():
        stack.log.conn.execute("DELETE FROM run_cursors WHERE run_id = ?", (run_id,))
    before = stack.log.read_run(run_id)
    assert [event.type for event in before].count("model.intent") == 1
    model = RecordingModel()
    engine = engine_for(stack, model)
    requests = queue.SimpleQueue()
    desktop = ControlPlane(stack, model=model, resume_requests=requests)
    guidance = "游标缺失.*核对.*取消并另开"
    if entry == "engine":
        with pytest.raises(TianshuError, match=guidance):
            engine.resume_run(run_id)
    elif entry == "control":
        with pytest.raises(ControlError, match=guidance) as rejected:
            desktop.handle("run.resume", {"run_id": run_id})
        assert rejected.value.code == "conflict"
        assert rejected.value.details.get("reason") == "chat_resume_cursor_missing"
        assert requests.empty()
    else:
        daemon = _Daemon(tmp_path, model=model, limits=None, driver_interval_s=0,
                         stop_event=threading.Event(), on_ready=None)
        daemon.driver_stack, daemon.driver_log, daemon.plane = stack, stack.log, desktop
        try:
            daemon._resume_one(engine, run_id)
        finally:
            daemon._close_resume_channel()
        assert "游标缺失" in capsys.readouterr().err

    assert not model.requests
    assert stack.log.read_run(run_id) == before
    assert stack.claims.job_for_run(run_id) == original_job
    assert stack.tasks.get_run(run_id) == original_run


@pytest.mark.parametrize(("point", "action"), [
    ("model-result-before-commit", "cancel"),
    ("after_intent", "cancel"),
    ("after_intent", "resume"),
    ("after_intent", "recover_cancel"),
    ("after_write", "recover_unknown"),
    ("after_receipt", "cancel"),
    ("after_receipt", "resume"),
])
def test_worker_holds_crashed_chat_without_cursor(tmp_path, point, action):
    db_path = tmp_path / "db"
    child = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from pathlib import Path

from tianshu.control import ControlPlane
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.restore_access import default_material_paths
from tianshu.worker import Worker

db_path = Path(sys.argv[1])
root = db_path.parent
point = sys.argv[2]
os.environ["TIANSHU_FAILPOINT"] = point
log = EventLog(db_path, blob_dir=default_material_paths(db_path, root)[1])
stack = build_stack(log, data_root=root)
if point == "model-result-before-commit":
    task = stack.tasks.create_task("解释观点")
    model = ScriptedModel([ModelResponse("end_turn", content="第一次回答")])
    out_name = ""
else:
    from tianshu.cli import _scripted_model

    workspace = root / "workspace"
    workspace.mkdir()
    task = stack.tasks.create_task("写报告", [str(workspace)])
    out_name = "r.md"
    model = _scripted_model(out_name)
ControlPlane(stack, model=model).handle("run.start", {
    "task_id": task.task_id, "mode": "execute", "request_id": "chat-cursor",
    "out_name": out_name,
})
Worker(stack, model=model, heartbeat_interval_ms=None).run_once()
""", str(db_path), point],
        capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 77 and f"[failpoint] {point}" in child.stderr, child.stderr
    with closing(EventLog(db_path, blob_dir=default_material_paths(db_path, tmp_path)[1])) as log:
        stack = stack_for(log, data_root=tmp_path)
        (run_id,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        original_run = stack.tasks.get_run(run_id)
        assert original_run.state == "running"
        if point in ("after_intent", "after_write", "after_receipt"):
            assert stack.tasks.load_cursor(run_id) is not None
            with log.transaction():
                log.conn.execute("DELETE FROM run_cursors WHERE run_id = ?", (run_id,))
        assert stack.tasks.load_cursor(run_id) is None
        effect_events = ("model.intent", "model.result", "tool.intent", "tool.result", "run.end")
        before = log.read_run(run_id, event_types=effect_events)
        if point in ("after_intent", "after_write"):
            assert before[-1].type == "tool.intent"
            assert before[-1].payload["tool_id"] == "create_report"
        elif point == "after_receipt":
            assert before[-1].type == "tool.result"
            assert before[-1].payload["effect_state"] == "applied"
        else:
            assert [event.type for event in before] == ["model.intent"]
        receipts = log.read_run(run_id, event_types=("receipt",))
        leases = stack.leases.list_for_run(run_id)
        target = tmp_path / "workspace" / "r.md"
        original_bytes = target.read_bytes() if point == "after_receipt" else None
        assert len(receipts) == (1 if point == "after_receipt" else 0)

        model = RecordingModel()
        with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
            assert worker.run_once()
            assert not worker.run_once()
            assert any("游标缺失" in error for error in worker.errors)
        assert model.requests == []
        assert log.read_run(run_id, event_types=effect_events) == before
        assert stack.claims.job_for_run(run_id).state == "waiting"
        if point in ("after_intent", "after_write"):
            assert stack.tasks.get_run(run_id) == original_run
            assert stack.leases.list_for_run(run_id) == leases
        if point == "model-result-before-commit":
            assert stack.tasks.get_run(run_id).state == "reconciling"
        requests = queue.SimpleQueue()
        plane = ControlPlane(stack, model=model, resume_requests=requests)
        needs_recovery = (
            point in ("after_intent", "after_write") and action != "recover_cancel"
        )
        if action in ("recover_cancel", "recover_unknown"):
            from tianshu.cli import main

            if action == "recover_unknown":
                original_bytes = b"changed outside the run\n"
                target.write_bytes(original_bytes)
            status = main([
                "recover", "--run-id", run_id, "--workspace", str(target.parent),
                "--db", str(db_path), "--data-root", str(tmp_path),
            ])
            assert (status == 0) == (action == "recover_cancel")
            action = "cancel"
        parked_job = stack.claims.job_for_run(run_id)
        parked_events = log.read_run(run_id)
        locked_leases = stack.leases.list_for_run(run_id)
        if action == "resume" or needs_recovery:
            with pytest.raises(ControlError) as exc:
                plane.handle(f"run.{action}", {"run_id": run_id})
            assert exc.value.code == "conflict"
            assert exc.value.details == {
                "reason": "chat_resume_cursor_missing",
                "recovery_required": needs_recovery,
            }
            if needs_recovery:
                assert "recover" in str(exc.value)
            assert stack.leases.list_for_run(run_id) == locked_leases
            assert stack.claims.job_for_run(run_id) == parked_job
            assert log.read_run(run_id) == parked_events
        else:
            cancelled = plane.handle("run.cancel", {"run_id": run_id})["run"]
            assert cancelled["state"] == "cancelled"
            assert cancelled["control_requested"] is None
            assert stack.claims.job_for_run(run_id).state == "cancelled"
            end, = log.read_run(run_id, event_types=("run.end",))
            expected_effect = "applied" if point == "after_receipt" else "none_applied"
            assert end.payload["effect_state"] == expected_effect
            assert cancelled["effect_state"] == expected_effect
            assert end.payload["receipt_ids"] == [event.payload["receipt_id"] for event in receipts]
            model_results = [event for event in before if event.type == "model.result"]
            assert end.payload["turns"] == len(model_results)
            assert end.payload["tokens_unknown_calls"] == len(model_results)
            assert end.payload["cost_unknown_calls"] == len(model_results)
            assert end.payload["cost_total_micros"] is None
            assert end.payload["budget_enforced"] is False
        if original_bytes is None:
            assert not target.exists()
        else:
            assert target.read_bytes() == original_bytes
        assert requests.empty()
        assert not model.requests


def test_research_route_cannot_dispatch_forged_write(env, tmp_path):
    from tianshu.mission import mission_for

    plane, stack, task = env
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = stack.tasks.create_task("整理研究资料", [str(workspace)])
    rid = plane.handle("run.start", {
        "task_id": task.task_id, "task_kind": "research",
    })["run"]["run_id"]
    model = RecordingModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("fake", "create_report", {"name": "bad.md", "content": "bad"}),
        ]),
        ModelResponse("end_turn", content="已报告不能执行此工具。"),
    ])
    engine_for(stack, model).resume_run(rid)
    assert mission_for(stack.log, rid).task_kind == "research"
    assert "create_report" not in {t["name"] for t in model.requests[0][1]}
    assert not (workspace / "bad.md").exists()
    events = stack.log.read_run(rid)
    assert not any(e.type == "tool.intent" for e in events)
    assert any(e.type == "tool.not_dispatched" and "Mission" in e.payload["reason"] for e in events)
    assert replay(stack.log, rid).ok


def test_reused_mission_keeps_identity_but_intersects_each_run_policy(env):
    from tianshu.mission import freeze_mission, mission_for, prepare_mission

    _, stack, task = env
    spec = prepare_mission(stack.tasks, task.task_id, criteria=["由人确认答复内容"])
    assert spec.to_dict()["criteria"] == [{
        "criterion_id": "criterion_1", "description": "由人确认答复内容", "mode": "human",
    }]
    run = stack.tasks.start_run(task.task_id, payload={"task": task.goal, "out": ""},
                                execution_policy=PURE_ONLY)
    assert freeze_mission(stack.log, run.run_id, mission=spec).digest == spec.digest
    assert freeze_mission(stack.log, run.run_id, mission=spec).digest == spec.digest
    assert mission_for(stack.log, run.run_id).mission_id == spec.mission_id
    event = next(e for e in stack.log.read_run(run.run_id) if e.type == "run.mission")
    assert all(SPECS[t["tool_id"]].side_effect_level == "pure"
               for t in event.payload["effective_tools"])
    assert any(SPECS[t["tool_id"]].side_effect_level != "pure" for t in spec.tool_allowlist)
    assert len([e for e in stack.log.read_run(run.run_id) if e.type == "run.mission"]) == 1


def test_shared_mission_keeps_distinct_stage_text_across_runs(env):
    from tianshu.mission import freeze_mission, mission_for, prepare_mission

    _, stack, task = env
    spec = prepare_mission(stack.tasks, task.task_id, criteria=["由人确认结果"])
    for phase in ("提出计划", "执行确认的步骤", "核查结果"):
        run = stack.tasks.start_run(
            task.task_id, payload={"task": phase, "out": ""}, execution_policy=PURE_ONLY,
        )
        freeze_mission(stack.log, run.run_id, mission=spec)
        stack.claims.enqueue(run.run_id)
        model = RecordingModel()
        assert engine_for(stack, model).resume_run(run.run_id).terminal
        assert model.requests[0][0][1]["content"].startswith(phase)
        assert mission_for(stack.log, run.run_id).digest == spec.digest
        assert replay(stack.log, run.run_id).ok


def test_mission_rejects_changed_target_and_closed_fields(env):
    from tianshu.mission import MissionSpecV1, freeze_mission, prepare_mission

    _, stack, task = env
    spec = prepare_mission(stack.tasks, task.task_id)
    for changed in ({"task_kind": "unknown"}, {"extra": True}, {"format_version": True}):
        with pytest.raises(TianshuError):
            MissionSpecV1.of({**spec.to_dict(), **changed})
    with pytest.raises(TianshuError, match="条件|criterion"):
        prepare_mission(stack.tasks, task.task_id, criteria=[
            {"criterion_id": "same", "description": "一", "mode": "human"},
            {"criterion_id": "same", "description": "二", "mode": "human"},
        ])
    stack.tasks.revise_task(task.task_id, goal="不同目标")
    run = stack.tasks.start_run(task.task_id, payload={"task": "不同目标", "out": ""})
    with pytest.raises(TianshuError, match="修订|目标|绑定"):
        freeze_mission(stack.log, run.run_id, mission=spec)
    assert not any(e.type == "run.mission" for e in stack.log.read_run(run.run_id))


def test_same_mission_identity_cannot_name_different_content_across_runs(env):
    from tianshu.mission import MissionSpecV1, freeze_mission, prepare_mission

    _, stack, task = env
    spec = prepare_mission(stack.tasks, task.task_id)
    first = engine_for(stack, RecordingModel()).start(task.task_id, mission=spec)
    altered = spec.to_dict()
    altered["criteria"] = [{"criterion_id": "new", "description": "新要求", "mode": "human"}]
    run = stack.tasks.start_run(task.task_id, payload={"task": task.goal, "out": ""})
    with pytest.raises(TianshuError, match="Mission|身份"):
        freeze_mission(stack.log, run.run_id, mission=MissionSpecV1.of(altered))
    assert replay(stack.log, first.run_id).ok


def test_new_phase_rejects_changed_profile_renderer(env, monkeypatch):
    from tianshu import mission

    _, stack, task = env
    spec = mission.prepare_mission(stack.tasks, task.task_id, task_kind="coding")
    monkeypatch.setitem(mission._METHODS, "coding", "a different method")
    run = stack.tasks.start_run(task.task_id, payload={"task": task.goal, "out": ""})
    with pytest.raises(TianshuError, match="配置|profile|模板"):
        mission.freeze_mission(stack.log, run.run_id, mission=spec)


def test_fixed_tool_identity_intersection_rejects_registry_drift(env):
    from tianshu.mission import effective_manifest, freeze_mission, prepare_mission

    _, stack, task = env
    spec = prepare_mission(stack.tasks, task.task_id, tool_ids=["read_text"])
    run = stack.tasks.start_run(task.task_id, payload={"task": task.goal, "out": ""})
    freeze_mission(stack.log, run.run_id, mission=spec)
    registry = ToolRegistry(base={**SPECS, "read_text": replace(
        SPECS["read_text"], description="changed description",
    )})
    assert effective_manifest(stack.log, run.run_id, registry, registry.manifest()) == []


def test_request_retry_cannot_silently_change_mission_kind(env):
    plane, _, task = env
    params = {"task_id": task.task_id, "request_id": "kind", "task_kind": "research"}
    plane.handle("run.start", params)
    with pytest.raises(ControlError, match="Mission|使命|request_id"):
        plane.handle("run.start", {**params, "task_kind": "coding"})


def test_request_retry_cannot_silently_change_mission_tools(env):
    plane, stack, task = env
    params = {"task_id": task.task_id, "request_id": "tools", "tool_ids": ["read_text"]}
    rid = plane.handle("run.start", params)["run"]["run_id"]
    with pytest.raises(ControlError, match="Mission|使命|request_id"):
        plane.handle("run.start", {**params, "tool_ids": ["create_report"]})
    assert len([e for e in stack.log.read_run(rid) if e.type == "run.mission"]) == 1


@pytest.mark.parametrize("invalid", [[], "not-a-mission", 1])
def test_invalid_mission_input_is_rejected_without_partial_run(env, invalid):
    plane, stack, task = env
    before = stack.log.read_after(0)
    with pytest.raises(ControlError, match="Mission"):
        plane.handle("run.start", {"task_id": task.task_id, "mission": invalid})
    assert stack.log.read_after(0) == before


def test_cli_start_freezes_selected_mission(tmp_path, monkeypatch, capsys):
    from tianshu import cli

    model = RecordingModel()
    monkeypatch.setattr(cli, "_model_from", lambda args: model)
    db = tmp_path / "events.db"
    base = ["--db", str(db), "--data-root", str(tmp_path), "--json"]
    assert cli.main(["task", "create", "--goal", "研究概念", *base]) == 0
    task = json.loads(capsys.readouterr().out)["task"]["task_id"]
    assert cli.main(["run", "start", "--task-id", task, "--task-kind", "research", *base]) == 0
    rid = json.loads(capsys.readouterr().out)["run"]["run_id"]
    from tianshu.restore_access import default_material_paths

    with closing(EventLog(db, blob_dir=default_material_paths(db, tmp_path)[1])) as log:
        fact = next(e for e in log.read_run(rid) if e.type == "run.mission")
        assert fact.payload["mission"]["task_kind"] == "research"
        assert replay(log, rid).ok


def test_goal_g1_round_freezes_human_criteria(env):
    from tianshu.mission import mission_for

    plane, stack, task = env
    goal = plane.handle("goal.create", {
        "task_id": task.task_id, "goal_revision": 1, "criteria": ["人确认内容"],
        "max_rounds": 2, "execution_policy": PURE_ONLY.to_dict(), "request_id": "goal",
    })["goal"]
    rid = plane.handle("goal.advance", {
        "goal_id": goal["goal_id"], "request_id": "round",
    })["run"]["run_id"]
    spec = mission_for(stack.log, rid)
    assert spec.user_goal == task.goal
    assert spec.to_dict()["criteria"][0]["mode"] == "human"
    model = RecordingModel()
    engine_for(stack, model).resume_run(rid)
    assert "人确认内容" in model.requests[0][0][1]["content"]
    assert replay(stack.log, rid).ok


def test_spawned_child_freezes_generic_mission_before_worker_execution(env):
    from tianshu.mission import mission_for

    _, stack, _ = env
    task = stack.tasks.create_task("调查观点，再整合子任务结果。", max_subtasks=1)
    parent = engine_for(stack, RecordingModel([
        ModelResponse("tool_use", tool_calls=[ToolCall(
            "spawn-reader", "subtask.spawn",
            {"subtask_id": "reader", "goal": "解释观点中的因果关系。"},
        )]),
        ModelResponse("end_turn", content="等待子任务结果。"),
    ]))
    result = parent.start(task.task_id)
    assert result.state == "waiting_approval"
    assert approve_pending(stack, result.run_id)
    assert parent.resume_run(result.run_id).state == "running"
    (child,) = stack.tasks.list_subtasks(result.run_id)
    spec = mission_for(stack.log, child.child_run_id)
    assert spec is not None
    assert spec.task_kind == "general"
    assert spec.user_goal == "解释观点中的因果关系。"
    facts = [e for e in stack.log.read_run(child.child_run_id) if e.type == "run.mission"]
    assert len(facts) == 1
    assert not any(e.type == "model.intent" for e in stack.log.read_run(child.child_run_id))
    model = RecordingModel()
    assert engine_for(stack, model).resume_run(child.child_run_id).state == "succeeded"
    assert "Markdown 与纯文本" not in model.requests[0][0][0]["content"]
    assert model.requests[0][0][1]["content"].startswith(spec.user_goal)
    assert replay(stack.log, child.child_run_id).ok


def test_schema_matches_versioned_mission_contract():
    from tianshu.mission import MISSION_SCHEMA, TASK_KINDS

    schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/mission.json").read_text())
    assert schema.get("$ref") == "#/$defs/mission"
    assert schema["$defs"]["mission"] == MISSION_SCHEMA
    assert set(MISSION_SCHEMA["properties"]["task_kind"]["enum"]) == set(TASK_KINDS)
