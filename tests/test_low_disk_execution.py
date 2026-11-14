"""已有 Run 的空间变化不能消费未执行的批准，也不能封死读与停止。"""

from contextlib import closing
from uuid import uuid4

import pytest
from support import engine_for

from tianshu import event_schema
from tianshu.disk_admission import DiskAdmission
from tianshu.dispatcher import build_request
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.permissions import HumanIdentity
from tianshu.run_policy import EFFECTS, PURE_ONLY
from tianshu.tasks import RUN_RUNNING


class Space:
    def __init__(self):
        self.value = 2147483648
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        if self.value == "unknown":
            raise OSError("probe unavailable")
        return self.value


def _stack(log, root):
    stack = build_stack(log, data_root=root)
    space = Space()
    stack.tasks._disk_admission = DiskAdmission(stack.tasks.disk_admission.context, probe=space)
    return stack, space


def _start(stack, task, mode="effects"):
    if mode in ("v0", "v1"):
        run_id = f"run_{uuid4().hex[:12]}"
        payload = {
            "run_id": run_id,
            "task_id": task.task_id,
            "goal_revision": task.goal_revision,
            "lease_epoch": task.lease_epoch,
        }
        version = int(mode[1])
        event_schema.validate_payload("run.start", payload, version)
        seq = stack.log.append(
            run_id, "run.start", payload, task_id=task.task_id, legacy_write=True
        )
        stack.log.conn.execute("UPDATE event_versions SET version=? WHERE seq=?", (version, seq))
        stack.tasks.rebuild_from_events()
        run = stack.tasks.get_run(run_id)
    else:
        run = stack.tasks.start_run(
            task.task_id, execution_policy=PURE_ONLY if mode == "pure_only" else EFFECTS
        )
    job = stack.claims.enqueue(run.run_id)
    claim = stack.claims.claim(job.job_id)
    writable = [binding.identity() for binding in task.workspaces if binding.can_write]
    if writable and mode != "pure_only":
        stack.leases.acquire(writable, run.run_id)
    stack.tasks.transition(run.run_id, RUN_RUNNING, claim=claim)
    return stack.tasks.get_run(run.run_id), claim


def _request(stack, task, run, claim, tool, target, args, call_id=None):
    return build_request(
        task=task,
        run=run,
        claim=claim,
        call_id=call_id or uuid4().hex,
        tool_id=tool,
        target=target,
        args=args,
        registry=stack.dispatcher.registry,
    )


def _approve(stack, decision):
    stack.approvals.decide(
        decision.approval_id, decision="approved", actor=HumanIdentity.local_cli()
    )


@pytest.mark.parametrize(
    "value,rule", [(0, "dispatch.disk_space_low"), ("unknown", "dispatch.disk_space_unknown")]
)
def test_waiting_approval_rechecks_without_consumption(tmp_path, value, rule):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    note.write_bytes(b"before")
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("write", [str(workspace)])
        run, claim = _start(stack, task)
        stack.snapshots.init_run(run.run_id)
        args = {"path": "note.md", "content": "after"}
        request = _request(stack, task, run, claim, "modify_text", note, args)
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.is_waiting
        _approve(stack, decision)
        grants_before = list(log.conn.execute("SELECT * FROM grants"))
        space.value = value
        result = stack.dispatcher.dispatch(request.request_id, claim)
        assert result.status == "rejected", result.output
        assert result.output["rule_id"] == rule
        assert result.effect_state == "none_applied"
        assert stack.approvals.get(decision.approval_id).state == "approved"
        assert list(log.conn.execute("SELECT * FROM grants")) == grants_before
        assert not any(event.type == "tool.intent" for event in log.read_run(run.run_id))
        assert note.read_bytes() == b"before"


