"""Mission 与实际 MCP 发现的生产入口接缝，schema 身份不借用 Session generation。"""

from __future__ import annotations

import copy
import json
import sys
from contextlib import closing
from pathlib import Path

import pytest
from support import approve_pending, engine_for

from tianshu.control import ControlError, ControlPlane
from tianshu.events import EventLog
from tianshu.exec import LOCAL_EXEC_BACKEND
from tianshu.loop import build_stack
from tianshu.mcp import McpClients, McpServerConfig
from tianshu.mission import MissionSpecV1, effective_manifest, freeze_mission, prepare_mission
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.run_policy import EFFECTS, PURE_ONLY
from tianshu.tool_registry import build_registry

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
TOOL = {
    "name": "echo", "description": "echo",
    "inputSchema": {
        "type": "object", "properties": {"message": {"type": "string"}},
        "required": ["message"], "additionalProperties": False,
    },
}


class RecordingModel(ScriptedModel):
    def __init__(self, responses=None):
        super().__init__(responses or [ModelResponse("end_turn", content="已完成答复。")])
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return super().complete(messages, tools)


@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("historical", [False, True])
def test_shared_mission_uses_schema_identity_across_real_session_generations(
    tmp_path, drift, historical,
):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    tools_file = workspace / "tools.json"
    tools_file.write_text(json.dumps([TOOL]), encoding="utf-8")
    clients = McpClients({"selected": McpServerConfig(
        name="selected",
        command=(sys.executable, str(FIXTURE), "--tools-file", str(tools_file)),
    )})
    with closing(EventLog(data / "events.db", blob_dir=data / "blobs")) as log:
        stack = build_stack(log, data_root=data)
        task = stack.tasks.create_task("保持共享 Mission", [str(workspace)],
                                       mcp_servers=["selected"])
        first = clients.for_run(
            task.task_id, "first", workspace=workspace, data_root=data,
            exec_backend=LOCAL_EXEC_BACKEND, required_boundaries=(),
        )
        second = clients.for_run(
            task.task_id, "second", workspace=workspace, data_root=data,
            exec_backend=LOCAL_EXEC_BACKEND, required_boundaries=(),
        )
        try:
            first.start_all()
            registry = build_registry(first)
            mission = prepare_mission(stack.tasks, task.task_id, registry=registry)
            first_version = registry.spec("mcp__selected__echo").version
            if historical:
                value = mission.to_dict()
                next(t for t in value["tool_allowlist"]
                     if t["tool_id"] == "mcp__selected__echo")["version"] = first_version
                mission = MissionSpecV1.of(value)
            if drift:
                changed = copy.deepcopy(TOOL)
                changed["inputSchema"]["properties"]["message"] = {"type": "integer"}
                tools_file.write_text(json.dumps([changed]), encoding="utf-8")
            second.start_all()
            registry = build_registry(second)
            assert first.server("selected").pid != second.server("selected").pid
            assert first_version != registry.spec("mcp__selected__echo").version
            run = stack.tasks.start_run(task.task_id, payload={"task": task.goal})
            freeze_mission(log, run.run_id, mission=mission, registry=registry)
            ids = {
                row["name"] for row in effective_manifest(
                    log, run.run_id, registry, registry.manifest(),
                )
            }
            assert ("mcp__selected__echo" in ids) == (not drift and not historical)
        finally:
            clients.close_all()


@pytest.fixture
def connected(tmp_path):
    workspace, data = tmp_path / "ws", tmp_path / "data"
    workspace.mkdir()
    data.mkdir()
    calls = workspace / "calls.jsonl"
    tools_file = workspace / "tools.json"
    tools_file.write_text(json.dumps([TOOL]), encoding="utf-8")
    unselected = workspace / "unselected-started"
    clients = McpClients({
        "selected": McpServerConfig(
            name="selected",
            command=(sys.executable, str(FIXTURE), "--call-log", str(calls),
                     "--tools-file", str(tools_file)),
        ),
        "unselected": McpServerConfig(
            name="unselected", command=(
                sys.executable, "-c", f"open({str(unselected)!r}, 'w').write('bad')",
            ),
        ),
    })
    with closing(EventLog(data / "events.db", blob_dir=data / "blobs")) as log:
        stack = build_stack(log, data_root=data, mcp_clients=clients)
        task = stack.tasks.create_task("只调用选中的 MCP", [str(workspace)],
                                       mcp_servers=["selected"])
        yield ControlPlane(stack, model=ScriptedModel([])), stack, task, clients, workspace
        clients.close_all()


