"""R2-AUTH-01：只读子任务不能写候选记忆或制造嵌套审批。"""

from __future__ import annotations

from contextlib import closing

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import PolicyEngine
from tianshu.resources import MemoryScope
from tianshu.tasks import RUN_RUNNING
from tianshu.tools import SPECS, tool_manifest, validate_args
from tianshu.worker import Worker


def _spawn(stack):
    task = stack.tasks.create_task("Delegate read only", [], max_subtasks=2)
    model = ScriptedModel([
        ModelResponse("tool_use", tool_calls=[ToolCall(
            "spawn", "subtask.spawn", {"subtask_id": "reader", "goal": "Read only"}
        )]),
        ModelResponse("end_turn", content="Wait"),
    ])
    engine = engine_for(stack, model)
    result = engine.start(task.task_id)
    assert result.state == "waiting_approval"
    approve_pending(stack, result.run_id)
    result = engine.resume_run(result.run_id)
    row, = stack.tasks.list_subtasks(result.run_id)
    return result, row


@pytest.mark.parametrize("scope", ["user", "task"])
def test_child_worker_cannot_persist_memory_candidate(tmp_path, scope):
    """去掉子任务授权限制会落 candidate/applied；空 memory_scopes 不足以拒绝。"""
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        stack.memory.ensure_schema()
        parent, row = _spawn(stack)
        args = {"kind": "fact", "content": "CHILD_WRITE_MARKER", "scope": scope}
        if scope == "task":
            args["scope_id"] = row.child_task_id
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall("write", "memory.propose", args)]),
            ModelResponse("end_turn", content="Cannot write"),
        ])
        worker = Worker(stack, model=model, heartbeat_interval_ms=None)
        try:
            assert worker.run_once() is True
        finally:
            worker.close()
        assert worker.errors == []
        events = log.read_run(row.child_run_id)
        results = [e.payload for e in events if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["status"] == "rejected"
        assert results[0]["effect_state"] == "none_applied"
        assert not any(e.type in ("tool.intent", "approval.requested") for e in events)
        assert stack.approvals.pending(run_id=row.child_run_id) == []
        assert log.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert stack.tasks.get_run(row.child_run_id).state != "waiting_approval"
        assert stack.claims.job_for_run(row.child_run_id).state != "waiting"
        assert stack.claims.job_for_run(parent.run_id).state == "pending"


def test_root_task_can_still_propose_memory(tmp_path):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("Root may write candidates", [])
        engine = engine_for(stack, ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall("write", "memory.propose", {
                "kind": "fact", "content": "ROOT_CANDIDATE", "scope": "user",
            })]),
            ModelResponse("end_turn", content="Done"),
        ]))
        result = engine.start(task.task_id)
        results = [e.payload for e in log.read_run(result.run_id) if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["status"] == "succeeded"
        assert results[0]["effect_state"] == "applied"
        assert log.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def _seed_candidate(stack, *, scope):
    """通过真实控制 Run 的普通 propose 留候选，供子任务试图确认/改写/遗忘。"""
    control = stack.dispatcher.open_control_run(
        goal="Seed candidate", workspaces=[], memory_scopes=[scope]
    )
    try:
        args = {"kind": "fact", "content": "EXISTING_CANDIDATE"}
        task = control.task
        run = control.run
        request = build_request(
            task=task, run=run, claim=control.claim, call_id="seed",
            tool_id="memory.propose", target=scope.to_uri(), args=args,
        )
        decision = stack.dispatcher.prepare(request, control.claim, args=args)
        assert decision.is_admitted
        result = stack.dispatcher.dispatch(request.request_id, control.claim)
        assert result.status == "succeeded"
        return result.output["memory_id"]
    finally:
        stack.dispatcher.close_control_run(control)


@pytest.mark.parametrize("tool_id", [
    "memory.confirm", "memory.edit", "memory.forget", "memory.consolidate",
])
@pytest.mark.parametrize("scope_name", ["user", "task"])
def test_child_memory_writes_never_create_nested_approval(tmp_path, tool_id, scope_name):
    """有效记忆参数走真实 Worker；缺只读门会停在嵌套审批或写合成候选。"""
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        parent, row = _spawn(stack)
        scope = MemoryScope.user() if scope_name == "user" else MemoryScope.task(row.child_task_id)
        memory_id = _seed_candidate(stack, scope=scope)
        before = stack.memory.get(memory_id, allowed=(scope,))
        args = {"memory_id": memory_id, "revision": before.revision}
        if tool_id == "memory.edit":
            args["content"] = "CHILD_CHANGED_MARKER"
        elif tool_id == "memory.consolidate":
            args = {"scope": scope.scope, "scope_id": scope.scope_id}
        assert tool_id in {item["name"] for item in tool_manifest()}
        validate_args(args, SPECS[tool_id].input_schema)
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall("write", tool_id, args)]),
            ModelResponse("end_turn", content="Read only"),
        ])
        worker = Worker(stack, model=model, heartbeat_interval_ms=None)
        try:
            assert worker.run_once()
        finally:
            worker.close()
        assert worker.errors == []
        events = log.read_run(row.child_run_id)
        results = [e.payload for e in events if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["status"] == "rejected"
        assert results[0]["effect_state"] == "none_applied"
        assert results[0]["output"]["rule_id"] == "c4.memory_scope_denied"
        assert not any(e.type in ("tool.intent", "approval.requested") for e in events)
        assert stack.approvals.pending(run_id=row.child_run_id) == []
        assert stack.memory.get(memory_id, allowed=(scope,)) == before
        assert log.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert stack.tasks.get_run(row.child_run_id).state != "waiting_approval"
        assert stack.claims.job_for_run(row.child_run_id).state != "waiting"
        assert stack.claims.job_for_run(parent.run_id).state == "pending"


def test_child_read_memory_remains_available(tmp_path):
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        _, row = _spawn(stack)
        memory_id = _seed_candidate(stack, scope=MemoryScope.user())
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "read", "memory.get", {"memory_id": memory_id}
            )]),
            ModelResponse("end_turn", content="Read complete"),
        ])
        worker = Worker(stack, model=model, heartbeat_interval_ms=None)
        try:
            assert worker.run_once()
        finally:
            worker.close()
        assert worker.errors == []
        results = [e.payload for e in log.read_run(row.child_run_id) if e.type == "tool.result"]
        assert len(results) == 1
        assert results[0]["status"] == "succeeded"