@pytest.mark.parametrize("mode", ["effects", "v0", "v1", "pure_only"])
@pytest.mark.parametrize("value", [0, "unknown"])
def test_pure_dispatch_and_existing_stop(tmp_path, mode, value):
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("parent", max_subtasks=2)
        run, claim = _start(stack, task, mode)
        stack.tasks.create_subtask(task, run, subtask_id="reader", goal="read")
        space.value = value
        for tool in ("subtask.status", "subtask.result", "subtask.cancel"):
            args = {"subtask_id": "reader"}
            request = _request(stack, task, run, claim, tool, "subtask://reader", args)
            decision = stack.dispatcher.prepare(request, claim, args=args)
            if mode == "pure_only" and tool == "subtask.cancel":
                assert decision.is_rejected
                assert decision.rule_id == "dispatch.run_effects_disallowed"
            else:
                assert decision.is_admitted, decision
                result = stack.dispatcher.dispatch(request.request_id, claim)
                assert result.ok, result.output
        for tool in ("subtask.status", "subtask.result", "subtask.cancel"):
            args = {"subtask_id": "missing"}
            request = _request(stack, task, run, claim, tool, "subtask://missing", args)
            assert stack.dispatcher.prepare(request, claim, args=args).is_rejected
        other_task = stack.tasks.create_task("other parent", max_subtasks=2)
        other_run, other_claim = _start(stack, other_task, "pure_only")
        request = _request(
            stack,
            other_task,
            other_run,
            other_claim,
            "subtask.status",
            "subtask://reader",
            {"subtask_id": "reader"},
        )
        denied = stack.dispatcher.prepare(request, other_claim, args={"subtask_id": "reader"})
        assert denied.rule_id == "dispatch.subtask_not_found"
        stack.tasks.request_pause(run.run_id)
        stack.tasks.request_cancel(run.run_id)
        assert stack.tasks.get_run(run.run_id).control_requested == "cancel"


def _drive_route(stack, model, claim, route, limits=None):
    import threading

    from tianshu.control import ControlPlane
    from tianshu.daemon import _Daemon
    from tianshu.loop import _fresh_progress
    from tianshu.worker import Worker

    engine = engine_for(stack, model, limits=limits)
    if route == "worker":
        worker = Worker(stack, model, limits=limits, heartbeat_interval_ms=None)
        try:
            return worker._drive(claim)
        finally:
            worker.close()
    if route == "daemon":
        daemon = _Daemon(
            stack.tasks.data_root,
            model=model,
            limits=limits,
            driver_interval_s=0.1,
            stop_event=threading.Event(),
            on_ready=None,
        )
        daemon.driver_stack, daemon.driver_log = stack, stack.log
        daemon.plane = ControlPlane(stack, model=model, limits=limits)
        try:
            daemon._resume_one(engine, claim.run_id)
        finally:
            if daemon.side is not None:
                daemon.side.close()
        return stack.tasks.get_run(claim.run_id)
    if route == "drive":
        task = stack.tasks.get_task(stack.tasks.get_run(claim.run_id).task_id)
        return engine._drive(claim.run_id, claim, _fresh_progress(task.goal, None))
    return engine.resume_run(claim.run_id)


@pytest.mark.parametrize("route", ["resume", "reopen", "pause", "worker", "daemon", "drive"])
@pytest.mark.parametrize("mode", ["effects", "v0", "v1"])
@pytest.mark.parametrize("value", [0, "unknown"])
def test_effects_and_legacy_resume_pair(tmp_path, route, mode, value):
    from test_mcp_dispatch import _config

    from tianshu.mcp import McpClients
    from tianshu.model import ModelResponse, ScriptedModel, ToolCall

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.md").write_bytes(b"retained")
    log = EventLog(tmp_path / "events.db")
    clients = McpClients({"fake": _config()})
    try:
        stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
        space = Space()
        stack.tasks._disk_admission = DiskAdmission(stack.tasks.disk_admission.context, probe=space)
        task = stack.tasks.create_task("read", [str(workspace)], mcp_servers=["fake"])
        run, claim = _start(stack, task, mode)
        original = log.read_run(run.run_id)[0]
        account = f"run:{run.run_id}"
        stack.budget.create_account(account, limits={"requests": 20}, run_id=run.run_id)
        reserved = stack.budget.reserve(
            account, attempt_id="previous", run_id=run.run_id, need={"requests": 1, "tokens": 7}
        )
        stack.budget.settle(reserved.reservation_id, known=False)
        prior = stack.budget.reconcile(account)
        if route == "pause":
            stack.tasks.request_pause(run.run_id)
            paused = engine_for(stack, ScriptedModel([])).resume_run(run.run_id)
            assert paused.state == "paused"
        if route == "reopen":
            log.close()
            log = EventLog(tmp_path / "events.db")
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            stack.tasks._disk_admission = DiskAdmission(
                stack.tasks.disk_admission.context, probe=space
            )
        space.value = value
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use", tool_calls=[ToolCall("read1", "read_text", {"path": "note.md"})]
                ),
                ModelResponse("end_turn", content="done"),
            ]
        )
        result = _drive_route(stack, model, claim, route)
        assert result.state == "succeeded", result
        assert result.run_id == run.run_id
        assert log.read_run(run.run_id)[0] == original
        assert stack.tasks.run_execution_policy(run.run_id) == (
            EFFECTS if mode == "effects" else None
        )
        assert sum(event.type == "model.intent" for event in log.read_run(run.run_id)) == 2
        assert all(row in stack.budget.reconcile(account) for row in prior)
        assert stack.budget.usage(account)["reserved"]["requests"] >= 1
        job = stack.claims.job_for_run(run.run_id)
        assert job.claim_epoch == claim.claim_epoch
        assert clients.server("fake") is None, "恢复读/模型不能启动 MCP"
    finally:
        clients.close_all()
        log.close()