@pytest.mark.parametrize("entry", ["direct", "queued", "goal"])
def test_new_selected_mcp_run_finalizes_after_real_discovery_outside_transaction(
    connected, monkeypatch, entry,
):
    plane, stack, task, _, workspace = connected
    from tianshu.mcp import StdioServer

    original = StdioServer.start
    starts = []

    def observed_start(server):
        starts.append(server.config.name)
        assert not stack.log.conn.in_transaction, "MCP 发现不能持有 SQLite 写事务"
        return original(server)

    monkeypatch.setattr(StdioServer, "start", observed_start)
    model = RecordingModel([
        ModelResponse("tool_use", tool_calls=[
            ToolCall("mcp-call", "mcp__selected__echo", {"message": "real"}),
        ]),
        ModelResponse("end_turn", content="完成。"),
    ])
    engine = engine_for(stack, model)
    if entry == "direct":
        result = engine.start(task.task_id)
    elif entry == "queued":
        rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
        assert starts == []
        assert any(e.type == "run.mission_pending" for e in stack.log.read_run(rid))
        result = engine.resume_run(rid)
    else:
        goal = plane.handle("goal.create", {
            "task_id": task.task_id, "goal_revision": task.goal_revision,
            "criteria": ["结果可核对"], "request_id": "create-g1",
            "execution_policy": EFFECTS.to_dict(),
        })["goal"]
        rid = plane.handle("goal.advance", {
            "goal_id": goal["goal_id"], "request_id": "advance-g1",
        })["run"]["run_id"]
        assert starts == []
        assert any(e.type == "run.mission_pending" for e in stack.log.read_run(rid))
        result = engine.resume_run(rid)
    for _ in range(3):
        if result.state != "waiting_approval":
            break
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    events = stack.log.read_run(result.run_id)
    pending = next(e for e in events if e.type == "run.mission_pending")
    frozen = next(e for e in events if e.type == "run.mission")
    intent = next(e for e in events if e.type == "model.intent")
    assert pending.seq < frozen.seq < intent.seq
    assert any(e.type == "tool.result" and e.payload["status"] == "succeeded" for e in events)
    assert {t["name"] for t in model.requests[0][1]} >= {"mcp__selected__echo"}
    assert len((workspace / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert not (workspace / "unselected-started").exists()
    assert set(starts) == {"selected"}


@pytest.mark.parametrize("changed", [
    {"task_kind": "coding"}, {"criteria": ["不同条件"]}, {"tool_ids": ["read_text"]},
])
def test_pending_request_id_cannot_change_mission_configuration(connected, changed):
    plane, stack, task, _, _ = connected
    params = {"task_id": task.task_id, "request_id": "same-pending"}
    rid = plane.handle("run.start", params)["run"]["run_id"]
    assert any(e.type == "run.mission_pending" for e in stack.log.read_run(rid))
    with pytest.raises(ControlError, match="Mission|使命|request_id"):
        plane.handle("run.start", {**params, **changed})


@pytest.mark.parametrize("task_kind,tool_ids,expected", [
    ("general", ["mcp__selected__echo"], {"mcp__selected__echo"}),
    ("general", ["read_text"], {"read_text"}),
    ("research", None, set()),
])
def test_pending_discovery_preserves_explicit_tool_and_profile_limits(
    connected, task_kind, tool_ids, expected,
):
    plane, stack, task, _, workspace = connected
    params = {"task_id": task.task_id, "task_kind": task_kind}
    if tool_ids is not None:
        params["tool_ids"] = tool_ids
    rid = plane.handle("run.start", params)["run"]["run_id"]
    model = RecordingModel()
    engine_for(stack, model).resume_run(rid)
    ids = {t["name"] for t in model.requests[0][1]}
    if task_kind == "research":
        assert not any(name.startswith("mcp__") for name in ids)
    else:
        assert ids == expected
    assert not (workspace / "unselected-started").exists()


def test_shared_mission_does_not_gain_newly_discovered_tools(connected):
    plane, stack, task, _, _ = connected
    mission = prepare_mission(stack.tasks, task.task_id, registry=stack.dispatcher.registry)
    rid = plane.handle("run.start", {
        "task_id": task.task_id, "mission": mission.to_dict(),
    })["run"]["run_id"]
    model = RecordingModel()
    engine_for(stack, model).resume_run(rid)
    events = stack.log.read_run(rid)
    assert not any(e.type == "run.mission_pending" for e in events)
    assert not any(t["name"].startswith("mcp__") for t in model.requests[0][1])


def _previous_session_mission(stack, task, clients, workspace):
    bootstrap = McpClients({"selected": clients.configs["selected"]})
    try:
        first = bootstrap.for_run(
            task.task_id, "bootstrap", workspace=workspace, data_root=stack.tasks.data_root,
            exec_backend=LOCAL_EXEC_BACKEND, required_boundaries=(),
        )
        first.start_all()
        mission = prepare_mission(
            stack.tasks, task.task_id, registry=build_registry(first),
            tool_ids=["mcp__selected__echo"],
        )
    finally:
        bootstrap.close_all()
    return mission


@pytest.mark.parametrize("entry", ["direct", "queued"])
@pytest.mark.parametrize("drift", [False, True])
def test_shared_mission_from_previous_session_routes_in_fresh_empty_stack(
    connected, entry, drift,
):
    from tianshu.mission import mission_for

    plane, stack, task, clients, workspace = connected
    mission = _previous_session_mission(stack, task, clients, workspace)
    assert stack.dispatcher.registry.spec("mcp__selected__echo") is None
    if drift:
        changed = copy.deepcopy(TOOL)
        changed["inputSchema"]["properties"]["message"] = {"type": "integer"}
        (workspace / "tools.json").write_text(json.dumps([changed]), encoding="utf-8")
    model = RecordingModel([] if drift else [
        ModelResponse("tool_use", tool_calls=[
            ToolCall("shared", "mcp__selected__echo", {"message": "shared"}),
        ]),
        ModelResponse("end_turn", content="完成。"),
    ])
    engine = engine_for(stack, model)
    if entry == "direct":
        result = engine.start(task.task_id, mission=mission)
    else:
        rid = plane.handle("run.start", {
            "task_id": task.task_id, "mission": mission.to_dict(),
        })["run"]["run_id"]
        result = engine.resume_run(rid)
    for _ in range(3):
        if result.state != "waiting_approval":
            break
        approve_pending(stack, result.run_id)
        result = engine.resume_run(result.run_id)
    events = stack.log.read_run(result.run_id)
    assert not any(e.type == "run.mission_pending" for e in events)
    assert mission_for(stack.log, result.run_id).digest == mission.digest
    assert {row["name"] for row in model.requests[0][1]} == (
        set() if drift else {"mcp__selected__echo"}
    )
    succeeded = any(e.type == "tool.result" and e.payload["status"] == "succeeded"
                    for e in events)
    assert succeeded is not drift
    if drift:
        assert not (workspace / "calls.jsonl").exists()
    else:
        assert len((workspace / "calls.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert not (workspace / "unselected-started").exists()


@pytest.mark.parametrize("policy,selected,tools,expected", [
    (EFFECTS, True, None, {"mcp__selected__echo"}),
    (PURE_ONLY, True, None, set()),
    (EFFECTS, True, [], set()),
    (EFFECTS, False, None, set()),
])
def test_shared_offline_candidates_still_obey_run_policy_tools_and_selected_servers(
    connected, policy, selected, tools, expected,
):
    _, stack, task, clients, workspace = connected
    if not selected:
        task = stack.tasks.create_task("未选择 MCP", [str(workspace)])
    mission = _previous_session_mission(stack, task, clients, workspace)
    payload = {"task": task.goal}
    if tools is not None:
        payload["tools"] = tools
    run = stack.tasks.start_run(task.task_id, payload=payload, execution_policy=policy)
    freeze_mission(stack.log, run.run_id, registry=stack.dispatcher.registry, mission=mission)
    frozen = next(e for e in stack.log.read_run(run.run_id) if e.type == "run.mission")
    assert {t["tool_id"] for t in frozen.payload["effective_tools"]} == expected
    assert effective_manifest(
        stack.log, run.run_id, stack.dispatcher.registry, stack.dispatcher.registry.manifest(),
    ) == []


def test_pending_and_frozen_inputs_do_not_recompile_current_template(connected, monkeypatch):
    from tianshu import mission

    plane, stack, task, clients, _ = connected
    rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
    pending = next(e for e in stack.log.read_run(rid) if e.type == "run.mission_pending")
    expected = copy.deepcopy(pending.payload["messages"])
    monkeypatch.setattr(mission, "SYSTEM_PROMPT", "new template after queue")
    model = RecordingModel()
    result = engine_for(stack, model).resume_run(rid)
    assert result.terminal
    assert model.requests[0][0] == expected
    frozen = next(e for e in stack.log.read_run(rid) if e.type == "run.mission")
    clients.close_all()
    assert mission.mission_messages(stack.log, rid) == expected
    assert frozen.payload["messages"] == expected


def test_request_retry_keeps_original_pending_selection_after_unavailable_tool(connected):
    plane, stack, task, _, _ = connected
    params = {
        "task_id": task.task_id, "request_id": "unknown-selected-tool",
        "tool_ids": ["mcp__selected__not_advertised"],
    }
    rid = plane.handle("run.start", params)["run"]["run_id"]
    assert engine_for(stack, RecordingModel()).resume_run(rid).terminal
    assert plane.handle("run.start", params)["run"]["run_id"] == rid


def test_pending_known_schema_drift_is_not_accepted_as_a_new_tool(connected):
    from tianshu.tool_registry import set_registry

    plane, stack, task, clients, workspace = connected
    bootstrap = McpClients({"selected": clients.configs["selected"]})
    try:
        bootstrap.start_all()
        registry = build_registry(bootstrap)
        stack.dispatcher._registry = registry
        set_registry(registry)
        rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]
    finally:
        bootstrap.close_all()
    changed = copy.deepcopy(TOOL)
    changed["inputSchema"]["properties"]["message"] = {"type": "integer"}
    (workspace / "tools.json").write_text(json.dumps([changed]), encoding="utf-8")
    model = RecordingModel()
    engine_for(stack, model).resume_run(rid)
    assert "mcp__selected__echo" not in {t["name"] for t in model.requests[0][1]}


def test_frozen_pending_run_resumes_saved_input_without_refreezing(connected, monkeypatch):
    from tianshu import mission

    plane, stack, task, _, _ = connected
    rid = plane.handle("run.start", {"task_id": task.task_id})["run"]["run_id"]

    class PausingModel(RecordingModel):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            stack.tasks.request_pause(rid)
            return response

    first = PausingModel([ModelResponse("tool_use", tool_calls=[
        ToolCall("read", "list_files", {"path": "."}),
    ])])
    assert engine_for(stack, first).resume_run(rid).state == "paused"
    saved = mission.mission_messages(stack.log, rid)
    fact = next(e for e in stack.log.read_run(rid) if e.type == "run.mission")
    monkeypatch.setattr(mission, "SYSTEM_PROMPT", "changed frozen template")
    second = RecordingModel()
    engine_for(stack, second).resume_run(rid)
    assert second.requests[0][0][:len(saved)] == saved
    facts = [e for e in stack.log.read_run(rid) if e.type == "run.mission"]
    assert len(facts) == 1
    assert facts[0].payload == fact.payload