@pytest.mark.parametrize("scope_name", ["user", "task"])
def test_memory_executor_rechecks_child_capability(tmp_path, scope_name):
    """构造旧版准入曾签出的真实一次性票据，独立执行层仍拒绝子任务。"""
    from tianshu.dispatcher import _Authority
    from tianshu.memory_exec import MemoryDispatchRefused

    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path / "data")
        _, row = _spawn(stack)
        task = stack.tasks.get_task(row.child_task_id)
        run = stack.tasks.get_run(row.child_run_id)
        claim = stack.claims.claim(row.job_id)
        stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
        scope = MemoryScope.user() if scope_name == "user" else MemoryScope.task(task.task_id)
        args = {"kind": "fact", "content": "EXECUTOR_MUST_REFUSE"}
        request = build_request(
            task=task, run=run, claim=claim, call_id="write", tool_id="memory.propose",
            target=scope.to_uri(), args=args,
        )
        # 故障注入只绕过上游新只读门：其余 grant/dispatch/claim 均由真实存储
        # 构造；执行器不能把这些票据当作子任务具有写权限的证据。
        auth = _Authority(run, task, list(task.workspaces), [], [])
        target = stack.dispatcher._resolve_target(request, auth)
        with log.transaction() as tx:
            stack.dispatcher._store_request(
                request, args, "admitted", "legacy admission fixture", "c4.memory_scope"
            )
            grant = stack.grants.issue(
                subject=f"{run.run_id}#{claim.claim_epoch}", request=request,
                issuer="policy:auto",
            )
            stack.grants.consume(
                grant.grant_id, request_digest=request.digest(), subject=grant.subject
            )
            seq = tx.append(run.run_id, "tool.intent", {
                "call_id": request.call_id, "tool_id": request.tool_id,
                "args": args, "args_hash": request.args_hash,
                "request_id": request.request_id,
                "request_digest": request.digest(),
                "dispatch_id": stack.dispatcher._dispatch_id(request),
                "attempt_id": request.attempt_id,
                "approval_id": None, "grant_id": grant.grant_id,
                "claim_epoch": claim.claim_epoch,
            }, task_id=task.task_id)
            stack.dispatcher._insert_dispatch(request, auth, target, seq, None, grant.grant_id)
        admitted = stack.dispatcher._admitted(request, claim, target, seq, None, grant.grant_id)
        executor = stack.dispatcher._memory_executor_for()
        with stack.dispatcher._begin_memory_execution(request, claim) as permit:
            with pytest.raises(MemoryDispatchRefused, match="只读子任务"):
                executor.execute(admitted, permit)
        assert log.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


@pytest.mark.parametrize("action", ["create", "overwrite", "delete", "read"])
def test_readonly_memory_policy_denies_writes_without_approval(action):
    decision = PolicyEngine().classify(
        action, ["memory://user/local"], [], {
            "memory_readonly_task": True, "memory_kind": "fact",
            "memory_tool": "memory.propose",
        }, task_id="child_task",
    )
    if action == "read":
        assert decision.is_auto
    else:
        assert decision.is_denied
        assert decision.rule_id == "c4.memory_scope_denied"
        assert not decision.needs_approval