@pytest.mark.parametrize("route", ["resume", "worker", "daemon", "drive"])
@pytest.mark.parametrize("mode", ["effects", "v0", "v1"])
@pytest.mark.parametrize("value", [0, "unknown"])
def test_summary_model_survives_low_space(tmp_path, route, mode, value):
    from test_summary_loop import ConstraintModel, _ws

    from tianshu.loop import Limits

    workspace = _ws(tmp_path)
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("summarize", [str(workspace)])
        run, claim = _start(stack, task, mode)
        original = log.read_run(run.run_id)[0]
        space.value = value
        model = ConstraintModel()
        result = _drive_route(
            stack,
            model,
            claim,
            route,
            limits=Limits(max_turns=9, context_budget_bytes=3000, summary_mode="model"),
        )
        assert result.state == "succeeded", result
        assert model.summary_calls >= 1
        assert model.round_calls == 7
        assert log.read_run(run.run_id)[0] == original
        assert stack.summaries.summary_count() == model.summary_calls


@pytest.mark.parametrize("boundary", ["begin", "file", "memory_begin", "memory", "network", "mcp"])
@pytest.mark.parametrize("value", [0, "unknown"])
def test_actual_boundary_rechecks(tmp_path, monkeypatch, boundary, value):
    """真实执行链已消费审批后，只在指定边界前改变空间叶子读数。"""
    from httpserver import NetFixture
    from test_mcp_dispatch import _config

    from tianshu.mcp import McpClients
    from tianshu.resources import MemoryScope

    fixture = NetFixture().route("/page", body="read").start()
    calls = tmp_path / "calls.jsonl"
    clients = McpClients({"fake": _config("fake", "--call-log", str(calls))})
    workspace = tmp_path / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    note.write_bytes(b"before")
    try:
        with closing(EventLog(tmp_path / "events.db")) as log:
            stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=clients)
            space = Space()
            stack.tasks._disk_admission = DiskAdmission(
                stack.tasks.disk_admission.context, probe=space
            )
            task = stack.tasks.create_task(
                "effects",
                [] if boundary.startswith("memory") else [str(workspace)],
                outbound_allow=[fixture.origin_uri],
                mcp_servers=["fake"],
            )
            if boundary == "mcp":
                stack.dispatcher.prepare_run(task.task_id, EFFECTS)
            run, claim = _start(stack, task)
            stack.snapshots.init_run(run.run_id)
            if boundary.startswith("memory"):
                tool, target = "memory.propose", MemoryScope.user().to_uri()
                args = {"kind": "preference", "content": "write no bytes"}
                owner, method = (
                    (stack.dispatcher, "_begin_memory_execution")
                    if boundary == "memory_begin"
                    else (stack.dispatcher._memory_executor_for(), "execute")
                )
            elif boundary == "network":
                tool, target = "network.fetch", fixture.url("/page")
                args = {"url": target}
                owner, method = stack.dispatcher, "_dispatch_network"
            elif boundary == "mcp":
                tool, target = "mcp__fake__echo", "mcp://fake/echo"
                args = {"message": "must not send"}
                owner, method = stack.dispatcher, "_mark_mcp_send_boundary"
            else:
                tool, target = "modify_text", note
                args = {"path": "note.md", "content": "after"}
                owner, method = (
                    (stack.tasks, "begin_execution")
                    if boundary == "begin"
                    else (stack.dispatcher._executor, "execute")
                )
            request = _request(stack, task, run, claim, tool, target, args)
            decision = stack.dispatcher.prepare(request, claim, args=args)
            assert decision.is_waiting or decision.is_admitted, decision
            if decision.is_waiting:
                _approve(stack, decision)
            original = getattr(owner, method)
            entered = []

            def lower_at_boundary(*args, **kwargs):
                entered.append(True)
                space.value = value
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, method, lower_at_boundary)
            result = stack.dispatcher.dispatch(request.request_id, claim)
            assert entered
            assert result.status == "rejected", result.output
            assert result.effect_state == "none_applied"
            assert result.output["rule_id"] == (
                "dispatch.disk_space_low" if value == 0 else "dispatch.disk_space_unknown"
            )
            assert note.read_bytes() == b"before"
            assert fixture.hits == []
            assert not calls.exists() or not calls.read_bytes()
            if boundary.startswith("memory"):
                assert not list(log.conn.execute("SELECT * FROM memories"))
    finally:
        clients.close_all()
        fixture.stop()


