"""Responses 完整 Agent 往返、scope、审批恢复与真实进程事务崩溃验收。"""

import os
import subprocess
import sys
from contextlib import closing

import pytest
from support import CHILD_TEXT, approve_pending, engine_for, stack_for
from test_responses_model import Transport, function, message, model, reasoning, response

from tianshu.events import EventLog
from tianshu.loop import Limits
from tianshu.replay import replay


def _stack(tmp_path):
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    stack = stack_for(log, data_root=root)
    task = stack.tasks.create_task("检查目录", [str(ws)])
    return log, stack, task, ws


def test_real_tool_roundtrip_persists_continuation_replay_and_projection(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    try:
        transport = Transport(response([reasoning(), function()]), response())
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        results = [e for e in events if e.type == "model.result"]
        assert results[0].payload_version == 2
        saved = results[0].payload["continuation"]
        assert stack.tasks.list_model_turns(result.run_id)[0]["continuation"] == saved
        assert stack.tasks.load_cursor(result.run_id)[0].messages[2]["continuation"] == saved
        assert [e for e in events if e.type == "tool.result"][0].payload["call_id"] == "call_1"
        assert replay(log, result.run_id).ok
        before = [(e.seq, e.payload_hash) for e in events]
        # 删除真实投影，不能把“保留原行后重建 tasks/runs”当成续接恢复证据。
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        assert stack.tasks.list_model_turns(result.run_id)[0]["continuation"] == saved
        assert stack.tasks.load_cursor(result.run_id)[0].messages[2]["continuation"] == saved
        assert [(e.seq, e.payload_hash) for e in log.read_run(result.run_id)] == before
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_responses_over_budget_does_not_spill_or_summarize(tmp_path):
    log, stack, task, ws = _stack(tmp_path)
    (ws / "large.txt").write_bytes(b"x" * 25000)
    f = function()
    f.update(name="read_text", arguments='{"path":"large.txt"}')
    transport = Transport(response([reasoning(), f]), response())
    try:
        result = engine_for(
            stack, model(transport), limits=Limits(context_budget_bytes=12000, summary_mode="model")
        ).start(task.task_id, out_name="")
        assert result.outcome == "failed"
        assert len(transport.calls) == 1
        assert "预算" in result.error
        events = log.read_run(result.run_id)
        intents = [e for e in events if e.type == "model.intent"]
        assert intents[0].payload["context"]["budget_version"] == "ctx-responses-v1"
        assert not intents[0].payload["context"]["dropped_seqs"]
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_cached_usage_survives_both_v2_model_events_in_real_loop(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    data = response()
    data["usage"]["input_tokens_details"] = {"cached_tokens": 4}
    try:
        result = engine_for(stack, model(Transport(data))).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        saved = next(e.payload for e in events if e.type == "model.result")
        assert saved["usage"]["cache_read_input_tokens"] == 4
        assert saved["cost"]["cost"] is None
        assert stack.tasks.list_model_turns(result.run_id)[0]["usage"] == saved["usage"]
        turn_event = next(e.payload for e in events if e.type == "run.model_turn")
        assert turn_event["usage"] == saved["usage"]
        assert next(e.payload_version for e in events if e.type == "run.model_turn") == 2
        assert replay(log, result.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("arguments", ['{"path":', '[]', '"not-an-object"'])
def test_invalid_arguments_are_refused_and_raw_continuation_survives_replay(tmp_path, arguments):
    log, stack, task, _ = _stack(tmp_path)
    transport = Transport(response([function(arguments=arguments)]), response())
    try:
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "noop"
        events = log.read_run(result.run_id)
        assert not [e for e in events if e.type in ("tool.intent", "tool.result")]
        assert len([e for e in events if e.type == "tool.not_dispatched"]) == 1
        function_input = next(
            item for item in transport.calls[1]["body"]["input"]
            if item.get("type") == "function_call"
        )
        assert function_input["arguments"] == arguments
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_incomplete_response_with_valid_arguments_never_executes_tool(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    incomplete = response(
        [function()], status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    transport = Transport(incomplete)
    try:
        result = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert result.outcome == "failed"
        events = log.read_run(result.run_id)
        assert len(transport.calls) == 1
        assert not [e for e in events if e.type in ("tool.intent", "tool.result")]
        assert len([e for e in events if e.type == "tool.not_dispatched"]) == 1
        assert replay(log, result.run_id).ok
    finally:
        log.close()


def test_approval_resume_reuses_saved_model_after_projection_loss(tmp_path):
    log, stack, task, ws = _stack(tmp_path)
    (ws / "note.txt").write_text("before")
    f = function()
    f.update(name="modify_text", arguments='{"path":"note.txt","content":"after"}')
    transport = Transport(response([reasoning(), f]), response())
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "waiting_approval"
        assert len(transport.calls) == 1
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        assert stack.tasks.load_cursor(first.run_id)[0].approval_ids == first.approval_ids
        approve_pending(stack, first.run_id)
        # 批准后换一个 loop 实例，保存的函数提案不能重新问模型。
        second = engine_for(stack, model(transport)).resume_run(first.run_id)
        assert second.outcome in {"noop", "succeeded"}
        assert len(transport.calls) == 2
        assert (ws / "note.txt").read_text() == "after"
        assert len([e for e in log.read_run(first.run_id) if e.type == "tool.intent"]) == 1
        assert replay(log, first.run_id).ok
    finally:
        log.close()


@pytest.mark.parametrize("vendor", ["openai", "anthropic", "scripted"])
def test_scope_switch_after_approval_cannot_drop_saved_continuation(tmp_path, vendor):
    from test_model_contract import FakeTransport, _model

    from tianshu.model import ModelResponse, ScriptedModel

    log, stack, task, ws = _stack(tmp_path)
    (ws / "note.txt").write_text("before")
    f = function()
    f.update(name="modify_text", arguments='{"path":"note.txt","content":"after"}')
    transport = Transport(response([reasoning(), f]))
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "waiting_approval"
        approve_pending(stack, first.run_id)
        other = (
            ScriptedModel([ModelResponse("end_turn")])
            if vendor == "scripted"
            else _model(vendor, FakeTransport({}))
        )
        result = engine_for(stack, other).resume_run(first.run_id)
        assert result.outcome == "failed"
        assert "新 Run" in result.error
        assert len([e for e in log.read_run(first.run_id) if e.type == "model.result"]) == 1
    finally:
        log.close()


_CHILD = r"""
import json, os
from pathlib import Path
from tianshu.events import EventLog
from tianshu.loop import AgentLoop, build_stack
from tianshu.openai_responses import OpenAIResponsesModel
root = Path(os.environ["RESPONSES_CASE"])
ws = root / "ws"; ws.mkdir(exist_ok=True)
state = root / "state"; state.mkdir(exist_ok=True)
log = EventLog(state / "events.db", blob_dir=state / "blobs")
stack = build_stack(log, data_root=state)
task = stack.tasks.create_task("检查目录", [str(ws)])
class T:
    def post(self, url, **kw):
        if os.environ.get("RESPONSES_TERMINAL"):
            return {"id":"resp_1","status":"completed","output":[
                {"type":"message","id":"msg_1","role":"assistant","status":"completed",
                 "content":[{"type":"output_text","text":"done","annotations":[]}]}
            ],"usage":{"input_tokens":1,"output_tokens":1}}
        return {"id":"resp_1", "status":"completed", "model":"test-model", "output":[
            {"type":"reasoning","id":"rs_1","summary":[],"encrypted_content":"opaque"},
            {"type":"function_call","id":"fc_1","call_id":"call_1","name":"list_files","arguments":json.dumps({"path":"."}),"status":"completed"}
        ],"usage":{"input_tokens":1,"output_tokens":1}}
m = OpenAIResponsesModel("test-model", api_key="fake", transport=T())
engine = AgentLoop(tasks=stack.tasks, leases=stack.leases, claims=stack.claims,
    dispatcher=stack.dispatcher, approvals=stack.approvals, snapshots=stack.snapshots,
    contents=stack.contents, budget=stack.budget, memory=stack.memory,
    summaries=stack.summaries, model=m)
engine.start(task.task_id, out_name="")
"""


_MISSING_CURSOR_CANCEL_CHILD = r"""
import json
import sys
from pathlib import Path
from tianshu.control import ControlPlane
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.openai_responses import OpenAIResponsesModel
from tianshu.restore_access import default_material_paths
from tianshu.worker import Worker

root = Path(sys.argv[1])
db = root / "events.db"
workspace = root / "ws"
workspace.mkdir()
log = EventLog(db, blob_dir=default_material_paths(db, root)[1], data_root=root)
stack = build_stack(log, data_root=root)
task = stack.tasks.create_task("写报告", [str(workspace)])
class Transport:
    def post(self, url, *, body, **kwargs):
        return {
            "id": "resp_probe", "status": "completed",
            "output": [{
                "type": "function_call", "id": "fc_probe", "call_id": "responses-write",
                "name": "create_report",
                "arguments": json.dumps({"name": "r.md", "content": "changed"}),
                "status": "completed",
            }],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
model = OpenAIResponsesModel("test-model", api_key="review-only", transport=Transport())
ControlPlane(stack, model=model).handle("run.start", {
    "task_id": task.task_id, "mode": "execute", "request_id": "responses-probe",
    "out_name": "r.md",
})
with Worker(stack, model=model, heartbeat_interval_ms=None) as worker:
    worker.run_once()
"""


@pytest.mark.parametrize("derived_before_cancel", [False, True])
def test_responses_missing_cursor_unknown_write_cannot_be_cancelled(
    tmp_path, derived_before_cancel,
):
    from tianshu.control import ControlPlane
    from tianshu.jobs import JobStore
    from tianshu.model import ScriptedModel
    from tianshu.restore_access import default_material_paths
    from tianshu.worker import Worker

    db = tmp_path / "events.db"
    workspace = tmp_path / "ws"
    child = subprocess.run(
        [sys.executable, "-c", _MISSING_CURSOR_CANCEL_CHILD, str(tmp_path)],
        env={**os.environ, "TIANSHU_FAILPOINT": "after_write"},
        capture_output=True, timeout=30, **CHILD_TEXT,
    )
    assert child.returncode == 77 and "[failpoint] after_write" in child.stderr, child.stderr
    target = workspace / "r.md"
    assert target.read_bytes() == b"changed"

    class NeverModel(ScriptedModel):
        def complete(self, messages, tools):
            raise AssertionError("缺游标 Responses 取消不得重新调用模型")

    with closing(EventLog(
        db, blob_dir=default_material_paths(db, tmp_path)[1], data_root=tmp_path,
    )) as log:
        stack = stack_for(log, data_root=tmp_path)
        (run_id,) = log.conn.execute("SELECT run_id FROM runs").fetchone()
        before = log.read_run(run_id)
        assert any(e.type == "model.result" and "continuation" in e.payload for e in before)
        assert any(e.type == "tool.intent" for e in before)
        assert not any(e.type == "tool.result" for e in before)
        assert not any(e.type == "receipt" for e in before)
        assert stack.tasks.get_run(run_id).effect_state is None
        with log.transaction():
            assert log.conn.execute(
                "DELETE FROM run_cursors WHERE run_id=?", (run_id,),
            ).rowcount == 1
        assert stack.tasks.load_cursor(run_id) is None
        cursor_events_before = log.read_run(run_id, event_types=("run.cursor",))
        if derived_before_cancel:
            assert stack.tasks.restore_responses_cursor(run_id) is not None
            assert stack.tasks.load_cursor(run_id) is not None

        model_instance = NeverModel([])
        plane = ControlPlane(stack, model=model_instance)
        requested = plane.handle("run.cancel", {"run_id": run_id})["run"]
        assert requested["state"] == "running"
        assert requested["control_requested"] == "cancel"
        evidence_types = (
            "model.intent", "model.result", "tool.intent", "tool.result", "receipt", "run.end",
        )
        evidence_before = log.read_run(run_id, event_types=evidence_types)
        with Worker(stack, model=model_instance, heartbeat_interval_ms=None) as worker:
            assert worker.run_once()
            assert not worker.run_once()
        assert log.read_run(run_id, event_types=evidence_types) == evidence_before
        assert JobStore(log).job_for_run(run_id).state == "waiting"
        run = stack.tasks.get_run(run_id)
        assert run.state == "reconciling"
        assert run.control_requested == "cancel"
        assert run.effect_state is None
        assert stack.tasks.load_cursor(run_id) is not None
        assert log.read_run(run_id, event_types=("run.cursor",)) == cursor_events_before
        assert target.read_bytes() == b"changed"


@pytest.mark.parametrize(
    "point,committed", [("model-result-before-commit", False), ("model-result-after-commit", True)]
)
def test_real_subprocess_crash_preserves_atomic_result_turn_cursor(tmp_path, point, committed):
    env = {**os.environ, "TIANSHU_FAILPOINT": point, "RESPONSES_CASE": str(tmp_path)}
    p = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, timeout=45)
    assert p.returncode == 77, p.stderr.decode("utf-8", "replace")
    assert f"[failpoint] {point}".encode() in p.stderr
    root = tmp_path / "state"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        results = [e for e in log.read_run(run_id) if e.type == "model.result"]
        turns = stack.tasks.list_model_turns(run_id)
        cursor = stack.tasks.load_cursor(run_id)
        assert bool(results) is committed
        assert bool(turns) is committed
        assert bool(cursor and any("continuation" in m for m in cursor[0].messages)) is committed
        data = [response()] if committed else [response([reasoning(), function()]), response()]
        transport = Transport(*data)
        result = engine_for(stack, model(transport)).resume_run(run_id)
        assert result.outcome == "noop"
        assert len(transport.calls) == (1 if committed else 2)
        assert len([e for e in log.read_run(run_id) if e.type == "tool.result"]) == 1
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_committed_terminal_result_resumes_without_new_generation(tmp_path):
    env = {
        **os.environ,
        "TIANSHU_FAILPOINT": "model-result-after-commit",
        "RESPONSES_CASE": str(tmp_path),
        "RESPONSES_TERMINAL": "1",
    }
    child = subprocess.run([sys.executable, "-c", _CHILD], env=env, capture_output=True, timeout=45)
    assert child.returncode == 77, child.stderr.decode("utf-8", "replace")
    root = tmp_path / "state"
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    try:
        stack = stack_for(log, data_root=root)
        run_id = log.conn.execute("SELECT run_id FROM runs").fetchone()[0]
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        transport = Transport()  # 任何模型调用都会失败，已提交的 end_turn 应直接收尾。
        result = engine_for(stack, model(transport)).resume_run(run_id)
        assert result.outcome == "noop"
        assert not transport.calls
        assert len([e for e in log.read_run(run_id) if e.type == "model.result"]) == 1
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_pause_after_terminal_response_resumes_without_new_generation(tmp_path):
    log, stack, task, _ = _stack(tmp_path)

    class PauseTransport(Transport):
        def post(self, url, **kwargs):
            result = super().post(url, **kwargs)
            run_id = stack.tasks.get_task(task.task_id).active_run_id
            stack.tasks.request_pause(run_id)
            return result

    transport = PauseTransport(response())
    try:
        first = engine_for(stack, model(transport)).start(task.task_id, out_name="")
        assert first.state == "paused" and len(transport.calls) == 1
        resumed_transport = Transport()
        second = engine_for(stack, model(resumed_transport)).resume_run(first.run_id)
        assert second.outcome == "noop"
        assert not resumed_transport.calls
        assert replay(log, first.run_id).ok
    finally:
        log.close()


def test_default_diagnostic_archive_excludes_continuation(tmp_path):
    import zipfile

    from tianshu.diagnostics_export import export_diagnostics

    log, stack, task, _ = _stack(tmp_path)
    try:
        result = engine_for(stack, model(Transport(response([reasoning(), message()])))).start(
            task.task_id, out_name=""
        )
        assert result.outcome == "noop"
        assert any("continuation" in e.payload for e in log.read_run(result.run_id))
        exported = export_diagnostics(
            tmp_path / "state", tmp_path / "export", sections=["events_timeline"]
        )
        with zipfile.ZipFile(exported["archive_path"]) as archive:
            content = b"".join(archive.read(p) for p in archive.namelist())
        assert b"opaque-secret-reasoning" not in content
        assert b"output_items" not in content
    finally:
        log.close()


def test_desktop_attachment_and_guidance_rebuild_exact_responses_cursor(tmp_path):
    from tianshu.control import ControlPlane

    log, stack, task, _ = _stack(tmp_path)
    try:
        class GuidedTransport(Transport):
            def post(self, url, *, body, headers, timeout):
                if not self.calls:
                    plane.handle("run.guide", {"run_id": run_id, "text": "补充：用中文回答。"})
                return super().post(url, body=body, headers=headers, timeout=timeout)

        transport = GuidedTransport(response([message("first")]), response([message("second")]))
        adapter = model(transport)
        plane = ControlPlane(stack, model=adapter)
        run_id = plane.handle("run.start", {
            "task_id": task.task_id,
            "attachments": [{
                "name": "notes.txt", "media_type": "text/plain", "data_base64": "YQ==",
            }],
        })["run"]["run_id"]
        assert engine_for(stack, adapter).resume_run(run_id).terminal
        first_input = transport.calls[0]["body"]["input"]
        assert first_input[-1]["content"] == [
            {"type": "input_text", "text": "检查目录"},
            {"type": "input_text", "text": '附件（用户提供的资料）："notes.txt"\na'},
        ]
        assert transport.calls[1]["body"]["input"][-1] == {
            "role": "user", "content": "补充：用中文回答。",
        }
        before = stack.tasks.load_cursor(run_id)[0].to_dict()
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors WHERE run_id=?", (run_id,))
            log.conn.execute("DELETE FROM run_turns WHERE run_id=?", (run_id,))
        rebuilt = stack.tasks.restore_responses_cursor(run_id)
        assert rebuilt[0].to_dict() == before
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_responses_plan_preserves_attachment_and_rebuilds_workflow_cursor(tmp_path):
    from base64 import b64encode

    from tianshu.control import ControlPlane

    log, stack, task, _ = _stack(tmp_path)
    transport = Transport(response([reasoning(), message('先检查目录，再报告结果。')]))
    provider = model(transport)
    plane = ControlPlane(stack, model=provider)
    try:
        run = plane.handle('run.start', {
            'task_id': task.task_id, 'mode': 'plan', 'request_id': 'plan-1',
            'attachments': [{'name': 'brief.txt', 'media_type': 'text/plain',
                             'data_base64': b64encode(b'unique-brief').decode()}],
        })['run']
        run_id = run['run_id']
        assert run['workflow']['mode'] == 'plan'
        assert engine_for(stack, provider).resume_run(run_id).outcome == 'noop'
        assert 'unique-brief' in repr(transport.calls[0]['body']['input'])
        before = stack.tasks.load_cursor(run_id)[0].to_dict()
        with log.transaction():
            log.conn.execute('DELETE FROM run_cursors')
            log.conn.execute('DELETE FROM run_turns')
        stack.tasks.rebuild_from_events()
        assert stack.tasks.load_cursor(run_id)[0].to_dict() == before
        assert replay(log, run_id).ok
    finally:
        log.close()


def test_responses_compact_new_run_preserves_dialogue_and_excludes_old_reasoning(tmp_path):
    from tianshu.control import ControlPlane

    log, stack, task, _ = _stack(tmp_path)
    transport = Transport(
        response([reasoning(), message('原回答')]),
        response([reasoning(), message('后续回答')]),
        response([reasoning(), message('保留已完成的检查；下一步核对目录。')]),
        response([reasoning(), message('继续完成')]),
    )
    provider = model(transport)
    plane = ControlPlane(stack, model=provider)
    engine = engine_for(stack, provider)
    try:
        first = plane.handle('run.start', {'task_id': task.task_id})['run']['run_id']
        assert engine.resume_run(first).terminal
        second = plane.handle('run.follow_up', {
            'task_id': task.task_id, 'source_run_id': first, 'message': '请保留这句用户约束',
        })['run']['run_id']
        assert engine.resume_run(second).terminal
        compact = plane.handle('run.compact', {
            'task_id': task.task_id, 'source_run_id': second, 'request_id': 'compact-responses',
        })['run']['run_id']
        assert engine.resume_run(compact).outcome == 'noop'
        compact_body = transport.calls[2]['body']
        assert '原回答' in repr(compact_body['input'])
        assert '请保留这句用户约束' in repr(compact_body['input'])
        assert not compact_body['tools']
        third = plane.handle('run.start', {'task_id': task.task_id})['run']['run_id']
        assert engine.resume_run(third).outcome == 'noop'
        assert '下一步核对目录' in repr(transport.calls[3]['body']['input'])
        for call in transport.calls[2:]:
            assert not any(item.get('type') == 'reasoning' for item in call['body']['input'])
        for run_id in [first, second, compact, third]:
            assert replay(log, run_id).ok
    finally:
        log.close()


def test_responses_compacts_committed_prefix_without_orphaning_tool_results(tmp_path):
    log, stack, task, _ = _stack(tmp_path)
    compaction = {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque-window"}
    transport = Transport(
        response([reasoning(), function()]),
        response([compaction, function(id="fc_2", call_id="call_2")]),
        response(),
    )
    # This fixture uses HTTP JSON deliberately; SSE framing is covered by adapter tests.
    class JsonTransport:
        post = transport.post

    try:
        result = engine_for(
            stack, model(JsonTransport(), compact_threshold=2048),
            limits=Limits(summary_mode="model"),
        ).start(task.task_id, out_name="")
        assert result.outcome == "noop", result.error
        bodies = [call["body"] for call in transport.calls]
        assert len(bodies) == 3
        assert all(b["context_management"] == [{"type": "compaction", "compact_threshold": 2048}]
                   for b in bodies)
        assert bodies[1]["input"][-1]["call_id"] == "call_1"
        assert [i.get("type") for i in bodies[2]["input"]] == [
            None, "compaction", "function_call", "function_call_output",
        ]
        assert bodies[2]["input"][1] == compaction
        assert bodies[2]["input"][-1]["call_id"] == "call_2"
        events = log.read_run(result.run_id)
        compacted = next(e for e in events if e.type == "model.result" and e.payload_version == 3)
        assert compacted.payload["continuation"]["schema_version"] == "1.1"
        assert replay(log, result.run_id).ok
        with log.transaction():
            log.conn.execute("DELETE FROM run_cursors")
            log.conn.execute("DELETE FROM run_turns")
        stack.tasks.rebuild_from_events()
        assert replay(log, result.run_id).ok
    finally:
        log.close()
