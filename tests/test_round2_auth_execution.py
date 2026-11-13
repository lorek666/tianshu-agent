"""R2-AUTH-02：真实进程在 intent 已提交后撤销，必须先于实际执行生效。"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from support import approve_pending, engine_for, stack_for

from tianshu.events import EventLog
from tianshu.leases import GuardTimeout, execution_guard
from tianshu.loop import build_stack
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.permissions import ApprovalError, HumanIdentity


def _drive(root: Path, kind: str, url: str, point: str, conn) -> None:
    import tianshu.dispatcher as module

    with closing(EventLog(root / "events.db", blob_dir=root / "blobs")) as log:
        clock = [int(time.time() * 1000)]
        stack = build_stack(log, data_root=root / "data", clock=lambda: clock[0])
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        if kind == "network":
            task = stack.tasks.create_task(
                "Fetch local fixture", [], outbound_allow=[url.rsplit("/", 1)[0]]
            )
            call = ToolCall("call", "network.fetch", {"url": url})
        else:
            task = stack.tasks.create_task(
                "Delegate read only", [str(workspace)], max_subtasks=2
            )
            call = ToolCall(
                "call", "subtask.spawn", {"subtask_id": "reader", "goal": "Read only"}
            )
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[call]),
            ModelResponse("end_turn", content="Done"),
        ])
        engine = engine_for(stack, model)
        result = engine.start(task.task_id)
        assert result.state == "waiting_approval", result
        approval_id, = approve_pending(stack, result.run_id)

        def barrier() -> None:
            conn.send({
                "pid": os.getpid(), "run_id": result.run_id,
                "task_id": task.task_id, "approval_id": approval_id,
            })
            assert conn.poll(20), "controller did not release the barrier"
            assert conn.recv() == "release"

        original_failpoint = module.failpoint

        def failpoint(name: str, **kwargs) -> None:
            if name == point or (name == "after_intent" and point == "grant_expiry"):
                barrier()
                if point == "grant_expiry":
                    record = stack.dispatcher.dispatch_record(result.run_id, call.call_id)
                    grant = stack.grants.get(record.grant_id)
                    clock[0] = grant.expires_at + 1
            original_failpoint(name, **kwargs)

        module.failpoint = failpoint
        if point == "network_send":
            original_fetch = module.net_fetch

            def fetch(*args, **kwargs):
                barrier()
                return original_fetch(*args, **kwargs)

            module.net_fetch = fetch
        elif point == "subtask_result":
            original_commit = stack.dispatcher._commit_result

            def commit(*args, **kwargs):
                barrier()
                return original_commit(*args, **kwargs)

            stack.dispatcher._commit_result = commit
        resumed = engine.resume_run(result.run_id)
        conn.send({"finished": True, "state": resumed.state})


@pytest.fixture()
def local_http():
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            body = b"LOCAL_ONLY_RESPONSE"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"network://http/127.0.0.1:{server.server_port}/probe", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def _start(root, kind, url, point="after_intent"):
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_drive, args=(root, kind, url, point, child))
    process.start()
    child.close()
    assert parent.poll(20), "no dispatch barrier"
    reached = parent.recv()
    assert reached["pid"] != os.getpid()
    return process, parent, reached


def _finish(process, conn):
    conn.send("release")
    assert conn.poll(20), "dispatch did not finish"
    assert conn.recv()["finished"] is True
    process.join(10)
    assert process.exitcode == 0


@pytest.mark.parametrize("kind", ["network", "subtask"])
@pytest.mark.parametrize("control", ["revoke", "fence", "cancel", "pause", "claim_lost"])
def test_control_wins_after_committed_intent(tmp_path, local_http, kind, control):
    """删掉执行边界的复核会真实 GET/派子；不能靠结果状态掩盖已发生的动作。"""
    url, received = local_http
    process, conn, reached = _start(tmp_path, kind, url)
    try:
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = stack_for(log, data_root=tmp_path / "data")
            record = stack.dispatcher.dispatch_record(reached["run_id"], "call")
            assert record.state == "intent"
            if control == "revoke":
                revoked = stack.dispatcher.revoke(
                    reached["approval_id"], actor=HumanIdentity.local_cli()
                )
                assert revoked.state == "revoked"
            elif control == "fence":
                new_workspace = tmp_path / "unapproved-workspace"
                new_workspace.mkdir()
                stack.tasks.revise_task(
                    reached["task_id"], goal="Changed goal", bindings=[str(new_workspace)]
                )
            elif control == "cancel":
                stack.tasks.request_cancel(reached["run_id"])
            elif control == "pause":
                stack.tasks.request_pause(reached["run_id"])
            else:
                job = stack.claims.job_for_run(reached["run_id"])
                stack.claims.cancel(job.job_id, reason="controller reclaimed the job")
            control_seq = log.seq_head()
            _finish(process, conn)
            assert received == [], "control completed before any HTTP request"
            assert stack.tasks.list_subtasks(reached["run_id"]) == []
            events = log.read_run(reached["run_id"])
            assert not any(e.type == "execution.started" and e.seq > control_seq
                           for e in events)
            results = [e.payload for e in events
                       if e.type == "tool.result" and e.payload["call_id"] == "call"]
            assert len(results) == 1
            assert results[0]["effect_state"] == "none_applied"
            assert results[0]["status"] != "succeeded"
            record = stack.dispatcher.dispatch_record(reached["run_id"], "call")
            assert record.state == ("revoked" if control == "revoke" else "rejected")
    finally:
        conn.close()
        if process.is_alive():
            process.terminate()
        process.join(5)


def test_subtask_execution_holds_guard_and_revoke_cannot_succeed(tmp_path, local_http):
    """派生事务先赢：真实子实体落库，结果前仍持锁，撤销不能谎称成功。"""
    url, received = local_http
    process, conn, reached = _start(tmp_path, "subtask", url, "subtask_result")
    try:
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = stack_for(log, data_root=tmp_path / "data")
            row, = stack.tasks.list_subtasks(reached["run_id"])
            child = stack.tasks.get_task(row.child_task_id)
            assert [b.root_path for b in child.workspaces] == [
                str((tmp_path / "workspace").resolve())
            ]
            assert stack.dispatcher.dispatch_record(reached["run_id"], "call").state == "executing"
            assert not any(e.type == "tool.result" for e in log.read_run(reached["run_id"]))
            with pytest.raises(GuardTimeout):
                with execution_guard(tmp_path / "data", timeout_ms=0):
                    pytest.fail("spawn must hold the cross-process guard through result")
            _finish(process, conn)
            with pytest.raises(ApprovalError, match="已经开始执行"):
                stack.dispatcher.revoke(reached["approval_id"], actor=HumanIdentity.local_cli())
            assert stack.approvals.get(reached["approval_id"]).state == "consumed"
            assert received == []
            events = log.read_run(reached["run_id"])
            started, = [e.seq for e in events if e.type == "execution.started"]
            spawned, = [e.seq for e in events if e.type == "subtask.spawned"]
            result, = [e.seq for e in events if e.type == "tool.result"]
            assert started < spawned < result
    finally:
        conn.close()
        if process.is_alive():
            process.terminate()
        process.join(5)


def test_network_execution_wins_revoke_is_rejected(tmp_path, local_http):
    """发送开始先赢时，撤销不能报告成功；真实请求随后完成一次。"""
    url, received = local_http
    process, conn, reached = _start(tmp_path, "network", url, "network_send")
    try:
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = stack_for(log, data_root=tmp_path / "data")
            assert received == []
            with pytest.raises(ApprovalError, match="已经开始执行"):
                stack.dispatcher.revoke(
                    reached["approval_id"], actor=HumanIdentity.local_cli()
                )
            _finish(process, conn)
            assert received == ["/probe"]
            assert stack.approvals.get(reached["approval_id"]).state == "consumed"
            assert stack.dispatcher.dispatch_record(reached["run_id"], "call").state == "done"
            events = log.read_run(reached["run_id"])
            started = [e.seq for e in events if e.type == "execution.started"]
            results = [e.seq for e in events if e.type == "tool.result"]
            assert len(started) == len(results) == 1
            assert started[0] < results[0]
    finally:
        conn.close()
        if process.is_alive():
            process.terminate()
        process.join(5)


@pytest.mark.parametrize("kind", ["network", "subtask"])
def test_consumed_grant_expiry_is_rechecked_before_execution(tmp_path, local_http, kind):
    """推进注入时钟越过已消费 grant 的期限；不等待真实 TTL，也不改授权行。"""
    url, received = local_http
    process, conn, reached = _start(tmp_path, kind, url, "grant_expiry")
    try:
        _finish(process, conn)
        with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
            stack = stack_for(log, data_root=tmp_path / "data")
            assert received == []
            assert stack.tasks.list_subtasks(reached["run_id"]) == []
            assert stack.dispatcher.dispatch_record(reached["run_id"], "call").state == "rejected"
            results = [e.payload for e in log.read_run(reached["run_id"])
                       if e.type == "tool.result"]
            assert len(results) == 1
            assert results[0]["status"] == "rejected"
            assert results[0]["effect_state"] == "none_applied"
            assert results[0]["output"]["rule_id"] == "dispatch.not_admitted"
    finally:
        conn.close()
        if process.is_alive():
            process.terminate()
        process.join(5)