@pytest.mark.parametrize("value", [0, "unknown", "schema_changed"])
def test_mcp_reopen_rejects_without_sending_or_consuming(tmp_path, value):
    import json

    from test_mcp_dispatch import _config

    from tianshu.mcp import McpClients
    from tianshu.model import ModelResponse, ScriptedModel, ToolCall

    log = EventLog(tmp_path / "events.db")
    first = McpClients({"fake": _config()})
    calls = tmp_path / "second-calls.jsonl"
    tools = tmp_path / "changed-tools.json"
    tools.write_text(
        json.dumps(
            [
                {
                    "name": "echo",
                    "description": "回显一段文本",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}, "extra": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                }
            ]
        )
    )
    second = McpClients(
        {
            "fake": _config(
                "fake",
                "--call-log",
                str(calls),
                *(("--tools-file", str(tools)) if value == "schema_changed" else ()),
            )
        }
    )
    try:
        stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=first)
        task = stack.tasks.create_task("pending", mcp_servers=["fake"])
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("mcp1", "mcp__fake__echo", {"message": "pending"})],
                )
            ]
        )
        waiting = engine_for(stack, model).start(task.task_id)
        assert waiting.state == "waiting_approval"
        approval = stack.approvals.pending(run_id=waiting.run_id)[0]
        stack.approvals.decide(
            approval.approval_id, decision="approved", actor=HumanIdentity.local_cli()
        )
        first.close_all()
        log.close()
        log = EventLog(tmp_path / "events.db")
        stack = build_stack(log, data_root=tmp_path / "data", mcp_clients=second)
        space = Space()
        space.value = 2147483648 if value == "schema_changed" else value
        stack.tasks._disk_admission = DiskAdmission(stack.tasks.disk_admission.context, probe=space)
        result = engine_for(
            stack, ScriptedModel([ModelResponse("end_turn", content="continued")])
        ).resume_run(waiting.run_id)
        assert result.state == "succeeded", result.error
        assert (second.server("fake") is None) == (value != "schema_changed")
        assert not calls.exists() or not calls.read_bytes()
        assert stack.approvals.get(approval.approval_id).state == "approved"
        events = log.read_run(waiting.run_id)
        assert not any(e.type == "tool.intent" for e in events)
        rejected = [e for e in events if e.type == "tool.result"]
        assert len(rejected) == 1
        assert rejected[0].payload["effect_state"] == "none_applied"
        assert rejected[0].payload["output"]["rule_id"] == (
            "dispatch.not_admitted"
            if value == "schema_changed"
            else "dispatch.disk_space_low"
            if value == 0
            else "dispatch.disk_space_unknown"
        )
    finally:
        first.close_all()
        second.close_all()
        log.close()


@pytest.mark.parametrize("value", [0, "unknown"])
def test_each_actual_location_is_checked_at_file_boundary(tmp_path, monkeypatch, value):
    """每个真实材料路径单独不足；在同一实例 probe 叶子选路径，不另造上下文。"""
    from tianshu.authority import authority_root

    workspace = tmp_path / "ws"
    nested = workspace / "mounted"
    nested.mkdir(parents=True)
    note = nested / "note.md"
    note.write_bytes(b"before")
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("write", [str(workspace)])
        run, claim = _start(stack, task)
        stack.snapshots.init_run(run.run_id)
        moved_authority = tmp_path / "new-authority"
        moved_authority.mkdir()
        context = stack.tasks.disk_admission.context
        locations = list(
            dict.fromkeys(p.resolve() for p in (*context.locations(), workspace, note, note.parent))
        )
        original = stack.dispatcher._executor.execute
        for selected in locations:
            args = {"path": "mounted/note.md", "content": "after"}
            request = _request(stack, task, run, claim, "modify_text", note, args)
            decision = stack.dispatcher.prepare(request, claim, args=args)
            assert decision.is_waiting, decision
            _approve(stack, decision)
            seen = []

            def probe(path, seen=seen, selected=selected):
                seen.append(path)
                if path == selected:
                    if value == "unknown":
                        raise OSError("unavailable")
                    return 0
                return 2147483648

            def at_file(*args, **kwargs):
                stack.tasks.disk_admission._probe = probe
                return original(*args, **kwargs)

            monkeypatch.setattr(stack.dispatcher._executor, "execute", at_file)
            result = stack.dispatcher.dispatch(request.request_id, claim)
            stack.tasks.disk_admission._probe = space
            assert result.status == "rejected", (selected, result.output)
            assert selected in seen
            assert note.read_bytes() == b"before"
        # authority 解析器每次现读：切到新 root 后也出现在实际 probe 序列。
        monkeypatch.setenv("TIANSHU_AUTHORITY_ROOT", str(moved_authority))
        assert authority_root() == moved_authority
        space.paths.clear()
        args = {"path": "mounted/note.md", "content": "after"}
        request = _request(stack, task, run, claim, "modify_text", note, args)
        stack.dispatcher.prepare(request, claim, args=args)
        assert (moved_authority / "authority.db").resolve() in space.paths


def test_enospc_rejection_does_not_consume_or_rewrite_history(tmp_path, monkeypatch):
    import errno

    workspace = tmp_path / "ws"
    workspace.mkdir()
    note = workspace / "note.md"
    note.write_bytes(b"before")
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("write", [str(workspace)])
        run, claim = _start(stack, task)
        stack.snapshots.init_run(run.run_id)
        args = {"path": "note.md", "content": "first"}
        request = _request(stack, task, run, claim, "modify_text", note, args)
        decision = stack.dispatcher.prepare(request, claim, args=args)
        _approve(stack, decision)
        assert stack.dispatcher.dispatch(request.request_id, claim).effect_state == "applied"
        old_events = log.read_run(run.run_id)
        old_manifest = (stack.snapshots.manifest_dir / f"{run.run_id}.json").read_bytes()
        args = {"path": "note.md", "content": "second"}
        request = _request(stack, task, run, claim, "modify_text", note, args)
        decision = stack.dispatcher.prepare(request, claim, args=args)
        _approve(stack, decision)
        space.value = 0
        original = log.append

        def disk_full(*args, **kwargs):
            if args[1] == "dispatch.rejected":
                raise OSError(errno.ENOSPC, "audit disk full")
            return original(*args, **kwargs)

        monkeypatch.setattr(log, "append", disk_full)
        with pytest.raises(OSError, match="audit disk full"):
            stack.dispatcher.dispatch(request.request_id, claim)
        assert log.read_run(run.run_id)[: len(old_events)] == old_events
        assert (stack.snapshots.manifest_dir / f"{run.run_id}.json").read_bytes() == old_manifest
        assert stack.approvals.get(decision.approval_id).state == "approved"
        assert note.read_bytes() == b"first"
        assert sum(e.type == "tool.intent" for e in log.read_run(run.run_id)) == 1


def _waiting_process(root, pipe):
    """拥有 claim 的真实执行进程；通信后现读控制进程写的 probe 值。"""
    import traceback

    try:
        with closing(EventLog(root / "events.db")) as log:
            stack, _space = _stack(log, root / "data")

            def probe(_path):
                return int((root / "free").read_text())

            stack.tasks._disk_admission = DiskAdmission(
                stack.tasks.disk_admission.context, probe=probe
            )
            task = stack.tasks.create_task("write", [str(root / "ws")])
            run, claim = _start(stack, task)
            stack.snapshots.init_run(run.run_id)
            args = {"path": "note.md", "content": "after"}
            request = _request(stack, task, run, claim, "modify_text", root / "ws/note.md", args)
            decision = stack.dispatcher.prepare(request, claim, args=args)
            assert decision.is_waiting
            pipe.send(("waiting", run.run_id, decision.approval_id))
            assert pipe.poll(20) and pipe.recv() == "dispatch"
            result = stack.dispatcher.dispatch(request.request_id, claim)
            pipe.send(
                (
                    "result",
                    result.status,
                    result.effect_state,
                    result.output,
                    stack.approvals.get(decision.approval_id).state,
                    [e.type for e in log.read_run(run.run_id)],
                )
            )
    except BaseException:
        pipe.send(("error", traceback.format_exc()))
        raise
    finally:
        pipe.close()


def _approval_process(root, pipe):
    import traceback

    from tianshu.permissions import ApprovalStore

    try:
        assert pipe.poll(20)
        approval_id, value = pipe.recv()
        with closing(EventLog(root / "events.db")) as log:
            ApprovalStore(log).decide(
                approval_id, decision="approved", actor=HumanIdentity.local_cli()
            )
            (root / "free").write_text(str(value))
            pipe.send("approved-and-space-changed")
    except BaseException:
        pipe.send(("error", traceback.format_exc()))
        raise
    finally:
        pipe.close()


@pytest.mark.parametrize("value", [0, "unknown"])
def test_two_process_approval_barrier(tmp_path, value):
    import multiprocessing

    (tmp_path / "ws").mkdir()
    (tmp_path / "ws/note.md").write_bytes(b"before")
    (tmp_path / "free").write_text("2147483648")
    context = multiprocessing.get_context("spawn")
    parent_writer, child_writer = context.Pipe()
    parent_control, child_control = context.Pipe()
    writer = context.Process(target=_waiting_process, args=(tmp_path, child_writer))
    controller = context.Process(target=_approval_process, args=(tmp_path, child_control))
    writer.start()
    controller.start()
    child_writer.close()
    child_control.close()
    try:
        assert parent_writer.poll(20)
        ready = parent_writer.recv()
        assert ready[0] == "waiting", ready
        parent_control.send((ready[2], value))
        assert parent_control.poll(20)
        assert parent_control.recv() == "approved-and-space-changed"
        parent_writer.send("dispatch")
        assert parent_writer.poll(20)
        result = parent_writer.recv()
        assert result[0:3] == ("result", "rejected", "none_applied"), result
        assert result[3]["rule_id"] == (
            "dispatch.disk_space_low" if value == 0 else "dispatch.disk_space_unknown"
        )
        assert result[4] == "approved"
        assert "tool.intent" not in result[5]
        assert (tmp_path / "ws/note.md").read_bytes() == b"before"
        writer.join(10)
        controller.join(10)
        assert writer.exitcode == controller.exitcode == 0
    finally:
        for process in (writer, controller):
            if process.is_alive():
                process.terminate()
            process.join(10)
        parent_writer.close()
        parent_control.close()


@pytest.mark.parametrize("value", [0, "unknown"])
def test_crash_unknown_is_preserved_at_low_space(tmp_path, value):
    from test_mcp_crash import CALL_ID, _crash_after_call, _recover

    from tianshu.recovery import assess_run

    env, run_id = _crash_after_call(tmp_path)
    try:
        space = Space()
        space.value = value
        env.stack.tasks._disk_admission = DiskAdmission(
            env.stack.tasks.disk_admission.context, probe=space
        )
        original = env.log.read_run(run_id)
        manifest = (env.stack.snapshots.manifest_dir / f"{run_id}.json").read_bytes()
        assert len(env.calls()) == len(env.effects()) == 1
        assert assess_run(env.data, run_id, log=env.log).unresolved == (CALL_ID,)
        assert env.log.read_run(run_id) == original
        report = _recover(env, run_id)
        assert report.unresolved == [CALL_ID]
        assert report.none_applied == []
        settled = env.log.read_run(run_id)
        assert settled[: len(original)] == original
        results = env.payloads(run_id, "tool.result")
        assert len(results) == 1 and results[0]["effect_state"] == "unknown"
        assert env.types(run_id).count("receipt") == 0
        _recover(env, run_id)
        assert env.log.read_run(run_id) == settled
        assert len(env.calls()) == len(env.effects()) == 1
        assert (env.stack.snapshots.manifest_dir / f"{run_id}.json").read_bytes() == manifest
    finally:
        env.log.close()


def test_exec_cwd_and_pure_direct_begin_are_gated(tmp_path):
    import sys

    from test_run_command import _policy

    from tianshu.run_policy import RunEffectsDisallowed

    workspace = tmp_path / "ws"
    cwd = workspace / "mounted"
    cwd.mkdir(parents=True)
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, _space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("exec", [str(workspace)], exec_policy=_policy(workspace))
        run, claim = _start(stack, task)

        def probe(path):
            return 0 if path == cwd.resolve() else 2147483648

        stack.tasks.disk_admission._probe = probe
        args = {"argv": [sys.executable, "-c", "print('not executed')"], "cwd": "mounted"}
        request = _request(stack, task, run, claim, "run_command", cwd, args)
        decision = stack.dispatcher.prepare(request, claim, args=args)
        assert decision.rule_id == "dispatch.disk_space_low"
        assert not stack.approvals.pending(run_id=run.run_id)
        pure_task = stack.tasks.create_task("pure", [str(cwd)])
        pure_run, _ = _start(stack, pure_task, "pure_only")
        # 伪造调用方显式取得租约也不能把 pure_only 升成 effects。
        stack.leases.release_run(run.run_id)
        stack.leases.acquire([cwd], pure_run.run_id)
        with pytest.raises(RunEffectsDisallowed):
            with stack.tasks.begin_execution(stack.tasks.data_root, pure_run.run_id, [cwd]):
                pytest.fail("pure_only must never yield an execution permit")
        assert not any(e.type == "execution.started" for e in log.read_run(pure_run.run_id))


def test_pure_only_write_proposal_keeps_dispatch_and_result_audit(tmp_path):
    """pure_only 的合法提案必须进入派发层拒绝，而不是在模型轮次被静默丢弃。"""
    from tianshu.loop import Limits
    from tianshu.model import ModelResponse, ScriptedModel, ToolCall

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack = build_stack(log, data_root=tmp_path / "data")
        task = stack.tasks.create_task("pure-only audit", [str(workspace)])
        run, claim = _start(stack, task, "pure_only")
        # 模拟旧版或伪造调用方已拿到租约：pure_only 资格仍须在派发层独立拒绝。
        stack.leases.acquire([workspace], run.run_id)
        model = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            "write-1",
                            "create_report",
                            {"name": "must-not-exist.md", "content": "forbidden"},
                        )
                    ],
                ),
                ModelResponse("end_turn", content="proposal refused"),
            ]
        )
        result = _drive_route(
            stack,
            model,
            claim,
            "drive",
            limits=Limits(8, 20_000),
        )

        events = log.read_run(result.run_id)
        rejected = [e.payload for e in events if e.type == "dispatch.rejected"]
        results = [e.payload for e in events if e.type == "tool.result"]
        assert len(rejected) == len(results) == 1
        assert rejected[0]["rule_id"] == "dispatch.run_effects_disallowed"
        assert results[0]["call_id"] == "write-1"
        assert results[0]["status"] == "rejected"
        assert results[0]["effect_state"] == "none_applied"
        assert results[0]["output"]["rule_id"] == "dispatch.run_effects_disallowed"
        assert not any(e.type == "tool.not_dispatched" for e in events)
        assert not (workspace / "must-not-exist.md").exists()


@pytest.mark.parametrize("boundary", ["begin", "memory_begin"])
def test_direct_execution_guard_rejects_before_started(tmp_path, boundary):
    from tianshu.disk_admission import DiskAdmissionError
    from tianshu.dispatcher import DispatchDenied
    from tianshu.resources import MemoryScope

    workspace = tmp_path / "ws"
    workspace.mkdir()
    with closing(EventLog(tmp_path / "events.db")) as log:
        stack, space = _stack(log, tmp_path / "data")
        task = stack.tasks.create_task("guard", [str(workspace)])
        run, claim = _start(stack, task)
        if boundary == "memory_begin":
            args = {"kind": "preference", "content": "no bytes"}
            request = _request(
                stack, task, run, claim, "memory.propose", MemoryScope.user().to_uri(), args
            )
            assert stack.dispatcher.prepare(request, claim, args=args).is_admitted
            enter = stack.dispatcher._begin_memory_execution(request, claim)
        else:
            enter = stack.tasks.begin_execution(stack.tasks.data_root, run.run_id, [workspace])
        space.value = 0
        with pytest.raises((DiskAdmissionError, DispatchDenied)) as refusal:
            with enter:
                pytest.fail("low space must not yield an execution permit")
        assert refusal.value.rule_id == "dispatch.disk_space_low"
        assert not any(e.type == "execution.started" for e in log.read_run(run.run_id))
